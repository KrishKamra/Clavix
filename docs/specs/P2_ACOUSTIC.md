# P2 Spec: Acoustic Ingestion (Endpointing + Faster-Whisper)

**Owner:** Person 2  **Phase:** 2 (milestones P2-M1, P2-M2, P2-M3)  **Platform:** Windows 11 first

## 1. Mission

Implement `WhisperAcoustic`, an `AcousticInterface` that captures one spoken command
(from the microphone or from WAV files), transcribes it with Faster-Whisper, and reports the
ASR latency. This is the first segment of the paper's latency waterfall (ASR, then LLM,
then OS execution).

## 2. Read first, in this order

1. `AGENTS.md`.
2. `clavix/modules/acoustic.py`: **your contract**. Do not edit it.
3. `clavix/core/state_machine.py`, methods `run` and `_ingest`: how `listen()` is called and
   how `t_asr_ms` is recorded.
4. `clavix/testing/contracts.py`, class `AcousticContract`.

## 3. Ownership

| Action            | Paths |
|-------------------|-------|
| Create and own    | `clavix/modules/acoustic_whisper.py`, `tests/test_acoustic_whisper.py`, `tests/fixtures/audio/` (WAV files plus `README.md`), `scripts/record_fixture.py` |
| Never edit        | `clavix/modules/acoustic.py`, any other file under `clavix/` and `configs/` |

## 4. The contract (verbatim from `clavix/modules/acoustic.py`)

```python
@dataclass(frozen=True)
class Transcript:
    text: str                          # final text, stripped
    t_asr_ms: float = 0.0              # end-of-speech -> transcript ready, in milliseconds
    confidence: Optional[float] = None  # in [0, 1]

class AcousticInterface(ABC):
    @abstractmethod
    async def listen(self) -> Transcript: ...   # blocks until one utterance is transcribed
```

- `listen()` is called once per task. Each call must capture **one** utterance.
- An empty `text` is allowed (silence or noise); the agent turns it into a failed task.
- By default, `listen()` waits indefinitely for speech to start.

## 5. Dependencies

Already declared as the `acoustic` extra: `faster-whisper>=1.0.0` (lockfile: 1.2.1),
`sounddevice>=0.4.6` (0.5.6) and `numpy>=1.26.0`.

```powershell
uv sync --extra acoustic
```

The first use of a model downloads it from the Hugging Face Hub into the user cache (about
140 MB for `base.en`). Tests that need it are marked `slow`.

## 6. Public API of `clavix/modules/acoustic_whisper.py`

```python
class AudioSource(ABC):
    @abstractmethod
    def read_utterance(self) -> tuple[np.ndarray, float]:
        """Block until one utterance is captured.

        Returns (audio, t_end): mono float32 audio at 16 kHz in [-1, 1], and the
        time.perf_counter() at which end of speech was detected.
        """

class WavFileSource(AudioSource):
    def __init__(self, paths: Sequence[str]) -> None: ...
    # Each call returns the next file, decoded with faster_whisper.decode_audio(path, sampling_rate=16000).
    # t_end = time.perf_counter() just after decoding. Raises EOFError when exhausted.

class MicrophoneSource(AudioSource):
    def __init__(
        self,
        input_device: int | str | None = None,  # sounddevice device; None = system default
        energy_threshold: float = 0.01,         # frame RMS that counts as speech
        silence_ms: int = 700,                  # trailing silence that ends an utterance
        min_speech_ms: int = 90,                # speech needed to start an utterance
        pre_roll_ms: int = 300,                 # audio kept from before the onset
        max_utterance_sec: float = 15.0,        # hard cut-off
        calibrate_sec: float = 0.5,             # ambient-noise calibration at stream start (0 = off)
    ) -> None: ...

class WhisperAcoustic(AcousticInterface):
    def __init__(
        self,
        model_size: str = "base.en",      # any faster_whisper.available_models() name
        device: str = "cpu",              # "cpu" | "cuda" | "auto"
        compute_type: str = "int8",       # int8 on CPU; float16 on CUDA
        language: str | None = "en",
        beam_size: int = 1,               # 1 = greedy, lowest latency
        source: AudioSource | None = None,  # None -> MicrophoneSource(**mic options)
        **mic_options: Any,               # forwarded to MicrophoneSource
    ) -> None: ...

    @classmethod
    def from_wav_files(cls, paths: Sequence[str], **options: Any) -> "WhisperAcoustic":
        """Used by the benchmark harness (P1-4) and the tests."""

    def warmup(self) -> None:
        """Load the model and transcribe 0.5 s of silence, blocking."""

    def close(self) -> None: ...
```

