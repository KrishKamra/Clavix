"""Unit and model-backed tests for acoustic ingestion."""

from __future__ import annotations

import asyncio
import json
import math
import re
import subprocess
import sys
import threading
import types
import wave
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from clavix.modules import acoustic_whisper
from clavix.modules.acoustic import Transcript
from clavix.modules.acoustic_whisper import (
    AudioSource,
    Endpointer,
    MicrophoneSource,
    WavFileSource,
    WhisperAcoustic,
)
from clavix.testing.contracts import AcousticContract

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = sorted((ROOT / "tests" / "fixtures" / "audio").glob("*.wav"))
SUITE = ROOT / "configs" / "benchmark_suite.json"

FRAME = 480
SILENCE = np.zeros(FRAME, dtype=np.float32)
SINE = (0.1 * np.sin(2 * np.pi * 440 * np.arange(FRAME) / 16_000)).astype(np.float32)
LOUD = np.full(FRAME, 0.1, dtype=np.float32)


def word_error_rate(reference: str, hypothesis: str) -> float:
    """Word-level Levenshtein distance over casefolded, punctuation-free words."""
    expected = re.sub(r"[^\w\s]", "", reference.casefold()).split()
    actual = re.sub(r"[^\w\s]", "", hypothesis.casefold()).split()
    previous = list(range(len(actual) + 1))
    for row, expected_word in enumerate(expected, start=1):
        current = [row]
        for column, actual_word in enumerate(actual, start=1):
            current.append(
                min(
                    current[column - 1] + 1,
                    previous[column] + 1,
                    previous[column - 1] + (expected_word != actual_word),
                )
            )
        previous = current
    return previous[-1] / max(1, len(expected))


class TestEndpointer:
    def test_detects_speech_with_preroll_and_trimmed_trailing_silence(self) -> None:
        detector = Endpointer(silence_ms=300, min_speech_ms=90, pre_roll_ms=60)
        assert not detector.feed(SILENCE)
        assert not detector.feed(SILENCE)
        completed = False
        for _ in range(34):  # ~1 s of speech
            completed = detector.feed(SINE)
        assert not completed
        assert detector.speaking
        for _ in range(10):
            completed = detector.feed(SILENCE)
            if completed:
                break
        assert completed
        audio = detector.audio
        # 2 pre-roll frames + 34 speech frames + exactly 200 ms of the 300 ms silence.
        assert len(audio) == 36 * FRAME + 3_200
        assert np.max(np.abs(audio[: 2 * FRAME])) == 0.0
        assert np.max(np.abs(audio[2 * FRAME : 36 * FRAME])) > 0.0
        assert np.max(np.abs(audio[36 * FRAME :])) == 0.0

    def test_silence_alone_never_completes(self) -> None:
        detector = Endpointer()
        assert not any(detector.feed(SILENCE) for _ in range(200))
        assert not detector.speaking
        assert detector.audio.size == 0

    def test_ignores_speech_shorter_than_minimum(self) -> None:
        detector = Endpointer(min_speech_ms=90, pre_roll_ms=0)
        for _ in range(2):
            assert not detector.feed(LOUD)
        for _ in range(10):
            assert not detector.feed(SILENCE)
        assert not detector.speaking
        assert detector.audio.size == 0

    def test_short_burst_stays_in_preroll(self) -> None:
        # A rejected burst must not leave a gap in the pre-roll ring.
        detector = Endpointer(min_speech_ms=90, pre_roll_ms=150, silence_ms=60)
        marker = np.full(FRAME, 0.5, dtype=np.float32)
        detector.feed(marker)  # 1-frame burst, rejected
        detector.feed(SILENCE)
        for _ in range(3):
            detector.feed(LOUD)
        detector.feed(SILENCE)
        assert detector.feed(SILENCE)
        audio = detector.audio
        assert audio[0] == pytest.approx(0.5)  # burst, silence, then the 3 onset frames
        assert len(audio) == 5 * FRAME + 2 * FRAME

    def test_maximum_duration_completes_utterance(self) -> None:
        detector = Endpointer(min_speech_ms=30, max_utterance_sec=0.09, pre_roll_ms=0)
        assert not detector.feed(LOUD)
        assert not detector.feed(LOUD)
        assert detector.feed(LOUD)
        assert detector.complete
        assert detector.feed(LOUD)  # stays complete
        assert len(detector.audio) == 3 * FRAME

    def test_cutoff_keeps_trailing_silence_under_200_ms(self) -> None:
        detector = Endpointer(min_speech_ms=30, max_utterance_sec=0.3, pre_roll_ms=0, silence_ms=3_000)
        detector.feed(LOUD)
        completed = False
        while not completed:
            completed = detector.feed(SILENCE)
        # 1 speech frame + 9 silent frames (270 ms): the tail is cut to 200 ms, not removed.
        assert len(detector.audio) == FRAME + 3_200

    def test_calibration_raises_threshold_under_noise(self) -> None:
        noise = np.full(FRAME, 0.02, dtype=np.float32)
        assert Endpointer.calibrate([noise, noise], 0.01) == pytest.approx(0.06)
        assert Endpointer.calibrate([SILENCE], 0.01) == 0.01
        assert Endpointer.calibrate([], 0.01) == 0.01

    def test_frame_rms(self) -> None:
        assert Endpointer.frame_rms(LOUD) == pytest.approx(0.1)
        assert Endpointer.frame_rms(np.empty(0, dtype=np.float32)) == 0.0


