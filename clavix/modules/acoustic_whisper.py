"""Faster-Whisper acoustic input with WAV and microphone sources.

Optional audio dependencies are imported only when the corresponding source or
model is first used, so importing this module is safe in a headless install.
"""

from __future__ import annotations

import asyncio
import logging
import math
import queue
import time
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any

from clavix.modules.acoustic import AcousticInterface, Transcript

if TYPE_CHECKING:
    import numpy as np
    from numpy.typing import NDArray

    FloatAudio = NDArray[np.float32]
else:
    FloatAudio = Any

logger = logging.getLogger(__name__)
_SAMPLE_RATE = 16_000
_FRAME_SAMPLES = 480
_FRAME_MS = 30


class AudioSource(ABC):
    """Blocking provider of a single 16 kHz mono utterance."""

    @abstractmethod
    def read_utterance(self) -> tuple[FloatAudio, float]:
        """Return audio and its end-of-speech ``perf_counter`` timestamp."""


class WavFileSource(AudioSource):
    """Read WAV paths in sequence using Faster-Whisper's decoder."""

    def __init__(self, paths: Sequence[str]) -> None:
        self._paths = iter(paths)

    def read_utterance(self) -> tuple[FloatAudio, float]:
        """Decode and return the next file, or raise ``EOFError`` when exhausted."""
        try:
            path = next(self._paths)
        except StopIteration:
            raise EOFError("no WAV files left") from None
        from faster_whisper import decode_audio

        audio = decode_audio(path, sampling_rate=_SAMPLE_RATE)
        return audio, time.perf_counter()


class Endpointer:
    """Energy based endpoint detector operating on 30 ms frames."""

    def __init__(
        self,
        *,
        energy_threshold: float = 0.01,
        silence_ms: int = 700,
        min_speech_ms: int = 90,
        pre_roll_ms: int = 300,
        max_utterance_sec: float = 15.0,
    ) -> None:
        self.threshold = energy_threshold
        self._silence_frames = max(1, math.ceil(silence_ms / _FRAME_MS))
        self._min_speech_frames = max(1, math.ceil(min_speech_ms / _FRAME_MS))
        self._pre_roll_frames = max(0, math.ceil(pre_roll_ms / _FRAME_MS))
        self._max_frames = max(1, math.ceil(max_utterance_sec * 1000 / _FRAME_MS))
        self._pre_roll: deque[FloatAudio] = deque(maxlen=self._pre_roll_frames or None)
        self._active: list[FloatAudio] = []
        self._speech_run: list[FloatAudio] = []
        self._silence_run = 0
        self._speaking = False
        self._complete = False

    @staticmethod
    def frame_rms(frame: Iterable[float]) -> float:
        """Calculate root mean square energy without requiring NumPy."""
        samples = [float(sample) for sample in frame]
        if not samples:
            return 0.0
        return math.sqrt(sum(sample**2 for sample in samples) / len(samples))

    @staticmethod
    def calibrate(frames: Sequence[FloatAudio], energy_threshold: float) -> float:
        """Return the configured threshold or three times measured ambient RMS."""
        if not frames:
            return energy_threshold
        ambient = sum(Endpointer.frame_rms(frame) for frame in frames) / len(frames)
        return max(energy_threshold, 3.0 * ambient)

    @property
    def audio(self) -> FloatAudio:
        """Captured samples, excluding silence beyond the 200 ms tail."""
        import numpy as np

        frames = self._active
        if self._complete:
            trim_frames = min(self._silence_run, max(0, self._silence_frames - math.ceil(200 / _FRAME_MS)))
            if trim_frames:
                frames = frames[:-trim_frames]
        if not frames:
            return np.empty(0, dtype=np.float32)
        return np.concatenate(frames).astype(np.float32, copy=False)

    @property
    def speaking(self) -> bool:
        """Whether the detector has accepted an utterance onset."""
        return self._speaking

    def feed(self, frame: FloatAudio) -> bool:
        """Consume a frame and return true once one utterance is complete."""
        if self._complete:
            return True
        level = self.frame_rms(frame)
        if not self._speaking:
            if level >= self.threshold:
                self._speech_run.append(frame.copy())
                if len(self._speech_run) >= self._min_speech_frames:
                    self._speaking = True
                    self._active = list(self._pre_roll) + self._speech_run
                    self._speech_run = []
            else:
                self._speech_run.clear()
                if self._pre_roll_frames:
                    self._pre_roll.append(frame.copy())
            return False

        self._active.append(frame.copy())
        if level < self.threshold:
            self._silence_run += 1
        else:
            self._silence_run = 0
        if self._silence_run >= self._silence_frames or len(self._active) >= self._max_frames:
            self._complete = True
            return True
        return False