Constructors must be cheap. Don't load the model, open the microphone or start threads in
`__init__`; do it on the first `listen()` or `warmup()`.

Config entry at integration:

```yaml
modules:
  acoustic:
    class: clavix.modules.acoustic_whisper:WhisperAcoustic
    options: {model_size: base.en, device: cpu, compute_type: int8, beam_size: 1, silence_ms: 700}
```

## 7. Required behaviour

### 7.1 `listen()`

1. `audio, t_end = await asyncio.to_thread(self._source.read_utterance)`.
2. Transcribe on a **single dedicated worker thread**
   (`ThreadPoolExecutor(max_workers=1)`, created lazily). Load the `WhisperModel` there on first
   use.
3. Call:
   ```python
   segments, info = model.transcribe(
       audio, language=self.language, beam_size=self.beam_size,
       vad_filter=True, condition_on_previous_text=False,
   )
   segments = list(segments)
   ```
   **`segments` is a lazy generator.** Decoding only happens when you iterate it, so the
   `list()` must run inside the timed, off-loop section.
4. `text = " ".join(s.text.strip() for s in segments).strip()`.
5. `confidence = mean(exp(s.avg_logprob) for s in segments)`, clipped to [0, 1], or `None` if
   there are no segments.
6. `t_asr_ms = (time.perf_counter() - t_end) * 1000`, measured after step 4.
7. Return `Transcript(text, t_asr_ms, confidence)`. Log at DEBUG: the duration, `info.duration`,
   `info.duration_after_vad` and the per-segment `no_speech_prob`.
8. When the source raises `EOFError`, let it propagate. The agent reports it, and the CLI's
   interactive loop exits on it.

### 7.2 `MicrophoneSource.read_utterance()`: energy endpointing

- Open `sounddevice.InputStream(samplerate=16000, channels=1, dtype="float32", blocksize=480,
  device=input_device, callback=cb)` for each utterance, and close it when the utterance ends.
  480 samples is a 30 ms frame.
- The callback `cb(indata, frames, time_info, status)` only does
  `queue.put(indata[:, 0].copy())`. Never do work in the callback. Log `status` if it is set.
- **Calibration:** if `calibrate_sec > 0`, measure the mean frame RMS over the first
  `calibrate_sec` and set `threshold = max(energy_threshold, 3 × ambient_rms)`.
- The state machine runs over frames (frame RMS = `sqrt(mean(x**2))`):
  - **WAITING:** keep a ring buffer of the last `pre_roll_ms` of audio. Once
    `min_speech_ms / 30` consecutive frames are at or above the threshold, switch to SPEAKING,
    starting with the pre-roll.
  - **SPEAKING:** append every frame. Once `silence_ms / 30` consecutive frames are below
    the threshold, or the total length reaches `max_utterance_sec`, stop. Set
    `t_end = time.perf_counter()` and return the audio, with the trailing silence trimmed to
    200 ms.
- Implement the frame state machine as a pure class (`Endpointer.feed(frame) -> bool`), so it
  can be unit-tested without a microphone.

`t_asr_ms` therefore excludes the `silence_ms` hang time. That is intentional: the paper
reports the hang separately as a configuration constant.

### 7.3 Verified reference facts (faster-whisper 1.2.1, sounddevice 0.5.6)

