"""Unit and model-backed tests for acoustic ingestion."""

from __future__ import annotations

import json
import math
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from clavix.testing.contracts import AcousticContract

FIXTURE_DIR = Path("tests/fixtures/audio")
FIXTURES = sorted(FIXTURE_DIR.glob("*.wav"))


class TestEndpointer:
    def test_detects_speech_with_preroll_and_trimmed_trailing_silence(self) -> None:
        from clavix.modules.acoustic_whisper import Endpointer

        detector = Endpointer(silence_ms=300, min_speech_ms=90, pre_roll_ms=60)
        silence = np.zeros(480, dtype=np.float32)
        sine = (0.1 * np.sin(2 * np.pi * 440 * np.arange(480) / 16_000)).astype(np.float32)
        assert not detector.feed(silence)
        assert not detector.feed(silence)
        completed = False
        for _ in range(34):
            completed = detector.feed(sine)
        assert not completed
        for _ in range(10):
            completed = detector.feed(silence)
            if completed:
                break
        assert completed
        audio = detector.audio
        assert len(audio) > 16_000  # one second of speech plus pre-roll
        assert len(audio) < 21_000  # the full silence hang is not returned
        assert np.max(np.abs(audio[:960])) == 0.0

    def test_ignores_speech_shorter_than_minimum(self) -> None:
        from clavix.modules.acoustic_whisper import Endpointer

        detector = Endpointer(min_speech_ms=90, pre_roll_ms=0)
        quiet = np.zeros(480, dtype=np.float32)
        loud = np.full(480, 0.1, dtype=np.float32)
        for _ in range(2):
            assert not detector.feed(loud)
        for _ in range(10):
            assert not detector.feed(quiet)
        assert not detector.speaking
        assert detector.audio.size == 0

    def test_maximum_duration_completes_utterance(self) -> None:
        from clavix.modules.acoustic_whisper import Endpointer

        detector = Endpointer(min_speech_ms=30, max_utterance_sec=0.09, pre_roll_ms=0)
        loud = np.full(480, 0.1, dtype=np.float32)
        assert not detector.feed(loud)
        assert not detector.feed(loud)
        assert detector.feed(loud)

    def test_calibration_raises_threshold_under_noise(self) -> None:
        from clavix.modules.acoustic_whisper import Endpointer

        noise = np.full(480, 0.02, dtype=np.float32)
        assert Endpointer.calibrate([noise, noise], 0.01) == pytest.approx(0.06)


def test_confidence_is_mean_exponentiated_logprob_and_clipped() -> None:
    from clavix.modules.acoustic_whisper import WhisperAcoustic

    assert WhisperAcoustic._confidence([-1.0, -2.0]) == pytest.approx(
        (math.exp(-1.0) + math.exp(-2.0)) / 2
    )
    assert WhisperAcoustic._confidence([0.1]) == 1.0
    assert WhisperAcoustic._confidence([]) is None


def test_wav_source_raises_eof_when_exhausted() -> None:
    from clavix.modules.acoustic_whisper import WavFileSource

    with pytest.raises(EOFError):
        WavFileSource([]).read_utterance()


def test_constructing_acoustic_does_not_import_faster_whisper() -> None:
    code = (
        "import sys; from clavix.modules.acoustic_whisper import WhisperAcoustic; "
        "WhisperAcoustic(); assert 'faster_whisper' not in sys.modules"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


@pytest.mark.slow
class TestWhisperAcousticContract(AcousticContract):
    listen_calls = 2

    @pytest.fixture
    def impl(self):
        from clavix.modules.acoustic_whisper import WhisperAcoustic

        acoustic = WhisperAcoustic.from_wav_files(
            [str(path) for path in FIXTURES[:2]], model_size="tiny.en"
        )
        yield acoustic
        acoustic.close()


@pytest.mark.slow
def test_fixture_transcription_wer_at_most_025() -> None:
    from clavix.modules.acoustic_whisper import WhisperAcoustic

    suite = json.loads(Path("configs/benchmark_suite.json").read_text(encoding="utf-8"))
    commands = {task["task_id"]: task["command"] for task in suite["tasks"]}

    def word_error_rate(reference: str, hypothesis: str) -> float:
        expected = reference.lower().split()
        actual = hypothesis.lower().split()
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

    assert len(FIXTURES) == 10
    acoustic = WhisperAcoustic.from_wav_files(
        [str(path) for path in FIXTURES], model_size="base.en"
    )
    try:
        for path in FIXTURES:
            transcript = pytest.importorskip("asyncio").run(acoustic.listen())
            assert word_error_rate(commands[path.stem], transcript.text) <= 0.25
    finally:
        acoustic.close()