class FakeSoundDevice(types.ModuleType):
    """Stands in for ``sounddevice``: each stream replays the next scripted frame list."""

    def __init__(self, scripts: list[list[np.ndarray]], close_sec: float = 0.0) -> None:
        super().__init__("sounddevice")
        self.scripts = scripts
        self.opened: list[dict[str, Any]] = []
        self.open_streams = 0
        self.max_open_streams = 0
        fake = self

        class InputStream:
            def __init__(self, **kwargs: Any) -> None:
                fake.opened.append(kwargs)
                fake.open_streams += 1
                fake.max_open_streams = max(fake.max_open_streams, fake.open_streams)
                self._callback = kwargs["callback"]
                self._frames = fake.scripts.pop(0)
                self.active = False

            def start(self) -> None:
                self.active = True
                for frame in self._frames:
                    self._callback(frame[:, None], len(frame), None, None)
                self.active = False  # script exhausted: the stream has stopped

            def close(self) -> None:
                acoustic_whisper.time.sleep(close_sec)
                fake.open_streams -= 1

        self.InputStream = InputStream


class TestMicrophoneSource:
    def test_calibrates_once_then_endpoints(self, monkeypatch: pytest.MonkeyPatch) -> None:
        noise = np.full(FRAME, 0.02, dtype=np.float32)  # RMS 0.02 -> threshold 0.06
        speech = np.full(FRAME, 0.05, dtype=np.float32)  # under the calibrated threshold
        louder = np.full(FRAME, 0.2, dtype=np.float32)
        first = [noise] * 2 + [speech] * 5 + [louder] * 5 + [SILENCE] * 3
        second = [louder] * 4 + [SILENCE] * 3  # no calibration frames needed
        sd = FakeSoundDevice([first, second])
        monkeypatch.setitem(sys.modules, "sounddevice", sd)

        source = MicrophoneSource(silence_ms=90, min_speech_ms=60, pre_roll_ms=0, calibrate_sec=0.06)
        assert source.threshold is None
        audio, t_end = source.read_utterance()
        assert source.threshold == pytest.approx(0.06)
        assert audio.dtype == np.float32
        assert np.all(audio[: 5 * FRAME] == pytest.approx(0.2))
        assert t_end > 0

        audio, _ = source.read_utterance()
        assert np.all(audio[: 4 * FRAME] == pytest.approx(0.2))
        assert sd.opened[0] | {"callback": None} == {
            "samplerate": 16_000,
            "blocksize": 480,
            "device": None,
            "channels": 1,
            "dtype": "float32",
            "callback": None,
        }

    def test_slow_close_is_off_the_critical_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        utterance = [LOUD] * 4 + [SILENCE] * 3
        sd = FakeSoundDevice([list(utterance), list(utterance)], close_sec=0.5)
        monkeypatch.setitem(sys.modules, "sounddevice", sd)
        source = MicrophoneSource(silence_ms=90, min_speech_ms=60, pre_roll_ms=0, calibrate_sec=0)

        started = acoustic_whisper.time.perf_counter()
        source.read_utterance()
        assert acoustic_whisper.time.perf_counter() - started < 0.3  # did not wait for close()
        assert sd.open_streams == 1  # still closing in the background
        source.read_utterance()
        assert sd.max_open_streams == 1  # the second read waited for the first close

    def test_dead_stream_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "sounddevice", FakeSoundDevice([[SILENCE] * 3]))
        with pytest.raises(RuntimeError, match="stopped"):
            MicrophoneSource(calibrate_sec=0).read_utterance()

    def test_abort_interrupts_capture(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "sounddevice", FakeSoundDevice([[SILENCE] * 3]))
        source = MicrophoneSource(calibrate_sec=0)
        monkeypatch.setattr(source._abort, "clear", lambda: None)  # abort lands before the read
        source.abort()
        with pytest.raises(InterruptedError):
            source.read_utterance()


