"""Benchmark suite loading, harness execution and metric aggregation."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pandas as pd
import pytest

from clavix.config import ClavixConfig, load_config, resolve_repo_path
from clavix.core.verifier import StateVerifier
from clavix.evaluation.harness import BenchmarkHarness, BenchmarkSuite, BenchmarkTask, main
from clavix.evaluation.metrics import (
    latency_summary,
    latency_waterfall,
    load_results,
    records_to_frame,
    success_rate_by_tier,
    write_summary,
)
from clavix.schemas.telemetry import BenchmarkRecord
from clavix.testing.fakes import FakeGrounding, FakeNativeOps, RecordingFeedback

SUITE_PATH = resolve_repo_path("configs/benchmark_suite.json")


def mini_suite() -> BenchmarkSuite:
    return BenchmarkSuite.model_validate(
        {
            "name": "mini",
            "tasks": [
                {
                    "task_id": "A",
                    "tier": 1,
                    "command": "write a file",
                    "reference_plan": [
                        {
                            "step_id": 1,
                            "action_type": "DETERMINISTIC",
                            "action_name": "run_command",
                            "payload": {"cmd": 'echo hi> "{workspace}/a.txt"'},
                            "expected_postcondition": "file_exists:{workspace}/a.txt",
                        }
                    ],
                    "ground_truth": [{"type": "file_contains", "path": "{workspace}/a.txt", "text": "hi"}],
                },
                {
                    "task_id": "B",
                    "tier": 2,
                    "command": "open a url then screenshot",
                    "reference_plan": [
                        {
                            "step_id": 1,
                            "action_type": "DETERMINISTIC",
                            "action_name": "launch_url",
                            "payload": {"url": "https://example.com"},
                        },
                        {
                            "step_id": 2,
                            "action_type": "DETERMINISTIC",
                            "action_name": "take_screenshot",
                            "payload": {"filepath": "{workspace}/b.png"},
                        },
                    ],
                    "ground_truth": [
                        {"type": "url_launched", "step_id": 1},
                        {"type": "screenshot_valid", "path": "{workspace}/b.png"},
                    ],
                },
                {
                    "task_id": "C",
                    "tier": 3,
                    "command": "click ok",
                    "reference_plan": [
                        {
                            "step_id": 1,
                            "action_type": "OS_GROUNDING",
                            "action_name": "click_element",
                            "payload": {"target_label": "OK"},
                        }
                    ],
                },
                {
                    "task_id": "D",
                    "tier": 2,
                    "command": "ground truth mismatch",
                    "reference_plan": [
                        {
                            "step_id": 1,
                            "action_type": "DETERMINISTIC",
                            "action_name": "run_command",
                            "payload": {"cmd": "echo nothing"},
                        }
                    ],
                    "ground_truth": [{"type": "file_exists", "path": "{workspace}/missing.txt"}],
                },
                {
                    "task_id": "E",
                    "tier": 1,
                    "command": "other platform only",
                    "platforms": ["plan9"],
                    "reference_plan": [
                        {
                            "step_id": 1,
                            "action_type": "DETERMINISTIC",
                            "action_name": "run_command",
                            "payload": {"cmd": "echo"},
                        }
                    ],
                },
            ],
        }
    )


@pytest.fixture
def harness(tmp_path: Path) -> BenchmarkHarness:
    return BenchmarkHarness(
        mini_suite(),
        ClavixConfig(),
        planner_mode="reference",
        output_dir=tmp_path,
        native=FakeNativeOps(),
        grounding=FakeGrounding(),
        feedback=RecordingFeedback(),
        verifier=StateVerifier(timeout_sec=0.5, poll_interval_sec=0.05, window_titles=lambda: []),
    )


class TestSuiteFile:
    def test_shipped_suite_has_ten_tasks_across_tiers(self) -> None:
        suite = BenchmarkSuite.load(SUITE_PATH)
        assert len(suite.tasks) == 10
        assert {t.tier for t in suite.tasks} == {1, 2, 3}
        assert len({t.task_id for t in suite.tasks}) == 10

    def test_shipped_reference_plans_validate(self, tmp_path: Path) -> None:
        for task in BenchmarkSuite.load(SUITE_PATH).tasks:
            resolved = task.resolve(tmp_path)
            steps = resolved.reference_steps()
            assert steps, task.task_id
            dumped = json.dumps(resolved.model_dump())
            assert "{workspace}" not in dumped

    def test_default_config_loads(self) -> None:
        config = load_config()
        assert config.planner.provider in {"ollama", "gemini", "mock"}
        assert config.execution.blocked_command_patterns

    def test_platform_filter(self) -> None:
        task = BenchmarkTask(task_id="x", tier=1, command="c", reference_plan=[], platforms=["win32"])
        assert task.runs_on("win32") and not task.runs_on("linux")


class TestHarness:
    def test_runs_suite_and_asserts_ground_truth(self, harness: BenchmarkHarness) -> None:
        reports = asyncio.run(harness.run())
        by_id = {r.record.task_id: r for r in reports}
        assert set(by_id) == {"A", "B", "C", "D"}  # E skipped by platform

        assert by_id["A"].record.task_success
        assert by_id["B"].record.task_success and by_id["B"].record.n_steps == 2
        # Grounding cannot resolve "OK" and there is no fallback coordinate.
        assert not by_id["C"].record.task_success
        # Agent succeeded but the independent ground truth did not.
        assert by_id["D"].outcome.success and not by_id["D"].record.task_success
        assert "ground truth failed" in (by_id["D"].record.error_message or "")
        assert all(r.record.planner == "reference" for r in reports)

    def test_tier_filter(self, harness: BenchmarkHarness) -> None:
        reports = asyncio.run(harness.run(tiers=[2]))
        assert {r.record.task_id for r in reports} == {"B", "D"}

    def test_writes_csv_and_json(self, harness: BenchmarkHarness) -> None:
        reports = asyncio.run(harness.run())
        csv_path, json_path = harness.write_results(reports)
        assert csv_path.name.startswith("results_") and csv_path.suffix == ".csv"

        df = pd.read_csv(csv_path)
        assert list(df.columns)[:7] == [
            "task_id",
            "tier",
            "t_asr_ms",
            "t_llm_planning_ms",
            "t_total_execution_ms",
            "task_success",
            "recovery_attempts",
        ]
        assert len(df) == 4

        data = json.loads(json_path.read_text())
        assert data["planner"] == "reference" and len(data["records"]) == 4
        assert data["tasks"][0]["outcome"]["steps"][0]["verification"] == "PASSED"
        # JSON results are loadable by the metrics module.
        assert len(load_results(json_path)) == 4


class TestMetrics:
    @staticmethod
    def records() -> list[BenchmarkRecord]:
        def rec(task_id: str, tier: int, ok: bool, ms: float) -> BenchmarkRecord:
            return BenchmarkRecord(
                task_id=task_id,
                tier=tier,
                t_asr_ms=ms,
                t_llm_planning_ms=2 * ms,
                t_total_execution_ms=3 * ms,
                task_success=ok,
                recovery_attempts=0 if ok else 1,
            )

        return [rec("a", 1, True, 10), rec("b", 1, False, 20), rec("c", 2, True, 30)]

    def test_success_rate(self) -> None:
        table = success_rate_by_tier(records_to_frame(self.records()))
        tier1 = table[table["tier"] == 1].iloc[0]
        assert tier1["n_tasks"] == 2 and tier1["task_success_rate"] == pytest.approx(0.5)
        overall = table[table["tier"] == "all"].iloc[0]
        assert overall["task_success_rate"] == pytest.approx(2 / 3)

    def test_latency_tables(self) -> None:
        df = records_to_frame(self.records())
        assert df["t_end_to_end_ms"].tolist() == [60, 120, 180]
        waterfall = latency_waterfall(df)
        assert waterfall.loc[waterfall["tier"] == 1, "t_llm_planning_ms"].item() == pytest.approx(30)
        summary = latency_summary(df)
        row = summary[(summary["tier"] == 1) & (summary["phase"] == "t_asr_ms")].iloc[0]
        assert row["mean"] == pytest.approx(15) and row["median"] == pytest.approx(15)

    def test_write_summary(self, tmp_path: Path) -> None:
        paths = write_summary(records_to_frame(self.records()), tmp_path)
        assert len(paths) == 3 and all(p.exists() for p in paths)


def test_cli_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The ``clavix-bench`` CLI runs a suite file and prints the success table."""
    suite = {"name": "cli", "tasks": [mini_suite().tasks[0].model_dump()]}
    suite_path = tmp_path / "suite.json"
    suite_path.write_text(json.dumps(suite))
    assert main(["--suite", str(suite_path), "--output", str(tmp_path / "out")]) == 0
    out = capsys.readouterr().out
    assert "task_success_rate" in out
    assert list((tmp_path / "out").glob("results_*.csv"))


@pytest.mark.skipif(sys.platform != "win32", reason="cmd.exe path quoting check")
def test_forward_slash_paths_work_in_cmd(tmp_path: Path) -> None:
    """The suite writes '{workspace}/x' paths; cmd.exe must accept them when quoted."""
    from clavix.execution.native_ops import run_cli

    target = tmp_path.as_posix() + "/dir"
    assert run_cli(f'mkdir "{target}"').success
    assert run_cli(f'echo ok> "{target}/f.txt"').success
    assert (tmp_path / "dir" / "f.txt").read_text().strip() == "ok"
