# Clavix Master Specification

> **Status:** Phase 1 (foundational core) is implemented. This document records the
> original system intent. For work assignments, read the team specs instead:
> [ROADMAP](ROADMAP.md) and [specs/](specs/). Where the code differs from this document,
> the code and the team specs are authoritative. Section 7 lists the differences.

**Target Platform:** Windows 11 (26H2) / Cross-platform POSIX compatibility  
**Environment:** Python 3.11+, uv package manager  
**Role:** Lead Architect & Core Systems Integrator (Person 1)

---

## 1. PROJECT OVERVIEW & ARCHITECTURAL INTENT
Clavix is a deterministic, closed-loop multimodal voice-to-action desktop automation engine.
Unlike brittle visual-only OS agents that predict raw pixel coordinates, Clavix uses a **hierarchical dual-path execution model**:
1. **Deterministic Path (Priority 1):** Native OS subprocesses, shell commands, and default protocol handlers (`start`/`open`, Chrome DevTools Protocol).
2. **Structural Grounding Path (Priority 2):** Accessibility Tree querying (UI Automation / AT-SPI) to resolve interactive elements by semantic identity and bounding rects.
3. **Visual/Coordinate Fallback (Priority 3):** Strict coordinate-based mouse/keyboard interaction via PyAutoGUI only when prior paths fail.

Every execution step passes through an explicit **State Verifier Loop** with telemetry logging for academic benchmarking (Task Success Rate, Latency Waterfall: ASR -> LLM -> OS Execution).

---

## 2. REPOSITORY & PACKAGE ARCHITECTURE
Initialize the repository using `uv` with the following clean, modular structure:

```text
clavix/
├── pyproject.toml
├── README.md
├── configs/
│   └── default_config.yaml
├── clavix/
│   ├── __init__.py
│   ├── schemas/
│   │   ├── __init__.py
│   │   ├── actions.py         # Pydantic v2 data models for tool schemas & action steps
│   │   └── telemetry.py       # Benchmark event logs, latency metrics, verification states
│   ├── core/
│   │   ├── __init__.py
│   │   ├── state_machine.py   # Event-driven agent loop: Ingest -> Plan -> Execute -> Verify
│   │   ├── planner.py         # Local Ollama / Gemini API client with strict JSON schema enforcement
│   │   └── verifier.py        # Post-execution state assertion engine (window titles, process status)
│   ├── modules/               # External sub-module contracts (stubs for teammate integration)
│   │   ├── __init__.py
│   │   ├── acoustic.py        # Interface stub for Person 2 (Faster-Whisper / VAD)
│   │   ├── grounding.py       # Interface stub for Person 3 (Accessibility Tree & UI Automation)
│   │   └── feedback.py        # Interface stub for Person 4 (TTS & Safety HUD)
│   ├── execution/
│   │   ├── __init__.py
│   │   ├── dispatcher.py      # Dual-path router (Deterministic Subprocess vs Grounding)
│   │   └── native_ops.py      # Concrete subprocess, URL, and system command handlers
│   └── evaluation/
│       ├── __init__.py
│       ├── harness.py         # Automated batch benchmark suite runner
│       └── metrics.py         # CSV/Pandas metric aggregator for paper figures
└── tests/
    ├── test_schemas.py
    ├── test_state_machine.py
    └── test_benchmarks.py
```

---

## 3. STRICT DATA CONTRACTS (Pydantic v2)

### `clavix/schemas/actions.py`

**`ActionType`**: Enum `[DETERMINISTIC, OS_GROUNDING, VISUAL_FALLBACK, FEEDBACK]`

**`ActionPayload`**: Discriminated union or flexible model supporting:

| Action             | Parameters                                                                 |
|--------------------|----------------------------------------------------------------------------|
| `launch_url`       | `url: HttpUrl`                                                             |
| `run_command`      | `cmd: str`, `timeout_sec: int = 10`                                        |
| `click_element`    | `target_label: str`, `fallback_coord: Optional[tuple[int, int]] = None`    |
| `take_screenshot`  | `filepath: str`, `target_region: Optional[tuple[int, int, int, int]] = None` |
| `speak_feedback`   | `message: str`, `blocking: bool = False`                                   |

**`ActionStep`**:
- `step_id: int`
- `action_type: ActionType`
- `action_name: str`
- `payload: Dict[str, Any]`
- `expected_postcondition: Optional[str]` (e.g., `"window_title_contains:GitHub"`)

**`ExecutionPlan`**:
- `plan_id: str` (UUID4)
- `original_prompt: str`
- `steps: list[ActionStep]`

### `clavix/schemas/telemetry.py`

**`StepTelemetry`**:
- `step_id: int`
- `t_dispatch_start: float`
- `t_execution_end: float`
- `t_verification_end: float`
- `success: bool`
- `error_message: Optional[str]`

**`BenchmarkRecord`**:
- `task_id: str`
- `tier: int` (1: Deterministic, 2: Sequential, 3: UI-Grounding)
- `t_asr_ms: float`
- `t_llm_planning_ms: float`
- `t_total_execution_ms: float`
- `task_success: bool`
- `recovery_attempts: int`

---

## 4. CORE ENGINE & STATE MACHINE SPECIFICATION

### `clavix/core/state_machine.py`
Design an asynchronous state machine:

```text
IDLE -> ACOUSTIC_INGESTION -> PLANNING -> DISPATCHING -> VERIFYING -> FEEDBACK -> COMPLETED/FAILED
```

