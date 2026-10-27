# Clavix

Deterministic, closed-loop voice-to-action desktop automation. A spoken (or typed) command
is planned by an LLM into a strictly validated `ExecutionPlan`. Each step runs on the
highest-priority path that can do it, and its postcondition is checked before the next step
starts.

| Priority | Path              | Mechanism                                          |
|----------|-------------------|----------------------------------------------------|
| 1        | `DETERMINISTIC`   | Shell commands, URL/protocol handlers, screenshots |
| 2        | `OS_GROUNDING`    | Accessibility tree (UIA / AT-SPI) element lookup   |
| 3        | `VISUAL_FALLBACK` | PyAutoGUI coordinate clicks (off by default)       |

```text
IDLE -> ACOUSTIC_INGESTION -> PLANNING -> DISPATCHING <-> VERIFYING -> FEEDBACK -> COMPLETED | FAILED
```

## Documentation

| Document | Read it if you... |
|----------|-------------------|
| [docs/ROADMAP.md](docs/ROADMAP.md) | want to know who owns what, and the phase plan |
| [docs/specs/](docs/specs/) | are implementing a module: one spec per member (P1–P4) |
| [CONTRIBUTING.md](CONTRIBUTING.md) | are about to open a PR, or need a contract changed |
| [AGENTS.md](AGENTS.md) | are an AI coding assistant (or directing one) |
| [docs/MASTER_SPEC.md](docs/MASTER_SPEC.md) | want the original system design and its deviations |

## Setup

```powershell
uv sync                 # creates .venv (Python 3.12), installs runtime + dev deps
uv run pytest           # run the test suite
uv run ruff check . ; uv run mypy   # lint + strict type check
```

For the default planner, run a local Ollama model (`ollama pull llama3:8b`). To use Gemini
instead, set `planner.provider: gemini` in `configs/default_config.yaml` and export
`GEMINI_API_KEY`.

## Usage

```powershell
uv run clavix "open github"                 # one command
uv run clavix --states                      # interactive; prints state transitions
uv run clavix-bench --planner reference     # benchmark execution with reference plans
uv run clavix-bench --planner llm --tiers 1 2
uv run python -m clavix.evaluation.metrics benchmarks/results_<run>.csv --out benchmarks/summary
```

`--planner reference` replaces the LLM with each task's reference plan, so the run measures
execution and verification only. `--planner llm` plans from the raw command text. Results
are written to `benchmarks/results_<timestamp>.csv` and `.json`. Each run gets a fresh
scratch directory under `benchmarks/runs/<timestamp>/workspace`.

Running the benchmark has real side effects: it opens browser tabs, writes files to the run
workspace, takes screenshots and, on Windows, launches Notepad.

## Layout

```text
clavix/
  schemas/actions.py       ActionStep / ExecutionPlan / ExecutionResult + typed payloads
  schemas/telemetry.py     StepTelemetry, VerificationResult, TaskOutcome, BenchmarkRecord
  core/planner.py          Prompt templates, Ollama/Gemini/Mock clients, schema-repair loop
  core/state_machine.py    ClavixAgent: the async closed loop
  core/verifier.py         Postcondition assertions (polled for live OS state)
  execution/dispatcher.py  Path router, safety policy, abort + PyAutoGUI fail-safe
  execution/native_ops.py  launch_url, run_cli, spawn_process, take_screenshot
  modules/                 Contracts + stubs for teammates (see below)
  evaluation/harness.py    Benchmark runner (clavix-bench)
  evaluation/metrics.py    Success-rate and latency tables
  runtime.py               Composition root: builds components from config
  config.py                Typed config (configs/default_config.yaml)
  testing/fakes.py         In-memory fakes of every interface
  testing/contracts.py     Conformance suites each module implementation must pass
docs/                      Master spec, roadmap, per-member specs
configs/
  default_config.yaml      Runtime configuration
  benchmark_suite.json     10 tasks, tiers 1-3
```

## Plan format

```json
{
  "steps": [
    {"step_id": 1, "action_type": "DETERMINISTIC", "action_name": "launch_url",
     "payload": {"url": "https://github.com"},
     "expected_postcondition": "window_title_contains:GitHub"}
  ]
}
```

| `action_name`     | `action_type`                     | payload                                                  |
|-------------------|-----------------------------------|----------------------------------------------------------|
| `launch_url`      | DETERMINISTIC                     | `url`                                                    |
| `run_command`     | DETERMINISTIC                     | `cmd`, `timeout_sec=10`, `detach=false`                  |
| `take_screenshot` | DETERMINISTIC                     | `filepath`, `target_region=[left, top, width, height]?`  |
| `click_element`   | OS_GROUNDING / VISUAL_FALLBACK    | `target_label`, `fallback_coord=[x, y]?`                 |
| `speak_feedback`  | FEEDBACK                          | `message`, `blocking=false`                              |

Postconditions use the form `<kind>:<argument>`. The kinds are `window_title_contains`,
`process_running`, `file_exists`, `exit_code` and `stdout_contains`. The first three are
polled for up to `verifier.timeout_sec`. On Windows, window titles come from `pygetwindow`.
On other platforms they are reported as `UNSUPPORTED` until the AT-SPI module provides them.

The LLM only writes `steps`. Clavix assigns `plan_id` (UUID4) and `original_prompt`. If the
LLM returns an invalid plan, the validation error is sent back to it for up to
`planner.max_retries` repair attempts.

## Teammate integration points

| Module                  | Owner    | Implement                                                                       |
|-------------------------|----------|---------------------------------------------------------------------------------|
| `modules/acoustic.py`   | Person 2 | `AcousticInterface.listen() -> Transcript(text, t_asr_ms, confidence)`          |
| `modules/grounding.py`  | Person 3 | `GroundingInterface.resolve_element`, `invoke_element`, `click_coordinate`      |
| `modules/feedback.py`   | Person 4 | `FeedbackInterface.speak`, optional `on_state_change` for the HUD               |

Implementations are selected in `configs/default_config.yaml` under `modules.<name>.class`,
so plugging in a module never requires editing core code. Each implementation must pass its
suite in `clavix/testing/contracts.py`.

Current stand-ins are `ConsoleAcoustic`/`ScriptedAcoustic`, `StubGrounding` (never resolves
elements; coordinate clicks go through PyAutoGUI) and `ConsoleFeedback`. A HUD abort button
calls `FeedbackInterface.request_abort()`. Until grounding is integrated, Tier 3 benchmark
tasks are expected to fail. That failure is the baseline.

## Safety

- `execution.blocked_command_patterns` is a list of regexes. The dispatcher refuses any
  shell command that matches one of them.
- Visual fallback is disabled by default (`execution.enable_visual_fallback`).
- `pyautogui.FAILSAFE` is forced on before any coordinate input. Moving the mouse into a
  screen corner raises `AbortRequested`, which stops the task.
