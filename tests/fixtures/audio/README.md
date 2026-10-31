# Acoustic benchmark fixtures

One WAV per task in `configs/benchmark_suite.json` (`<task_id>.wav`): the task's `command`
spoken once, as 16 kHz mono 16-bit PCM, each file under 300 KB.

## Recording

| Field       | Value |
|-------------|-------|
| Speaker     | Speaker A (adult, English) |
| Device      | Sony WH-CH520 Bluetooth headset microphone (hands-free profile), `--device 2` on the dev laptop |
| Environment | Quiet room; a fan was running in the background |
| Recorded    | 2026-10-31, `uv run python scripts/record_fixture.py <task_id> --device 2` |
| Text        | Each task's `command`, verbatim, read naturally ("note dot txt") |

Re-record a clip with the same device and room so the set stays consistent:

```powershell
uv run python scripts/record_fixture.py T1-02 --device 2   # one task
uv run python scripts/record_fixture.py --all --device 2   # all ten, prompted in turn
```

Wait for the `listening` line before speaking, and say the command in one breath: a pause
of 0.7 s ends the clip. Opening the input stream takes about 0.7 s on Windows, and the first
stream also spends 0.5 s measuring room noise.

`--synthesize` renders the commands with a Windows SAPI voice instead. Use it only as a
placeholder: clean TTS is easier than real speech.

## Results (2026-10-31)

Intel Core Ultra 9 285H, CPU, `compute_type=int8`, `beam_size=1`, model warmed up. Five passes
over the 10 files (n = 50). WER is computed on casefolded words with punctuation removed.

| Model   | Mean WER | Max WER | `t_asr_ms` median | p95 | Cold first call (load + ASR) |
|---------|----------|---------|-------------------|-----|------------------------------|
| base.en | 0.000    | 0.00    | 386               | 438 | 1294 ms                      |
| tiny.en | 0.175    | 1.00    | 221               | 244 | 655 ms                       |

- base.en transcribes every command exactly, including "Clavix".
- tiny.en misses "github" ("get up", T1-01), "file" ("sign", T3-01) and "corner.png"
  ("cornered.png", T2-03). Use base.en for the benchmark.
- Latency varies between runs on this laptop (hybrid P/E cores, power state). Compare
  configurations within one run.
- The fixtures were recorded on the headset. The live app uses the system default input
  unless `input_device` is set in the acoustic module's options, so the live mic can differ.
- `t_asr_ms` excludes the 700 ms end-of-speech silence (`silence_ms`), which the paper reports
  separately as a configuration constant.
- `without_timestamps=True` and `cpu_threads` tuning gave no consistent gain on synthetic
  audio, so the spec's `transcribe` call is used unchanged.
- `WavFileSource` decodes 16 kHz 16-bit PCM with the stdlib. `faster_whisper.decode_audio`
  1.2.1 fails with PyAV 19, so the acoustic extra pins `av<19`; the stdlib path needs no PyAV, and
  `decode_audio` is only the fallback for other formats.
