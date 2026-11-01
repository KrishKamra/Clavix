# Clavix Roadmap

The roadmap has four phases. Phase 1 is complete. In Phase 2 the four members work in parallel
against frozen interface contracts. Phase 3 wires the real modules together, and Phase 4
produces the evaluation results for the paper.

| Member   | Area                                        | Spec                                                    | Owns                                                       |
|----------|---------------------------------------------|---------------------------------------------------------|------------------------------------------------------------|
| Person 1 | Core, planner, execution, evaluation, integration | [specs/P1_CORE.md](specs/P1_CORE.md)              | `clavix/` except the files below; `configs/`; `docs/`      |
| Person 2 | Acoustic ingestion (VAD + Faster-Whisper)   | [specs/P2_ACOUSTIC.md](specs/P2_ACOUSTIC.md)            | `clavix/modules/acoustic_whisper.py`, `tests/test_acoustic_whisper.py`, `tests/fixtures/audio/`, `scripts/record_fixture.py` |
| Person 3 | Structural grounding (Windows UI Automation) | [specs/P3_GROUNDING.md](specs/P3_GROUNDING.md)         | `clavix/modules/grounding_uia.py`, `tests/test_grounding_uia.py` |
| Person 4 | Feedback (TTS) and safety HUD               | [specs/P4_FEEDBACK_HUD.md](specs/P4_FEEDBACK_HUD.md)    | `clavix/modules/feedback_tts.py`, `clavix/modules/hud.py`, `clavix/modules/hud_web/`, `tests/test_feedback_tts.py` |

