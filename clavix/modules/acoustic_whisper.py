"""Faster-Whisper acoustic input with WAV and microphone sources.

Optional audio dependencies are imported only when the corresponding source or
model is first used, so importing this module is safe in a headless install.
"""

from __future__ import annotations

import asyncio
import logging
import math
import queue
import threading
import time
import wave
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
_TAIL_SAMPLES = 3_200  # trailing silence kept after an endpoint (200 ms)
_POLL_SEC = 0.1  # how often a blocked capture checks for abort or a dead stream

# Loaded models are shared by every WhisperAcoustic with the same settings, so
# per-task instances (the audio benchmark builds one per WAV) pay the load once.
_MODELS: dict[tuple[str, str, str], Any] = {}
_MODELS_LOCK = threading.Lock()


def _shared_model(model_size: str, device: str, compute_type: str) -> Any:
    """Return the cached ``WhisperModel`` for these settings, loading it on first use."""
    key = (model_size, device, compute_type)
    with _MODELS_LOCK:
        model = _MODELS.get(key)
        if model is None:
            from faster_whisper import WhisperModel

            started = time.perf_counter()
            model = WhisperModel(model_size, device=device, compute_type=compute_type)
            logger.info(
                "loaded Faster-Whisper %s (%s, %s) in %.0f ms",
                model_size,
                device,
                compute_type,
                (time.perf_counter() - started) * 1000.0,
            )
            _MODELS[key] = model
        return model


class AudioSource(ABC):
    """Blocking provider of a single 16 kHz mono utterance."""

    @abstractmethod
    def read_utterance(self) -> tuple[FloatAudio, float]:
        """Block until one utterance is captured.

        Returns:
            ``(audio, t_end)``: mono float32 audio at 16 kHz in ``[-1, 1]``, and
            the ``time.perf_counter()`` at which end of speech was detected.

        Raises:
            EOFError: When the source has no more utterances.
        """

    def abort(self) -> None:  # noqa: B027 - optional hook, a no-op by default
        """Ask a blocked :meth:`read_utterance` on another thread to give up."""


def _read_pcm16_wav(path: str) -> FloatAudio | None:
    """Decode a 16 kHz 16-bit PCM WAV with the stdlib, or return ``None`` for other formats."""
    import numpy as np

    try:
        with wave.open(path, "rb") as wav_file:
            if wav_file.getsampwidth() != 2 or wav_file.getframerate() != _SAMPLE_RATE:
                return None
            channels = wav_file.getnchannels()
            pcm = np.frombuffer(wav_file.readframes(wav_file.getnframes()), dtype="<i2")
    except wave.Error:
        return None
    samples = pcm.reshape(-1, channels).mean(axis=1) if channels > 1 else pcm
    return (samples / 32768.0).astype(np.float32)


class WavFileSource(AudioSource):
    """Read WAV paths in sequence.

    16 kHz 16-bit PCM files (the fixture format) are decoded with the stdlib.
    Anything else goes through ``faster_whisper.decode_audio``, which needs a
    PyAV release that still accepts its ``metadata_errors`` argument (PyAV < 19).
    """

    def __init__(self, paths: Sequence[str]) -> None:
        self._paths = iter(list(paths))

    def read_utterance(self) -> tuple[FloatAudio, float]:
        """Decode and return the next file.

        Raises:
            EOFError: When every file has been returned.
        """
        try:
            path = next(self._paths)
        except StopIteration:
            # The CLI's interactive loop stops on errors that start with "EOF".
            raise EOFError("EOF: no WAV files left") from None
        audio = _read_pcm16_wav(path)
        if audio is None:
            from faster_whisper import decode_audio

            audio = decode_audio(path, sampling_rate=_SAMPLE_RATE)
        return audio, time.perf_counter()