### `clavix/core/planner.py`
The planner must:
- Connect to local Ollama (`llama3:8b` / `mistral:7b`) via `/api/chat` or Gemini API.
- Enforce output compliance using Pydantic JSON Schema mode (`response_format={"type": "json_object"}`).
- Include zero-shot / few-shot prompt templates converting natural speech commands into valid `ExecutionPlan` structures.

### `clavix/execution/dispatcher.py`
The dispatcher:
- Routes `DETERMINISTIC` actions directly to `clavix/execution/native_ops.py` (using non-blocking `subprocess.Popen` or native OS URL handlers).
- Routes `OS_GROUNDING` and `VISUAL_FALLBACK` to `clavix/modules/grounding.py`.
- Ensures a fail-safe abort mechanism (`pyautogui.FAILSAFE = True` or global Escape hook).

---

## 5. BENCHMARKING HARNESS SPECIFICATION

### `clavix/evaluation/harness.py`
- Provides an automated test runner that iterates over a test suite file (`configs/benchmark_suite.json`) containing 10 foundational tasks across Tiers 1-3.
- Executes each task sequentially, timing every phase via high-resolution monotonic clocks (`time.perf_counter()`).
- Programmatically asserts ground-truth postconditions (e.g., checks if a URL process spawned, output files were written, or test screenshots exist).
- Serializes telemetry results directly into `benchmarks/results_{timestamp}.csv` and `.json` to provide immediate datasets for academic tables.

---

## 6. PHASE 1 IMPLEMENTATION TASKS
Please execute the following setup immediately:

1. Initialize the `pyproject.toml` with uv containing dependencies:
   - `pydantic>=2.7.0`
   - `pyautogui>=0.9.54`
   - `requests>=2.31.0`
   - `pyyaml>=6.0.1`
   - `pandas>=2.2.0`
   - `pillow>=10.0.0`
   - `pytest>=8.0.0`
2. Create the complete directory structure and all `__init__.py` files.
3. Implement `clavix/schemas/actions.py` and `clavix/schemas/telemetry.py`.
4. Implement `clavix/execution/native_ops.py` with standard OS handlers (`launch_url`, `run_cli`, `take_screenshot`).
5. Implement `clavix/core/planner.py` with a mockable LLM client and Pydantic validation.
6. Implement `clavix/core/state_machine.py` executing a full loop from raw mock command to verified execution.
7. Write unit tests in `tests/` verifying the schema parsing, dispatch execution, and mock benchmark logging.

Maintain strict typing, explicit docstrings, and modular decoupling throughout.

---

## 7. IMPLEMENTATION NOTES & DEVIATIONS (Phase 1)

| Area | Original spec | As implemented | Reason |
|------|---------------|----------------|--------|
| Dependencies | `pytest` listed as a runtime dependency | `pytest`, `ruff`, `mypy` and type stubs are in the `dev` dependency group. Teammate libraries are optional extras (`acoustic`, `grounding`, `feedback`) | Runtime installs stay lean; `uv sync` still installs dev tools |
| Python | 3.11+ | `requires-python >= 3.11`; `.python-version` pins 3.12 for the team | Everyone tests on one interpreter |
| `ActionStep.action_name` | `str` | `ActionName` (a `StrEnum`, so it still serializes as a string) | Unknown actions are rejected at parse time |
| `ActionStep.payload` | `Dict[str, Any]` | Still a dict, but validated against a typed model per action and normalized | A plan that parses is guaranteed to be dispatchable |
| `run_command` payload | `cmd`, `timeout_sec` | Adds `detach: bool = False` | Needed to launch GUI apps without blocking |
| `take_screenshot.target_region` | 4-tuple | `(left, top, width, height)`, the PyAutoGUI convention | Removes ambiguity |
| Postconditions | example `window_title_contains:GitHub` | Closed grammar: `window_title_contains`, `process_running`, `file_exists`, `exit_code`, `stdout_contains` | Validated at plan time |
| Ollama JSON mode | `response_format={"type": "json_object"}` | Native `/api/chat` with `format=<PlanDraft JSON schema>`, plus a Pydantic repair loop | Ollama's schema mode is stricter than json_object |
| LLM output | full `ExecutionPlan` | LLM writes only `steps`; Clavix assigns `plan_id` and `original_prompt` | The model can't forge IDs |
| Fail-safe | `pyautogui.FAILSAFE` or global Escape hook | `FAILSAFE` forced on before coordinate input, plus `Dispatcher.abort()`. The global hotkey is assigned to Person 4 ([P4 spec](specs/P4_FEEDBACK_HUD.md)) | A hotkey library isn't in the core dependencies |
| Visual fallback | enabled as last resort | Off by default (`execution.enable_visual_fallback`) | Clicks at unverified coordinates are unsafe |
| Retries | not specified | Up to `agent.max_step_retries` per step, but only for `retryable` failures | Re-dispatching a step that never acted only inflates `recovery_attempts` |
| Wiring | not specified | `clavix/runtime.py` builds everything from `configs/default_config.yaml` (`modules.*.class`) | Teammates plug in by config, not by editing core code |
| Additions | none | `clavix/cli.py`, `clavix/config.py`, `clavix/runtime.py`, `clavix/testing/` (fakes + contract suites), safety command blocklist | Developer experience and safety |
| Benchmark working directory | not specified | Each task runs with its own workspace (`runs/<run_id>/workspace/<task>-<rep>`) as the current directory | An LLM can't know the workspace path, so "note.txt in the workspace" becomes a relative path; without this it lands in the repo root and ground truth can never pass |
| Audio benchmark | `--audio` for every task | Tasks without an `audio_fixture` are skipped with a warning | Recordings are optional per task; a missing file is not a task failure |
