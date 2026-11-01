"""Automated batch benchmark runner.

Runs every task in a suite (``configs/benchmark_suite.json``) through the full
agent loop, asserts ground-truth postconditions independently of the agent's own
verifier, and writes ``results_<timestamp>.csv`` / ``.json`` for analysis.

Planner modes:
    * ``reference`` — the LLM is replaced by a mock that returns each task's
      reference plan. Isolates execution + verification from planning quality.
    * ``llm`` — the configured LLM (Ollama / Gemini) plans from the raw command.

Options:
    * ``--repeat N`` runs each task N times, each in a fresh workspace
      (``runs/<run_id>/workspace/<task_id>-<rep>``), for meaningful p95s.
    * ``--cleanup`` kills processes a task started (only PIDs that appeared during
      the task, for the image names in its ``cleanup_processes``).
    * ``--audio`` feeds each task's recorded command (``audio_fixture``) through
      the configured acoustic module instead of passing the text.

Each task runs with its workspace as the current directory, so relative paths an
LLM plan writes ("note.txt in the workspace") land where ground truth looks.

Usage::

    uv run clavix-bench --planner reference --tiers 1 2
    uv run clavix-bench --planner llm --repeat 5 --cleanup --audio
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import csv
import io
import json
import logging
import os
import signal
import subprocess
import sys
from collections.abc import Iterable, Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Optional

import pandas as pd
from pydantic import BaseModel, Field

from clavix.config import ClavixConfig, load_config, resolve_repo_path
from clavix.core.planner import MockLLMClient, Planner, build_planner
from clavix.core.verifier import StateVerifier
from clavix.evaluation.metrics import word_error_rate
from clavix.execution.native_ops import NativeOps
from clavix.modules.acoustic import AcousticInterface
from clavix.modules.feedback import ConsoleFeedback, FeedbackInterface
from clavix.modules.grounding import GroundingInterface
from clavix.runtime import ComponentError, build_runtime, harden_stdio
from clavix.schemas.actions import ActionStep
from clavix.schemas.telemetry import BenchmarkRecord, TaskOutcome

logger = logging.getLogger(__name__)

PlannerMode = Literal["reference", "llm"]


class GroundTruth(BaseModel):
    """An externally checked success criterion for a task."""

    type: Literal[
        "file_exists",
        "file_contains",
        "screenshot_valid",
        "url_launched",
        "process_spawned",
        "stdout_contains",
    ]
    path: Optional[str] = None
    text: Optional[str] = None
    step_id: Optional[int] = None


class BenchmarkTask(BaseModel):
    """One benchmark task; strings may contain the ``{workspace}`` placeholder."""

    task_id: str
    tier: int = Field(ge=1, le=3)
    description: str = ""
    command: str
    reference_plan: list[dict[str, Any]]
    ground_truth: list[GroundTruth] = Field(default_factory=list)
    platforms: Optional[list[str]] = None
    """``sys.platform`` values the task can run on; ``None`` means all."""
    cleanup_processes: Optional[list[str]] = None
    """Image names (e.g. ``"notepad.exe"``) whose new processes ``--cleanup`` kills after the task."""
    audio_fixture: Optional[str] = None
    """Recorded command, relative to the repository root, used by ``--audio``."""

    def runs_on(self, platform: str) -> bool:
        """Whether this task applies to ``platform``."""
        return self.platforms is None or platform in self.platforms

    def resolve(self, workspace: Path) -> BenchmarkTask:
        """Return a copy with ``{workspace}`` substituted (POSIX-style separators)."""
        ws = workspace.resolve().as_posix()
        return BenchmarkTask.model_validate(_substitute(self.model_dump(), "{workspace}", ws))

    def reference_steps(self) -> list[ActionStep]:
        """Validate and return the reference plan steps."""
        return [ActionStep.model_validate(s) for s in self.reference_plan]


class BenchmarkSuite(BaseModel):
    """A named collection of benchmark tasks."""

    name: str
    version: str = "0"
    description: str = ""
    tasks: list[BenchmarkTask]

    @classmethod
    def load(cls, path: str | Path) -> BenchmarkSuite:
        """Load a suite from a JSON file."""
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))


def _substitute(value: Any, placeholder: str, replacement: str) -> Any:
    if isinstance(value, str):
        return value.replace(placeholder, replacement)
    if isinstance(value, list):
        return [_substitute(v, placeholder, replacement) for v in value]
    if isinstance(value, dict):
        return {k: _substitute(v, placeholder, replacement) for k, v in value.items()}
    return value


def check_ground_truth(gt: GroundTruth, outcome: TaskOutcome) -> tuple[bool, str]:
    """Assert one ground-truth criterion against the filesystem and step results.

    Returns:
        ``(passed, detail)``.
    """
    match gt.type:
        case "file_exists":
            ok = gt.path is not None and Path(gt.path).exists()
            return ok, f"{gt.path} exists={ok}"
        case "file_contains":
            if gt.path is None or not Path(gt.path).is_file():
                return False, f"{gt.path} missing"
            content = Path(gt.path).read_text(encoding="utf-8", errors="replace")
            ok = (gt.text or "") in content
            return ok, f"{gt.path} contains {gt.text!r}={ok}"
        case "screenshot_valid":
            if gt.path is None or not Path(gt.path).is_file():
                return False, f"{gt.path} missing"
            try:
                from PIL import Image

                with Image.open(gt.path) as img:
                    img.verify()
                    w, h = img.size
            except Exception as exc:
                return False, f"{gt.path} is not a valid image: {exc}"
            return w > 0 and h > 0, f"{gt.path} is {w}x{h}"
        case "url_launched" | "process_spawned":
            result = outcome.results.get(gt.step_id) if gt.step_id is not None else None
            if result is None:
                return False, f"step {gt.step_id} did not run"
            ok = result.success and (gt.type == "url_launched" or result.pid is not None)
            return ok, f"step {gt.step_id} success={result.success} pid={result.pid}"
        case "stdout_contains":
            result = outcome.results.get(gt.step_id) if gt.step_id is not None else None
            ok = result is not None and (gt.text or "") in result.stdout
            return ok, f"step {gt.step_id} stdout contains {gt.text!r}={ok}"
    raise AssertionError(f"unhandled ground truth {gt.type}")  # pragma: no cover


def compare_plans(plan: Sequence[ActionStep], reference: Sequence[ActionStep]) -> tuple[bool, bool]:
    """Compare an LLM plan with the reference plan.

    Returns:
        ``(action_match, exact_match)``. ``action_match``: the ``(action_name,
        action_type)`` sequences are equal. ``exact_match``: also every payload is
        equal. Payloads are compared in their ``ActionStep``-normalised form (so
        ``https://github.com`` equals ``https://github.com/``);
        ``expected_postcondition`` is ignored.
    """
    actions = [(s.action_name, s.action_type) for s in plan]
    action_match = actions == [(s.action_name, s.action_type) for s in reference]
    exact = action_match and all(
        ActionStep.model_validate(a.model_dump()).payload == ActionStep.model_validate(b.model_dump()).payload
        for a, b in zip(plan, reference, strict=True)
    )
    return action_match, exact


def list_pids(image_names: Iterable[str]) -> set[int]:
    """PIDs of running processes whose image name matches one of ``image_names`` (case-insensitive)."""
    wanted = {name.casefold() for name in image_names}
    if not wanted:
        return set()
    if sys.platform == "win32":
        out = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True, check=False
        ).stdout
        rows = ((row[0], row[1]) for row in csv.reader(io.StringIO(out)) if len(row) > 1)
    else:
        out = subprocess.run(
            ["ps", "-A", "-o", "pid=,comm="], capture_output=True, text=True, check=False
        ).stdout
        rows = ((parts[1], parts[0]) for line in out.splitlines() if len(parts := line.split(None, 1)) == 2)
    pids = set()
    for name, pid in rows:
        if Path(name.strip()).name.casefold() in wanted and pid.strip().isdigit():
            pids.add(int(pid))
    return pids


def kill_pids(pids: Iterable[int]) -> None:
    """Forcefully end ``pids``; processes that already exited are ignored."""
    for pid in pids:
        if sys.platform == "win32":
            subprocess.run(  # noqa: S603 - fixed argv; pid is an int
                ["taskkill", "/F", "/PID", str(pid)], capture_output=True, check=False
            )
        else:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGKILL)


@contextlib.contextmanager
def _working_directory(path: Path) -> Iterator[None]:
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


class TaskReport(BaseModel):
    """A task's benchmark record plus the detail needed to debug it."""

    record: BenchmarkRecord
    ground_truth: list[dict[str, Any]]
    outcome: TaskOutcome


class BenchmarkHarness:
    """Runs a suite sequentially and serialises the results."""

    def __init__(
        self,
        suite: BenchmarkSuite,
        config: ClavixConfig,
        *,
        planner_mode: PlannerMode = "reference",
        audio: bool = False,
        output_dir: str | Path | None = None,
        native: NativeOps | None = None,
        grounding: GroundingInterface | None = None,
        feedback: FeedbackInterface | None = None,
        verifier: StateVerifier | None = None,
    ) -> None:
        self.suite = suite
        self.config = config
        self.planner_mode = planner_mode
        self.audio = audio
        output = Path(output_dir) if output_dir else resolve_repo_path(config.benchmark.output_dir)
        self.output_dir = output.resolve()  # absolute: tasks run with their workspace as the cwd
        self.runtime = build_runtime(
            config, grounding=grounding, feedback=feedback, native=native, verifier=verifier
        )
        self.run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        self.workspace = self.output_dir / "runs" / self.run_id / "workspace"

    def _planner_for(self, task: BenchmarkTask) -> Planner:
        if self.planner_mode == "reference":
            plan = {"steps": task.reference_plan}
            return Planner(MockLLMClient(responder=lambda _messages: plan), max_retries=0)
        return build_planner(self.config.planner)

    def _acoustic_for(self, task: BenchmarkTask) -> AcousticInterface:
        """Build the configured acoustic class over the task's fixture and warm it up."""
        if task.audio_fixture is None:
            raise ComponentError(f"{task.task_id} has no audio_fixture")
        spec = self.config.modules.acoustic
        module_name, _, class_name = spec.class_path.partition(":")
        cls = getattr(__import__(module_name, fromlist=[class_name]), class_name)
        if not hasattr(cls, "from_wav_files"):
            raise ComponentError(f"{spec.class_path} has no from_wav_files(); --audio needs it")
        acoustic: AcousticInterface = cls.from_wav_files(
            [str(resolve_repo_path(task.audio_fixture))], **spec.options
        )
        warmup = getattr(acoustic, "warmup", None)
        if callable(warmup):
            warmup()  # keep the model load out of t_asr_ms
        return acoustic

    async def run_task(self, task: BenchmarkTask, repetition: int = 0) -> TaskReport:
        """Run one (already resolved) task and check its ground truth."""
        acoustic = self._acoustic_for(task) if self.audio else None
        agent = self.runtime.make_agent(self._planner_for(task), acoustic=acoustic)
        self.runtime.dispatcher.reset_abort()
        try:
            outcome = await agent.run(None if self.audio else task.command)
        finally:
            close = getattr(acoustic, "close", None)
            if callable(close):
                close()

        results = [(gt, *check_ground_truth(gt, outcome)) for gt in task.ground_truth]
        checks: list[dict[str, Any]] = [
            {"type": gt.type, "passed": ok, "detail": detail} for gt, ok, detail in results
        ]
        gt_ok = all(ok for _, ok, _ in results)
        error = outcome.error_message
        if outcome.success and not gt_ok:
            error = "ground truth failed: " + "; ".join(detail for _, ok, detail in results if not ok)

        plan_match: tuple[bool | None, bool | None] = (None, None)
        if self.planner_mode == "llm" and outcome.plan is not None:
            plan_match = compare_plans(outcome.plan.steps, task.reference_steps())
        record = outcome.to_benchmark_record(
            task.task_id,
            task.tier,
            task_success=outcome.success and gt_ok,
            planner=self._planner_label(),
            error_message=error,
            repetition=repetition,
            transcript_wer=word_error_rate(task.command, outcome.transcript) if self.audio else None,
            plan_action_match=plan_match[0],
            plan_exact_match=plan_match[1],
        )
        return TaskReport(record=record, ground_truth=checks, outcome=outcome)

    async def run(
        self, tiers: Sequence[int] | None = None, *, repeat: int = 1, cleanup: bool = False
    ) -> list[TaskReport]:
        """Run all applicable tasks in suite order, each ``repeat`` times in a row.

        Args:
            tiers: Restrict to these tiers; ``None`` runs all.
            repeat: Runs per task; each gets its own workspace.
            cleanup: Kill processes a task started (see ``cleanup_processes``).
        """
        reports: list[TaskReport] = []
        for task in self.suite.tasks:
            if tiers is not None and task.tier not in tiers:
                continue
            if not task.runs_on(sys.platform):
                logger.info("skipping %s: not supported on %s", task.task_id, sys.platform)
                continue
            if self.audio and task.audio_fixture is None:
                logger.warning("skipping %s: no audio_fixture for --audio", task.task_id)
                continue
            for rep in range(repeat):
                workspace = self.workspace / f"{task.task_id}-{rep}"
                workspace.mkdir(parents=True, exist_ok=True)
                names = (task.cleanup_processes or []) if cleanup else []
                before = list_pids(names)
                resolved = task.resolve(workspace)  # before the chdir, so paths never depend on the cwd
                try:
                    with _working_directory(workspace):
                        report = await self.run_task(resolved, repetition=rep)
                finally:
                    if names:
                        kill_pids(list_pids(names) - before)
                status = "PASS" if report.record.task_success else "FAIL"
                logger.info("%s#%d %s (%s)", task.task_id, rep, status, report.record.error_message or "ok")
                reports.append(report)
        return reports

    def write_results(self, reports: Sequence[TaskReport]) -> tuple[Path, Path]:
        """Write ``results_<run_id>.csv`` and ``.json`` into the output directory."""
        self.output_dir.mkdir(parents=True, exist_ok=True)
        csv_path = self.output_dir / f"results_{self.run_id}.csv"
        json_path = self.output_dir / f"results_{self.run_id}.json"

        records = [r.record.model_dump() for r in reports]
        pd.DataFrame(records, columns=list(BenchmarkRecord.model_fields)).to_csv(csv_path, index=False)
        payload = {
            "run_id": self.run_id,
            "suite": {"name": self.suite.name, "version": self.suite.version},
            "planner": self._planner_label(),
            "audio": self.audio,
            "platform": sys.platform,
            "config": self.config.model_dump(),
            "records": records,
            "tasks": [r.model_dump(mode="json") for r in reports],
        }
        json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return csv_path, json_path

    def _planner_label(self) -> str:
        if self.planner_mode == "reference":
            return "reference"
        return f"{self.config.planner.provider}:{self.config.planner.model}"


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point (``clavix-bench``)."""
    parser = argparse.ArgumentParser(description="Run the Clavix benchmark suite.")
    parser.add_argument("--config", help="config YAML (default: configs/default_config.yaml)")
    parser.add_argument("--suite", help="suite JSON (default: from config)")
    parser.add_argument("--planner", choices=["reference", "llm"], default="reference")
    parser.add_argument("--tiers", type=int, nargs="+", choices=[1, 2, 3])
    parser.add_argument("--output", help="output directory (default: from config)")
    parser.add_argument("--repeat", type=int, default=1, metavar="N", help="runs per task (default: 1)")
    parser.add_argument("--cleanup", action="store_true", help="kill processes each task started")
    parser.add_argument("--audio", action="store_true", help="speak each command from its recorded fixture")
    parser.add_argument(
        "--live-feedback",
        action="store_true",
        help="use the configured feedback (speech, HUD) instead of console output; adds speech time",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    harden_stdio()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(message)s"
    )
    config = load_config(args.config)
    suite = BenchmarkSuite.load(resolve_repo_path(args.suite or config.benchmark.suite_path))
    if args.repeat < 1:
        parser.error("--repeat must be at least 1")
    # Console feedback by default: blocking speech would add its own duration to execution time.
    feedback = None if args.live_feedback else ConsoleFeedback()
    harness = BenchmarkHarness(
        suite,
        config,
        planner_mode=args.planner,
        audio=args.audio,
        output_dir=args.output,
        feedback=feedback,
    )
    reports = asyncio.run(harness.run(args.tiers, repeat=args.repeat, cleanup=args.cleanup))
    csv_path, json_path = harness.write_results(reports)

    from clavix.evaluation.metrics import plan_accuracy_by_tier, success_rate_by_tier, transcription_summary

    df = pd.DataFrame([r.record.model_dump() for r in reports])
    if not df.empty:
        print(success_rate_by_tier(df).to_string(index=False))
        if df["plan_action_match"].notna().any():
            print("\nPlan accuracy\n" + plan_accuracy_by_tier(df).to_string(index=False))
        if df["transcript_wer"].notna().any():
            print("\nTranscription\n" + transcription_summary(df).to_string(index=False))
    print(f"\nresults: {csv_path}\n         {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