class Endpointer:
    """Energy-based end-of-utterance detector over 30 ms frames.

    Pure logic (no audio device), so it is unit-testable with synthetic frames.
    An instance captures exactly one utterance; create a new one for the next.
    """

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
        self._max_samples = max(_FRAME_SAMPLES, round(max_utterance_sec * _SAMPLE_RATE))
        self._pre_roll: deque[FloatAudio] = deque(maxlen=max(0, math.ceil(pre_roll_ms / _FRAME_MS)))
        self._onset: list[FloatAudio] = []
        self._frames: list[FloatAudio] = []
        self._samples = 0
        self._silent_frames = 0
        self._silent_samples = 0
        self._speaking = False
        self._complete = False

    @staticmethod
    def frame_rms(frame: FloatAudio) -> float:
        """Root mean square of one frame (0.0 for an empty frame)."""
        import numpy as np

        if frame.size == 0:
            return 0.0
        return float(np.sqrt(np.mean(np.square(frame, dtype=np.float64))))

    @staticmethod
    def calibrate(frames: Sequence[FloatAudio], energy_threshold: float) -> float:
        """Return ``max(energy_threshold, 3 × mean ambient frame RMS)``."""
        if not frames:
            return energy_threshold
        ambient = sum(Endpointer.frame_rms(frame) for frame in frames) / len(frames)
        return max(energy_threshold, 3.0 * ambient)

    @property
    def speaking(self) -> bool:
        """Whether an utterance onset has been accepted."""
        return self._speaking

    @property
    def complete(self) -> bool:
        """Whether the utterance has ended."""
        return self._complete

    @property
    def audio(self) -> FloatAudio:
        """Pre-roll plus utterance, with trailing silence cut to 200 ms."""
        import numpy as np

        if not self._frames:
            return np.empty(0, dtype=np.float32)
        audio = np.concatenate(self._frames).astype(np.float32, copy=False)
        excess = self._silent_samples - _TAIL_SAMPLES
        return audio[:-excess] if excess > 0 else audio

    def feed(self, frame: FloatAudio) -> bool:
        """Consume one frame; return ``True`` once the utterance is complete."""
        if self._complete:
            return True
        loud = self.frame_rms(frame) >= self.threshold
        frame = frame.copy()
        if not self._speaking:
            if not loud:
                # A burst too short to count as speech stays in the pre-roll, so
                # the ring holds contiguous audio.
                self._pre_roll.extend(self._onset)
                self._onset.clear()
                self._pre_roll.append(frame)
                return False
            self._onset.append(frame)
            if len(self._onset) < self._min_speech_frames:
                return False
            self._speaking = True
            self._frames = [*self._pre_roll, *self._onset]
            self._samples = sum(len(f) for f in self._frames)
            self._pre_roll.clear()
            self._onset = []
        else:
            self._frames.append(frame)
            self._samples += len(frame)
            if loud:
                self._silent_frames = self._silent_samples = 0
            else:
                self._silent_frames += 1
                self._silent_samples += len(frame)
        if self._silent_frames >= self._silence_frames or self._samples >= self._max_samples:
            self._complete = True
        return self._complete


class MicrophoneSource(AudioSource):
    """Capture one utterance per call from an input device, with energy endpointing.

    Ambient-noise calibration runs once, on the first stream, and its threshold
    is reused. That removes a ``calibrate_sec`` dead time before every later
    command, and the risk of calibrating on a user who starts talking at once.
    """

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
        self._calibrate_frames = max(0, math.ceil(calibrate_sec * 1000 / _FRAME_MS))
        self._threshold: float | None = None if self._calibrate_frames else energy_threshold
        self._abort = threading.Event()

    @property
    def threshold(self) -> float | None:
        """The speech threshold in use, or ``None`` before calibration."""
        return self._threshold

    def abort(self) -> None:
        """Make a blocked :meth:`read_utterance` raise ``InterruptedError``."""
        self._abort.set()

    def read_utterance(self) -> tuple[FloatAudio, float]:
        """Open the input stream, wait for one utterance, and close the stream.

        Raises:
            InterruptedError: If :meth:`abort` was called while waiting.
            RuntimeError: If the input stream stops delivering audio.
        """
        import sounddevice as sd

        self._abort.clear()
        frames: queue.Queue[FloatAudio] = queue.Queue()

        def callback(indata: FloatAudio, frame_count: int, time_info: Any, status: Any) -> None:
            # Runs on the PortAudio thread: copy the frame out and nothing else.
            if status:
                logger.warning("audio input status: %s", status)
            frames.put(indata[:, 0].copy())

        endpointer = Endpointer(
            energy_threshold=self._energy_threshold,
            silence_ms=self._silence_ms,
            min_speech_ms=self._min_speech_ms,
            pre_roll_ms=self._pre_roll_ms,
            max_utterance_sec=self._max_utterance_sec,
        )
        ambient: list[FloatAudio] = []
        with sd.InputStream(
            samplerate=_SAMPLE_RATE,
            blocksize=_FRAME_SAMPLES,
            device=self._input_device,
            channels=1,
            dtype="float32",
            callback=callback,
        ) as stream:
            if self._threshold is not None:
                endpointer.threshold = self._threshold
                logger.info("listening (threshold %.4f)", self._threshold)
            while True:
                if self._abort.is_set():
                    raise InterruptedError("microphone capture aborted")
                try:
                    frame = frames.get(timeout=_POLL_SEC)
                except queue.Empty:
                    if not stream.active:
                        raise RuntimeError("audio input stream stopped") from None
                    continue
                if self._threshold is None:
                    ambient.append(frame)
                    if len(ambient) >= self._calibrate_frames:
                        self._threshold = Endpointer.calibrate(ambient, self._energy_threshold)
                        endpointer.threshold = self._threshold
                        logger.info("calibrated; listening (threshold %.4f)", self._threshold)
                    continue
                if endpointer.feed(frame):
                    t_end = time.perf_counter()
                    return endpointer.audio, t_end