class MicrophoneSource(AudioSource):
    """Capture one utterance from the default or selected input device."""

    def __init__(
        self,
        input_device: int | str | None = None,
        energy_threshold: float = 0.01,
        silence_ms: int = 700,
        min_speech_ms: int = 90,
        pre_roll_ms: int = 300,
        max_utterance_sec: float = 15.0,
        calibrate_sec: float = 0.5,
    ) -> None:
        self._input_device = input_device
        self._energy_threshold = energy_threshold
        self._silence_ms = silence_ms
        self._min_speech_ms = min_speech_ms
        self._pre_roll_ms = pre_roll_ms
        self._max_utterance_sec = max_utterance_sec
        self._calibrate_sec = calibrate_sec

    def read_utterance(self) -> tuple[FloatAudio, float]:
        """Block on a microphone stream until energy endpointing completes."""
        import numpy as np
        import sounddevice as sd

        # sounddevice callbacks execute on an audio thread; use a thread-safe queue.
        audio_frames: queue.Queue[FloatAudio] = queue.Queue()

        def audio_callback(indata: FloatAudio, count: int, time_info: Any, status: Any) -> None:
            del count, time_info
            if status:
                logger.warning("audio input status: %s", status)
            audio_frames.put(indata[:, 0].copy())

        detector = Endpointer(
            energy_threshold=self._energy_threshold,
            silence_ms=self._silence_ms,
            min_speech_ms=self._min_speech_ms,
            pre_roll_ms=self._pre_roll_ms,
            max_utterance_sec=self._max_utterance_sec,
        )
        calibration: list[FloatAudio] = []
        calibrating = self._calibrate_sec > 0
        calibration_count = math.ceil(self._calibrate_sec * 1000 / _FRAME_MS) if calibrating else 0
        with sd.InputStream(
            samplerate=_SAMPLE_RATE,
            channels=1,
            dtype="float32",
            blocksize=_FRAME_SAMPLES,
            device=self._input_device,
            callback=audio_callback,
        ):
            while True:
                frame = audio_frames.get()
                if calibrating:
                    calibration.append(frame)
                    if len(calibration) < calibration_count:
                        continue
                    detector.threshold = Endpointer.calibrate(calibration, self._energy_threshold)
                    calibrating = False
                    continue
                if detector.feed(frame):
                    t_end = time.perf_counter()
                    return detector.audio, t_end


class WhisperAcoustic(AcousticInterface):
    """Transcribe microphone or WAV utterances using Faster-Whisper."""

    def __init__(
        self,
        model_size: str = "base.en",
        device: str = "cpu",
        compute_type: str = "int8",
        language: str | None = "en",
        beam_size: int = 1,
        source: AudioSource | None = None,
        **mic_options: Any,
    ) -> None:
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        self.language = language
        self.beam_size = beam_size
        self._source = source if source is not None else MicrophoneSource(**mic_options)
        self._executor: ThreadPoolExecutor | None = None
        self._model: Any = None
        self._closed = False

    @classmethod
    def from_wav_files(cls, paths: Sequence[str], **options: Any) -> WhisperAcoustic:
        """Construct an instance backed by WAV files instead of a microphone."""
        return cls(source=WavFileSource(paths), **options)

    async def listen(self) -> Transcript:
        """Capture and transcribe one utterance without blocking the event loop."""
        if self._closed:
            raise RuntimeError("WhisperAcoustic is closed")
        audio, t_end = await asyncio.to_thread(self._source.read_utterance)
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._worker(), self._transcribe_timed, audio, t_end)

    def _worker(self) -> ThreadPoolExecutor:
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="clavix-asr")
        return self._executor

    def _load_model(self) -> Any:
        if self._model is None:
            from faster_whisper import WhisperModel

            self._model = WhisperModel(
                self.model_size, device=self.device, compute_type=self.compute_type
            )
        return self._model

    def _transcribe_timed(self, audio: FloatAudio, t_end: float) -> Transcript:
        model = self._load_model()
        segments, info = model.transcribe(
            audio,
            language=self.language,
            beam_size=self.beam_size,
            vad_filter=True,
            condition_on_previous_text=False,
        )
        decoded = list(segments)
        text = " ".join(segment.text.strip() for segment in decoded).strip()
        confidence = self._confidence(segment.avg_logprob for segment in decoded)
        t_asr_ms = (time.perf_counter() - t_end) * 1000.0
        logger.debug(
            "ASR %.1f ms; audio duration=%s, after VAD=%s, no_speech_prob=%s",
            t_asr_ms,
            getattr(info, "duration", None),
            getattr(info, "duration_after_vad", None),
            [getattr(segment, "no_speech_prob", None) for segment in decoded],
        )
        return Transcript(text=text, t_asr_ms=max(0.0, t_asr_ms), confidence=confidence)

    @staticmethod
    def _confidence(avg_logprobs: Sequence[float] | Any) -> float | None:
        """Convert mean segment log probability to a clipped confidence score."""
        values = list(avg_logprobs)
        if not values:
            return None
        return min(1.0, max(0.0, sum(math.exp(float(value)) for value in values) / len(values)))

    def warmup(self) -> None:
        """Load the model and transcribe half a second of silence."""
        import numpy as np

        self._worker().submit(
            self._transcribe_timed,
            np.zeros(8_000, dtype=np.float32),
            time.perf_counter(),
        ).result()

    def close(self) -> None:
        """Shut down the transcription worker and release its model reference."""
        self._closed = True
        if self._executor is not None:
            self._executor.shutdown(wait=True)
            self._executor = None
        self._model = None