class TestWavFileSource:
    def test_raises_eof_when_exhausted(self) -> None:
        with pytest.raises(EOFError, match=r"^EOF"):  # the CLI's loop stops on "EOF..."
            WavFileSource([]).read_utterance()

    def test_decodes_pcm16_with_stdlib(self, tmp_path: Path) -> None:
        mono = tmp_path / "mono.wav"
        stereo = tmp_path / "stereo.wav"
        for path, channels, samples in ((mono, 1, [16384, -32768]), (stereo, 2, [16384, 0, -32768, 0])):
            with wave.open(str(path), "wb") as wav_file:
                wav_file.setnchannels(channels)
                wav_file.setsampwidth(2)
                wav_file.setframerate(16_000)
                wav_file.writeframes(np.array(samples, dtype="<i2").tobytes())
        source = WavFileSource([str(mono), str(stereo)])
        audio, _ = source.read_utterance()
        assert audio.dtype == np.float32
        assert audio.tolist() == [0.5, -1.0]
        audio, _ = source.read_utterance()
        assert audio.tolist() == [0.25, -0.5]


def test_confidence_is_mean_exponentiated_logprob_and_clipped() -> None:
    expected = (math.exp(-1.0) + math.exp(-2.0)) / 2
    assert WhisperAcoustic._confidence([-1.0, -2.0]) == pytest.approx(expected)
    assert WhisperAcoustic._confidence([0.1]) == 1.0
    assert WhisperAcoustic._confidence([]) is None


class FixedSource(AudioSource):
    def __init__(self, *utterances: np.ndarray) -> None:
        self._utterances = list(utterances)

    def read_utterance(self) -> tuple[np.ndarray, float]:
        if not self._utterances:
            raise EOFError("EOF: no utterances left")
        return self._utterances.pop(0), acoustic_whisper.time.perf_counter()


