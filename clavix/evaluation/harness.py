"""Automated batch benchmark runner.

Runs every task in a suite (``configs/benchmark_suite.json``) through the full
agent loop, asserts ground-truth postconditions independently of the agent's own
verifier, and writes ``results_<timestamp>.csv`` / ``.json`` for analysis.

Planner modes:
    * ``reference`` — the LLM is replaced by a mock that returns each task's
      reference plan. Isolates execution + verification from planning quality.
    * ``llm`` — the configured LLM (Ollama / Gemini) plans from the raw command.

Usage::

    uv run clavix-bench --planner reference --tiers 1 2
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Optional

import pandas as pd
from pydantic import BaseModel, Field

from clavix.config import ClavixConfig, load_config, resolve_repo_path
from clavix.core.planner import MockLLMClient, Planner, build_planner
from clavix.core.verifier import StateVerifier
from clavix.execution.native_ops import NativeOps
from clavix.modules.feedback import FeedbackInterface
from clavix.modules.grounding import GroundingInterface
from clavix.runtime import build_runtime, harden_stdio
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
        output_dir: str | Path | None = None,
        native: NativeOps | None = None,
        grounding: GroundingInterface | None = None,
        feedback: FeedbackInterface | None = None,
        verifier: StateVerifier | None = None,
    ) -> None:
        self.suite = suite
        self.config = config
        self.planner_mode = planner_mode
        self.output_dir = Path(output_dir) if output_dir else resolve_repo_path(config.benchmark.output_dir)
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

    async def run_task(self, task: BenchmarkTask) -> TaskReport:
        """Run one (already resolved) task and check its ground truth."""
        agent = self.runtime.make_agent(self._planner_for(task))
        self.runtime.dispatcher.reset_abort()
        outcome = await agent.run(task.command)

        results = [(gt, *check_ground_truth(gt, outcome)) for gt in task.ground_truth]
        checks: list[dict[str, Any]] = [
            {"type": gt.type, "passed": ok, "detail": detail} for gt, ok, detail in results
        ]
        gt_ok = all(ok for _, ok, _ in results)
        error = outcome.error_message
        if outcome.success and not gt_ok:
            error = "ground truth failed: " + "; ".join(detail for _, ok, detail in results if not ok)

        record = outcome.to_benchmark_record(
            task.task_id,
            task.tier,
            task_success=outcome.success and gt_ok,
            planner=self._planner_label(),
            error_message=error,
        )
        return TaskReport(record=record, ground_truth=checks, outcome=outcome)

    async def run(self, tiers: Sequence[int] | None = None) -> list[TaskReport]:
        """Run all applicable tasks in suite order.

        Args:
            tiers: Restrict to these tiers; ``None`` runs all.
        """
        self.workspace.mkdir(parents=True, exist_ok=True)
        reports: list[TaskReport] = []
        for task in self.suite.tasks:
            if tiers is not None and task.tier not in tiers:
                continue
            if not task.runs_on(sys.platform):
                logger.info("skipping %s: not supported on %s", task.task_id, sys.platform)
                continue
            report = await self.run_task(task.resolve(self.workspace))
            status = "PASS" if report.record.task_success else "FAIL"
            logger.info("%s %s (%s)", task.task_id, status, report.record.error_message or "ok")
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
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    harden_stdio()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(message)s"
    )
    config = load_config(args.config)
    suite = BenchmarkSuite.load(resolve_repo_path(args.suite or config.benchmark.suite_path))
    harness = BenchmarkHarness(suite, config, planner_mode=args.planner, output_dir=args.output)
    reports = asyncio.run(harness.run(args.tiers))
    csv_path, json_path = harness.write_results(reports)

    from clavix.evaluation.metrics import success_rate_by_tier

    df = pd.DataFrame([r.record.model_dump() for r in reports])
    if not df.empty:
        print(success_rate_by_tier(df).to_string(index=False))
    print(f"\nresults: {csv_path}\n         {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
