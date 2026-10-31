# P1 Spec: Core, Evaluation and Integration

**Owner:** Person 1 (lead architect)  **Phases:** 2–4

## 1. Mission

Keep the core stable while the members build their modules. Extend the harness so it
produces the paper's datasets, and integrate each module as it lands.

## 2. Ownership

Person 1 owns everything under `clavix/` except the member files listed in
[ROADMAP.md](../ROADMAP.md). That includes the three contract files (`clavix/modules/acoustic.py`,
`grounding.py`, `feedback.py`), `configs/`, `docs/`, `pyproject.toml` and CI.

P1 also reviews every member PR. Any change to a contract follows
[CONTRIBUTING.md § Changing a contract](../../CONTRIBUTING.md#changing-a-contract).

## 3. Tasks

Do them in ID order unless the dependency graph in the roadmap allows otherwise.

### P1-0 Repository setup (before Phase 2 starts)

1. `git init`, make the first commit, push to a private GitHub repo, and add the members.
2. Protect `main`: PRs required, one approval (P1), and CI must pass once P1-3 lands.
3. Add `.github/CODEOWNERS` with the ownership table from ROADMAP.md. The contract files
   require P1's review.

**Done when:** members can clone, run `uv sync`, and get a green `uv run pytest`.

### P1-1 Planner accuracy metric and prompt hardening

1. Add `plan_action_match: Optional[bool]` and `plan_exact_match: Optional[bool]` to
   `BenchmarkRecord`, defaulting to `None`.
2. In `BenchmarkHarness.run_task`, when `planner_mode == "llm"` and a plan was produced, compare
   it with `task.reference_steps()`:
   - `plan_action_match`: the lists of `(action_name, action_type)` are equal.
   - `plan_exact_match`: `plan_action_match`, and every payload is equal after both sides go
     through `ActionStep` normalization. Ignore `expected_postcondition`.
3. Add `plan_accuracy_by_tier(df)` to `metrics.py`: the mean of both columns per tier, ignoring `None`.
4. **Prompt hardening.** In a live test, `llama3:8b` (a) dropped a `speak_feedback` step that
   was explicitly requested and (b) set `detach: true` on `mkdir`. Add one few-shot example
   for each case to `FEW_SHOT_EXAMPLES` in `planner.py`. Then run
   `clavix-bench --planner llm --repeat 3` before and after the change, and record both
   results in the PR.

**Done when:** an LLM-mode results CSV contains both columns, and the metrics CLI prints plan
accuracy. Unit tests cover the comparison, including payload normalization (for example,
`https://github.com` and `https://github.com/` are equal).

### P1-2 Benchmark repetitions and cleanup

1. Add `--repeat N` (default 1). Each task runs N times in suite order. Records gain
   `repetition: int` (starting at 0).
2. Add an optional `cleanup_processes: list[str]` field to `BenchmarkTask` (image names such as
   `"notepad.exe"`). Set it on T3-01. Before the task, snapshot the PIDs of processes with
   those names. After the task, kill only the **new** PIDs (`taskkill /F /PID` on Windows,
   `os.kill` on POSIX). Enabled by `--cleanup` (off by default).
3. Use one workspace per repetition: `runs/<run_id>/workspace/<task_id>-<rep>`.

**Done when:** `clavix-bench --repeat 3 --cleanup --tiers 3` leaves no new Notepad processes,
and `latency_summary` p95 is computed over 3 samples per task.

### P1-3 Continuous integration

Create `.github/workflows/ci.yml`:
- Triggers: `pull_request`, and `push` to `main`.
- Matrix: `windows-latest` and `ubuntu-latest`.
- Steps: `actions/checkout`, `astral-sh/setup-uv` (pin a version tag), `uv sync`,
  `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`. Install **no extras**:
  this checks that every member module imports lazily.
  Then `uv run pytest -m "not slow and not desktop"`.

**Done when:** a PR shows both matrix jobs green, and `main` requires them.

### P1-4 Audio-in-the-loop benchmark (needs P2-M3)

1. Add an optional `audio_fixture: str` field to `BenchmarkTask`, relative to the repo root,
   e.g. `tests/fixtures/audio/T1-01.wav`.
2. Add `--audio`: for each task, build the configured acoustic class with
   `cls.from_wav_files([fixture], **options)` (a classmethod P2 provides; see the P2 spec, §6).
   Then call `agent.run()` with no command, so the transcript comes from ASR. Call
   `acoustic.warmup()` (when the class has it) before the timed run: the first call otherwise
   includes the model load (about 1.3 s for `base.en`) and inflates p95. Loaded models are
   shared between instances, so a fresh instance per task is cheap after the first.
3. Add `transcript: str` and `transcript_wer: Optional[float]` to `BenchmarkRecord`.
   Implement `word_error_rate(reference, hypothesis)` in `metrics.py` as a word-level
   Levenshtein distance over normalized text (casefold, strip punctuation). Don't add a
   dependency for this.

**Done when:** `clavix-bench --audio --planner llm` produces non-zero `t_asr_ms` for every task,
and the metrics CLI prints mean WER.

### P1-5 Integration (Phase 3)

For each member module, in the order **P4, then P3, then P2**:
1. Review the PR against that member's Definition of Done.
2. Merge it, then change `modules.<name>.class` and `options` in `configs/default_config.yaml`.
3. Run `uv run pytest`, then `uv run clavix-bench --planner reference --cleanup`. Compare against
   the Phase 1 baseline (Tier 1: 4/4, Tier 2: 3/3, Tier 3: 0/3). Any regression blocks the
   switch.
4. Update the "Current stand-ins" paragraph in README.md.

### P1-6 Backlog (not scheduled)

- A `type_text` and `press_keys` action (new `ActionName` values, payload models, dispatcher
  routes and grounding contract methods). This is a contract change, so announce it before
  starting.
- AT-SPI grounding for Linux.

## 4. Rules for P1 changes

- Tests stay green on every commit. Coverage of any new code is required.
- Never break a contract silently. Contract edits follow CONTRIBUTING.md.
- Keep `docs/MASTER_SPEC.md` §7 (deviations) current when the behaviour differs from the original spec.