The interface files `clavix/modules/acoustic.py`, `grounding.py` and `feedback.py` belong to
Person 1. They are **frozen contracts**; change them only through the process in
[CONTRIBUTING.md](../CONTRIBUTING.md#changing-a-contract).

---

## Phase 1: Foundational core (done)

Delivered:
- Schemas, planner (Ollama, Gemini, mock), state machine, verifier, dispatcher and native operations.
- Composition root (`clavix/runtime.py`) with config-driven module loading.
- Contract test suites (`clavix/testing/contracts.py`) and fakes (`clavix/testing/fakes.py`).
- A 10-task benchmark suite, the harness and metrics; ruff and strict mypy.

Baseline (`clavix-bench --planner reference`, Windows 11): Tier 1 4/4, Tier 2 3/3,
Tier 3 0/3. Tier 3 fails because grounding is still a stub.

## Phase 2: Module implementation (all members in parallel)

Each member works only in their own files, against the existing contracts. Nobody waits on
anyone else: every module is testable with the fakes and contract suites that already exist.

| ID    | Owner | Deliverable                                                       | Done when                                                       |
|-------|-------|-------------------------------------------------------------------|-----------------------------------------------------------------|
| P2-M1 | P2    | `WhisperAcoustic` with a WAV-file source                          | `AcousticContract` passes on WAV fixtures                       |
| P2-M2 | P2    | Live microphone source with energy-based endpointing              | Manual check: 10/10 short commands transcribed; `t_asr_ms` logged |
| P2-M3 | P2    | Audio fixtures for all 10 benchmark commands                      | `tests/fixtures/audio/<task_id>.wav` exists for every task       |
| P3-M1 | P3    | `UIAGrounding.resolve_element` and `list_window_titles`           | Done                                                            |
| P3-M2 | P3    | `invoke_element` (Invoke / ExpandCollapse / Toggle / click)       | Done                                                            |
| P4-M1 | P4    | `TTSFeedback` (non-blocking queue on a dedicated thread)          | `FeedbackContract` passes                                       |
| P4-M2 | P4    | Glass HUD per [design/HUD_DESIGN.md](design/HUD_DESIGN.md)        | HUD_DESIGN.md §7 checklist; Stop aborts a running task          |
| P4-M3 | P4    | Global double-Escape abort hotkey                                 | Manual check: double-Esc during a run aborts it                 |
| P1-1 | P1    | Planner accuracy metric and prompt hardening                      | Done: see the status below                                      |
| P1-2 | P1    | Benchmark repetitions and process cleanup                         | Done: `--repeat N`, `--cleanup` (verified on Tier 3)            |
| P1-3 | P1    | CI pipeline                                                       | ruff, mypy and pytest run on every PR (Windows and Ubuntu)      |
| P1-7 | P1    | HUD design system and `FeedbackInterface.on_event` progress events | Done: `docs/design/HUD_DESIGN.md`, reference UI, contract tests |

**Status (2026-11-01): P2 is done.**
- P2-M1 and P2-M3: 10 human-recorded fixtures; `base.en` WER 0.000 and `t_asr_ms` median
  386 / p95 438 ms on CPU (see `tests/fixtures/audio/README.md`).
- P2-M2: live sign-off with the WH-CH520 headset: 10 spoken commands in a row, all captured;
  9 exact and 1 word wrong ("node.txt"). Live `t_asr_ms` median 395 / p95 434 ms.

**Status (2026-11-01): P3 is done and integrated** (`UIAGrounding` is the default grounding module).
- P3-M1 / P3-M2: contract and desktop tests pass (Notepad File menu; Start opens and closes, checked by
  toggle state). `clavix-bench --planner reference`: **Tier 3 9/9** (`--repeat 3`), all tiers 10/10;
  every grounding step clicked the intended element ("File", "Start", "Search"), no coordinate fallback.
- Polished on a real desktop: bounded searches (a slow desktop window and a blocking `GetChildren()`
  had made a miss take minutes), exact matches beat partial ones (the taskbar's "File Explorer" had
  won over Notepad's "File"), and the pure tests now run on Linux CI.
- `--planner llm` (`llama3:8b`, `--repeat 3`): **30/30 across all tiers**. Two planner fixes got T3-02
  there: plans using `VISUAL_FALLBACK` without coordinates now fail validation, so the repair round
  switches them to `OS_GROUNDING`; and the prompt forbids guessed window titles (the model added
  `window_title_contains:Windows Start menu` to clicks). Only those 3 invented checks disappeared; all
  `file_exists`, `stdout_contains` and launched-app title checks remain.

**Status (2026-11-01): P1-1 to P1-4 are done** (`llama3:8b`, `--repeat 3 --cleanup`, 30 runs each;
Tier 3 needs P3's grounding):

| Prompt | Tier 1 | Tier 2 | Plan action / exact match |
|---|---|---|---|
| Phase 1 prompt | 12/12 | 6/9 | 90 % / 40 % |
| + 2 hardening examples | 9/12 | 9/9 | 90 % / 40 % |
| + workspace rule | 12/12 | 9/9 | 100 % / 40 % |
| + no benchmark commands in examples (current) | 12/12 | 9/9 | 90 % / 30 % |

- The hardening examples fixed the dropped confirmation and `detach` on `mkdir` (Tier 2 to 9/9), but
  the `mkdir` example made the model add `cd workspace` to T1-03; the workspace rule fixed that.
- Two Phase 1 few-shot examples were T1-01 and T3-01 verbatim. Without them, T3-02 is planned as
  `VISUAL_FALLBACK` without coordinates; recheck once grounding lands. A test now blocks leaks.
- `--audio --planner llm`: 10/10 transcripts exact, `t_asr_ms` recorded for every task. T1-02
  failed only in audio mode: the transcript's capitals and full stop ("Clavix Ready.") reached the
  `echo` command.

## Phase 3: Integration (Person 1 leads)

Order: **P4, then P3, then P2**. Feedback carries the least risk, and acoustic changes the
input path, so it goes last. In practice P2 and then P3 finished first and are integrated
(`WhisperAcoustic`, `UIAGrounding`); P4 integrates next.

1. Merge the member's PR once their contract suite and CI pass.
2. P1 switches the module's `class` in `configs/default_config.yaml`.
3. P1 runs `clavix-bench --planner reference` and compares the results against the Phase 1 baseline.
4. After P3 is integrated, Tier 3 must reach at least 2/3 (T3-02 "Start" and T3-01 "File").
5. After P2 is integrated, P1 adds the audio-in-the-loop benchmark mode (P1 spec, task P1-4).

## Phase 4: Evaluation and paper

| ID | Owner | Deliverable |
|----|-------|-------------|
| E1 | P1 | `clavix-bench --planner reference --repeat 5`: execution success rate and latency per tier |
| E2 | P1 | `clavix-bench --planner llm --repeat 5` for `llama3:8b` and `mistral:7b`: plan accuracy and end-to-end success |
| E3 | P1 + P2 | `clavix-bench --audio --planner llm`: full ASR to LLM to OS latency waterfall |
| E4 | P3 | Grounding ablation: Tier 3 with `OS_GROUNDING` against forced `VISUAL_FALLBACK` |
| E5 | all | Figures from `python -m clavix.evaluation.metrics ... --out` |

## Dependency graph

```text
P2-M1 --> P2-M3 --> P1-4 (audio bench) --> E3
P3-M1 --> P3-M2 --> Tier 3 >= 2/3 --> E4
P4-M1 --> P4-M2 --> P4-M3
P1-1 --> E2          P1-2 --> E1..E4
```