class FakeModel:
    """Mimics ``WhisperModel.transcribe``: lazy segments plus an info object."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.decode_threads: list[str] = []

    def transcribe(self, audio: np.ndarray, **kwargs: Any) -> tuple[Iterator[Any], Any]:
        self.calls.append(kwargs)

        def segments() -> Iterator[Any]:
            self.decode_threads.append(threading.current_thread().name)
            yield types.SimpleNamespace(text=" Open ", avg_logprob=-0.5, no_speech_prob=0.01)
            yield types.SimpleNamespace(text=" GitHub. ", avg_logprob=0.2, no_speech_prob=0.02)

        info = types.SimpleNamespace(duration=len(audio) / 16_000, duration_after_vad=0.5)
        return segments(), info


@pytest.fixture
def fake_model(monkeypatch: pytest.MonkeyPatch) -> FakeModel:
    model = FakeModel()
    monkeypatch.setattr(acoustic_whisper, "_shared_model", lambda *args: model)
    return model


class TestWhisperAcousticWithFakeModel:
    def test_listen_transcribes_on_dedicated_thread(self, fake_model: FakeModel) -> None:
        acoustic = WhisperAcoustic(source=FixedSource(LOUD, LOUD), beam_size=2)
        try:
            transcript = asyncio.run(acoustic.listen())
            asyncio.run(acoustic.listen())
        finally:
            acoustic.close()
        assert transcript.text == "Open GitHub."
        assert transcript.confidence == pytest.approx((math.exp(-0.5) + math.exp(0.2)) / 2)
        assert transcript.t_asr_ms > 0.0
        assert fake_model.calls[0] == {
            "language": "en",
            "beam_size": 2,
            "vad_filter": True,
            "condition_on_previous_text": False,
        }
        assert all(name.startswith("clavix-asr") for name in fake_model.decode_threads)

    def test_empty_audio_skips_the_model(self, fake_model: FakeModel) -> None:
        acoustic = WhisperAcoustic(source=FixedSource(np.empty(0, dtype=np.float32)))
        transcript = asyncio.run(acoustic.listen())
        acoustic.close()
        assert transcript.text == ""
        assert transcript.confidence is None
        assert fake_model.calls == []

    def test_eof_propagates_and_close_is_final(self, fake_model: FakeModel) -> None:
        acoustic = WhisperAcoustic(source=FixedSource())
        with pytest.raises(EOFError):
            asyncio.run(acoustic.listen())
        acoustic.close()
        acoustic.close()  # idempotent
        with pytest.raises(RuntimeError, match="closed"):
            asyncio.run(acoustic.listen())

    def test_cancelled_listen_aborts_the_capture_thread(self, fake_model: FakeModel) -> None:
        class BlockingSource(AudioSource):
            def __init__(self) -> None:
                self.started = threading.Event()
                self.aborted = threading.Event()

            def read_utterance(self) -> tuple[np.ndarray, float]:
                self.started.set()
                self.aborted.wait(timeout=5)
                raise InterruptedError("aborted")

            def abort(self) -> None:
                self.aborted.set()

        source = BlockingSource()
        acoustic = WhisperAcoustic(source=source)

        async def cancel_while_listening() -> None:
            task = asyncio.create_task(acoustic.listen())
            await asyncio.to_thread(source.started.wait, 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.run(cancel_while_listening())
        acoustic.close()
        assert source.aborted.is_set()

    def test_warmup_loads_the_model(self, fake_model: FakeModel) -> None:
        acoustic = WhisperAcoustic(source=FixedSource())
        acoustic.warmup()
        acoustic.close()
        assert len(fake_model.calls) == 1

    def test_from_wav_files_accepts_runtime_mic_options(self) -> None:
        acoustic = WhisperAcoustic.from_wav_files(["x.wav"], model_size="tiny.en", silence_ms=700)
        assert acoustic.model_size == "tiny.en"
        assert isinstance(acoustic._source, WavFileSource)


def test_constructing_acoustic_imports_no_audio_packages() -> None:
    code = (
        "import sys; from clavix.modules.acoustic_whisper import WhisperAcoustic; "
        "WhisperAcoustic(); WhisperAcoustic.from_wav_files(['x.wav']); "
        "loaded = {'faster_whisper', 'sounddevice', 'ctranslate2'} & set(sys.modules); "
        "assert not loaded, loaded"
    )
    subprocess.run([sys.executable, "-c", code], check=True, cwd=ROOT)


def test_word_error_rate_normalizes_case_and_punctuation() -> None:
    assert word_error_rate("open github", "Open GitHub.") == 0.0
    assert word_error_rate("run echo clavix ready", "Run Echo Clovix Ready.") == 0.25
    assert word_error_rate("a b", "") == 1.0


def test_every_benchmark_task_has_a_fixture() -> None:
    tasks = {task["task_id"] for task in json.loads(SUITE.read_text(encoding="utf-8"))["tasks"]}
    assert {path.stem for path in FIXTURES} == tasks
    for path in FIXTURES:
        assert path.stat().st_size < 300_000
        with wave.open(str(path), "rb") as wav_file:
            assert (wav_file.getnchannels(), wav_file.getsampwidth(), wav_file.getframerate()) == (
                1,
                2,
                16_000,
            )


@pytest.mark.slow
class TestWhisperAcousticContract(AcousticContract):
    listen_calls = 2

    @pytest.fixture
    def impl(self) -> Iterator[WhisperAcoustic]:
        acoustic = WhisperAcoustic.from_wav_files([str(path) for path in FIXTURES[:2]], model_size="tiny.en")
        yield acoustic
        acoustic.close()


@pytest.mark.slow
def test_fixture_transcription_wer_at_most_025() -> None:
    commands = {
        task["task_id"]: task["command"] for task in json.loads(SUITE.read_text(encoding="utf-8"))["tasks"]
    }
    assert len(FIXTURES) == 10
    acoustic = WhisperAcoustic.from_wav_files([str(path) for path in FIXTURES], model_size="base.en")
    acoustic.warmup()
    results: dict[str, Transcript] = {}
    try:
        for path in FIXTURES:
            results[path.stem] = asyncio.run(acoustic.listen())
    finally:
        acoustic.close()
    errors = {
        task_id: (word_error_rate(commands[task_id], transcript.text), transcript.text)
        for task_id, transcript in results.items()
    }
    assert all(wer <= 0.25 for wer, _ in errors.values()), errors
    assert all(transcript.t_asr_ms > 0.0 for transcript in results.values())


@pytest.mark.slow
def test_instances_share_one_loaded_model() -> None:
    first = WhisperAcoustic.from_wav_files([], model_size="tiny.en")
    second = WhisperAcoustic.from_wav_files([], model_size="tiny.en")
    first.warmup()
    second.warmup()
    assert first._model is second._model
    first.close()
    second.close()