class WhisperAcoustic(AcousticInterface):
    """Transcribe microphone or WAV utterances with Faster-Whisper."""

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
        """Build an instance that reads ``paths`` in order instead of the microphone.

        Microphone options in ``options`` (e.g. ``silence_ms`` from the runtime
        config) are accepted and ignored, so the configured options can be reused.
        """
        return cls(source=WavFileSource(paths), **options)

    async def listen(self) -> Transcript:
        """Capture and transcribe one utterance without blocking the event loop.

        Raises:
            EOFError: When the audio source is exhausted.
            RuntimeError: If the instance is closed.
        """
        if self._closed:
            raise RuntimeError("WhisperAcoustic is closed")
        worker = self._worker()
        if self._model is None:
            # Load on the ASR thread while the user is still speaking.
            worker.submit(self._load_model)
        try:
            audio, t_end = await asyncio.to_thread(self._source.read_utterance)
        except asyncio.CancelledError:
            # The capture thread outlives the cancelled await (and would keep
            # asyncio.run from exiting on Ctrl+C); tell it to stop.
            self._source.abort()
            raise
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(worker, self._transcribe_timed, audio, t_end)

    def warmup(self) -> None:
        """Load the model and transcribe 0.5 s of silence, blocking."""
        import numpy as np

        silence = np.zeros(_SAMPLE_RATE // 2, dtype=np.float32)
        self._worker().submit(self._transcribe_timed, silence, time.perf_counter()).result()

    def close(self) -> None:
        """Stop any capture in progress and shut down the ASR thread."""
        self._closed = True
        self._source.abort()
        if self._executor is not None:
            self._executor.shutdown(wait=True)
            self._executor = None
        self._model = None

    def _worker(self) -> ThreadPoolExecutor:
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="clavix-asr")
        return self._executor

    def _load_model(self) -> Any:
        if self._model is None:
            self._model = _shared_model(self.model_size, self.device, self.compute_type)
        return self._model

    def _transcribe_timed(self, audio: FloatAudio, t_end: float) -> Transcript:
        model = self._load_model()
        if audio.size == 0:
            return Transcript(text="", t_asr_ms=(time.perf_counter() - t_end) * 1000.0)
        segments, info = model.transcribe(
            audio,
            language=self.language,
            beam_size=self.beam_size,
            vad_filter=True,
            condition_on_previous_text=False,
        )
        decoded = list(segments)  # decoding happens here: keep it inside the timed section
        text = " ".join(segment.text.strip() for segment in decoded).strip()
        t_asr_ms = max(0.0, (time.perf_counter() - t_end) * 1000.0)
        confidence = self._confidence(segment.avg_logprob for segment in decoded)
        logger.debug(
            "ASR %.1f ms: duration=%.2f s, after VAD=%.2f s, no_speech_prob=%s",
            t_asr_ms,
            info.duration,
            info.duration_after_vad,
            [round(segment.no_speech_prob, 3) for segment in decoded],
        )
        return Transcript(text=text, t_asr_ms=t_asr_ms, confidence=confidence)

    @staticmethod
    def _confidence(avg_logprobs: Iterable[float]) -> float | None:
        """Mean of ``exp(avg_logprob)`` over segments, clipped to [0, 1]; ``None`` if empty."""
        values = [math.exp(value) for value in avg_logprobs]
        if not values:
            return None
        return min(1.0, max(0.0, sum(values) / len(values)))
