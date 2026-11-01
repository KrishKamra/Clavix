# Changelog

All notable changes to Clavix are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[semantic versioning](https://semver.org/).

## [0.1.0] - 2026-11-01

The first complete release: all four modules are integrated and the agent runs end to end from
speech to verified action.

### Added

- **Core (P1).**
  - The `ClavixAgent` closed-loop state machine.
  - A schema-validated LLM planner (Ollama, with repair rounds).
  - A deterministic-first dispatcher.
  - An operating-system state verifier.
  - Config-driven composition in `clavix/runtime.py`.
- **Acoustic ingestion (P2).**
  - Energy endpointing with Faster-Whisper `base.en` on the CPU (`WhisperAcoustic`).
  - Microphone and WAV sources.
  - Ten human-recorded benchmark fixtures.
- **Structural grounding (P3).**
  - Windows UI Automation search and invocation (`UIAGrounding`) on a dedicated worker thread.
  - Bounded searches: a per-window budget, exact matches before partial ones, and window listing
    through Win32.
- **Feedback and safety HUD (P4).**
  - Queued speech on a dedicated thread (`TTSFeedback`).
  - A glass Acrylic HUD in a separate WebView2 process that never takes focus and is excluded from
    screen capture.
  - A Stop button and a double-<kbd>Esc</kbd> abort.
- **Evaluation.**
  - `clavix-bench` with `--repeat`, `--cleanup`, `--audio` and `--live-feedback`.
  - Plan-accuracy and transcript-WER metrics.
  - A 10-task, 3-tier benchmark suite.
- **Engineering.**
  - Conformance contract suites for every module interface.
  - CI on Windows and Ubuntu (ruff, strict mypy, pytest).

### Results

- 50/50 runs with reference plans.
- 30/30 with `llama3:8b` plans.
- 44/50 from recorded speech, with 50/50 transcripts exact.

See [docs/RESULTS.md](docs/RESULTS.md) for the full numbers.

[0.1.0]: https://github.com/KrishKamra/Clavix/releases/tag/v0.1.0