| Fact | Value |
|------|-------|
| `WhisperModel.__init__` | `(model_size_or_path, device="auto", device_index=0, compute_type="default", cpu_threads=0, num_workers=1, download_root=None, local_files_only=False, ...)` |
| `transcribe` accepts | `audio` (path or float32 ndarray), `language`, `beam_size`, `vad_filter`, `vad_parameters`, `condition_on_previous_text`, `initial_prompt`, `word_timestamps`, ... |
| `Segment` fields | `id, seek, start, end, text, tokens, avg_logprob, compression_ratio, no_speech_prob, words, temperature` |
| `TranscriptionInfo` fields | `language, language_probability, duration, duration_after_vad, all_language_probs, transcription_options, vad_options` |
| `decode_audio` | `(input_file, sampling_rate=16000, split_stereo=False)` |
| `available_models()` | `tiny.en, tiny, base.en, base, small.en, small, medium.en, medium, large-v1, large-v2, large-v3, large, ...` |
| `sounddevice.InputStream` | `(samplerate, blocksize, device, channels, dtype, latency, ..., callback, finished_callback, ...)` |
| Dev machine input | "Microphone Array (Intel® Smart Sound...)" is the default input device |

Before using any other API, check it in the installed version with `inspect`.

## 8. Audio fixtures (P2-M3)

- `tests/fixtures/audio/<task_id>.wav`, one per task in `configs/benchmark_suite.json` (10 files).
  Each file is the task's `command` text spoken naturally: 16 kHz, mono, 16-bit PCM, with
  about 0.3 s of silence on each side.
- `tests/fixtures/audio/README.md`: the recording device, the speaker (anonymized, e.g.
  "speaker A") and the environment (quiet room or office noise).
- `scripts/record_fixture.py <task_id>`: reads the command from the suite, prints it, records
  with `MicrophoneSource`, and writes the WAV file with the stdlib `wave` module.
- Keep each file under 300 KB. Commit them; they are the ASR benchmark inputs.

## 9. Tests (`tests/test_acoustic_whisper.py`)

```python
import pytest
from pathlib import Path
from clavix.testing.contracts import AcousticContract

FIXTURES = sorted(Path("tests/fixtures/audio").glob("*.wav"))

@pytest.mark.slow
class TestWhisperAcousticContract(AcousticContract):
    listen_calls = 2

    @pytest.fixture
    def impl(self):
        from clavix.modules.acoustic_whisper import WhisperAcoustic
        a = WhisperAcoustic.from_wav_files([str(p) for p in FIXTURES[:2]], model_size="tiny.en")
        yield a
        a.close()
```

Also write:
- **Unmarked unit tests (no model, no microphone):**
  - `Endpointer` on synthetic frames: silence, then a 0.1-amplitude sine for 1 s, then silence,
    gives exactly one utterance, with the pre-roll included and the trailing silence trimmed.
  - Speech shorter than `min_speech_ms` is ignored.
  - The `max_utterance_sec` cut-off works.
  - Calibration raises the threshold under noise.
  - The confidence formula is correct and clipped.
  - `WavFileSource` raises `EOFError` when exhausted.
  - Constructing `WhisperAcoustic()` does not import `faster_whisper`; check `sys.modules` in a
    subprocess.
- **`slow` accuracy test:** for every fixture, the transcript's word error rate against the
  task command is at most 0.25 with `base.en`. Implement WER locally in the test (word-level
  Levenshtein); P1 adds a shared one later.

## 10. Definition of done

- [ ] `uv run pytest tests/test_acoustic_whisper.py` passes, including the `slow` tests.
- [ ] `uv run pytest -m "not slow"` passes on Linux without audio hardware.
- [ ] `ruff`, `ruff format --check` and `mypy` are clean.
- [ ] Manual check: with your class configured, `uv run clavix` handles 10 spoken commands in a
      row (interactive loop). Record the median and p95 `t_asr_ms` for `base.en` on CPU in the PR.
- [ ] All 10 fixtures are committed.
- [ ] No file outside §3 is modified.

## 11. Out of scope (Phase 2)

- Wake words, push-to-talk hotkeys, streaming partial transcripts.
- Neural VAD for endpointing. Faster-Whisper's built-in Silero VAD (`vad_filter=True`)
  already cleans up the captured utterance.
- GPU setup instructions beyond the `device` and `compute_type` options.
