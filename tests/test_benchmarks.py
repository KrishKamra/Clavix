"""Benchmark suite loading, harness execution and metric aggregation."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

import pandas as pd
import pytest

from clavix.config import ClavixConfig, load_config, resolve_repo_path
from clavix.core.planner import MockLLMClient, Planner
from clavix.core.verifier import StateVerifier
from clavix.evaluation import harness as harness_module
from clavix.evaluation.harness import (
    BenchmarkHarness,
    BenchmarkSuite,
    BenchmarkTask,
    compare_plans,
    kill_pids,
    list_pids,
    main,
)
from clavix.evaluation.metrics import (
    latency_summary,
    latency_waterfall,
    load_results,
    plan_accuracy_by_tier,
    records_to_frame,
    success_rate_by_tier,
    transcription_summary,
    word_error_rate,
    write_summary,
)
from clavix.evaluation.metrics import main as metrics_main
from clavix.runtime import ComponentError
from clavix.schemas.actions import ActionStep
from clavix.schemas.telemetry import BenchmarkRecord
from clavix.testing.fakes import FakeGrounding, FakeNativeOps, FakeWavAcoustic, RecordingFeedback

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


def _step(
    step_id: int, name: str = "launch_url", kind: str = "DETERMINISTIC", **payload: object
) -> ActionStep:
    return ActionStep.model_validate(
        {
            "step_id": step_id,
            "action_type": kind,
            "action_name": name,
            "payload": payload or {"url": "https://a.org"},
        }
    )


class TestComparePlans:
    def test_identical_plans_match_exactly(self) -> None:
        assert compare_plans([_step(1)], [_step(1)]) == (True, True)

    def test_payloads_are_compared_after_normalisation(self) -> None:
        with_slash = _step(1, url="https://github.com/")
        without = _step(1, url="https://github.com")
        assert compare_plans([with_slash], [without]) == (True, True)
        # Defaults count as present: {"cmd": "x"} equals {"cmd": "x", "timeout_sec": 10, ...}.
        short = _step(1, "run_command", cmd="notepad")
        full = _step(1, "run_command", cmd="notepad", timeout_sec=10, detach=False)
        assert compare_plans([short], [full]) == (True, True)

    def test_different_payload_is_an_action_match_only(self) -> None:
        plan, reference = [_step(1, "run_command", cmd="a")], [_step(1, "run_command", cmd="b")]
        assert compare_plans(plan, reference) == (True, False)

    def test_different_actions_or_lengths_do_not_match(self) -> None:
        assert compare_plans([_step(1)], [_step(1, "run_command", cmd="a")]) == (False, False)
        assert compare_plans([_step(1)], [_step(1), _step(2)]) == (False, False)
        grounded = _step(1, "click_element", "OS_GROUNDING", target_label="OK")
        visual = _step(1, "click_element", "VISUAL_FALLBACK", target_label="OK")
        assert compare_plans([grounded], [visual]) == (False, False)

    def test_postconditions_are_ignored(self) -> None:
        checked = ActionStep.model_validate(
            {**_step(1).model_dump(), "expected_postcondition": "window_title_contains:A"}
        )
        assert compare_plans([checked], [_step(1)]) == (True, True)


class TestWordErrorRate:
    def test_case_and_punctuation_are_ignored(self) -> None:
        assert word_error_rate("open github", "Open GitHub.") == 0.0
        assert word_error_rate("tell me when it's open", "Tell me when its open!") == 0.0

    def test_substitution_insertion_and_deletion(self) -> None:
        assert word_error_rate("run echo clavix ready", "run echo clovix ready") == 0.25
        assert word_error_rate("a b", "a b c") == 0.5
        assert word_error_rate("a b c d", "a d") == 0.5
        assert word_error_rate("a b", "") == 1.0

    def test_empty_reference_does_not_divide_by_zero(self) -> None:
        assert word_error_rate("", "") == 0.0
        assert word_error_rate("", "noise") == 1.0


class TestPlanAndTranscriptionMetrics:
    @staticmethod
    def frame() -> pd.DataFrame:
        def rec(
            task_id: str, tier: int, action: bool | None, exact: bool | None, wer: float | None
        ) -> BenchmarkRecord:
            return BenchmarkRecord(
                task_id=task_id,
                tier=tier,
                t_asr_ms=100.0,
                t_llm_planning_ms=1.0,
                t_total_execution_ms=1.0,
                task_success=True,
                recovery_attempts=0,
                plan_action_match=action,
                plan_exact_match=exact,
                transcript_wer=wer,
            )

        return records_to_frame(
            [
                rec("a", 1, True, True, 0.0),
                rec("b", 1, True, False, 0.5),
                rec("c", 2, False, False, None),
                rec("d", 2, None, None, None),
            ]
        )

    def test_plan_accuracy_ignores_unrated_rows(self) -> None:
        table = plan_accuracy_by_tier(self.frame())
        tier1 = table[table["tier"] == 1].iloc[0]
        assert tier1["n_plans"] == 2
        assert tier1["plan_action_match"] == pytest.approx(1.0)
        assert tier1["plan_exact_match"] == pytest.approx(0.5)
        overall = table[table["tier"] == "all"].iloc[0]
        assert overall["n_plans"] == 3 and overall["plan_action_match"] == pytest.approx(2 / 3)

    def test_transcription_summary(self) -> None:
        table = transcription_summary(self.frame())
        overall = table[table["tier"] == "all"].iloc[0]
        assert overall["n"] == 2 and overall["mean_wer"] == pytest.approx(0.25)
        assert overall["exact_rate"] == pytest.approx(0.5)
        assert overall["median_t_asr_ms"] == pytest.approx(100)

    def test_write_summary_adds_plan_and_asr_tables(self, tmp_path: Path) -> None:
        names = sorted(p.stem for p in write_summary(self.frame(), tmp_path))
        expected = (
            "success_by_tier",
            "latency_summary",
            "latency_waterfall",
            "plan_accuracy",
            "transcription",
        )
        assert names == sorted(f"summary_{name}" for name in expected)

    def test_metrics_cli_prints_plan_accuracy_and_wer(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = tmp_path / "results.csv"
        self.frame().to_csv(path, index=False)
        assert metrics_main([str(path)]) == 0
        out = capsys.readouterr().out
        assert "Plan accuracy" in out and "Mean WER: 0.250" in out


class TestRepetitionsAndWorkspaces:
    def test_each_repetition_gets_its_own_workspace(self, harness: BenchmarkHarness) -> None:
        cwd = Path.cwd()
        reports = asyncio.run(harness.run(tiers=[1], repeat=2))
        assert [(r.record.task_id, r.record.repetition) for r in reports] == [("A", 0), ("A", 1)]
        assert all(r.record.task_success for r in reports)
        for rep in (0, 1):
            assert (harness.workspace / f"A-{rep}" / "a.txt").read_text().strip() == "hi"
        assert Path.cwd() == cwd  # restored after each task

    def test_relative_paths_land_in_the_task_workspace(
        self, harness: BenchmarkHarness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        step = {
            "step_id": 1,
            "action_type": "DETERMINISTIC",
            "action_name": "run_command",
            "payload": {"cmd": "echo hi> a.txt"},
        }
        plan = {"steps": [step]}
        harness.planner_mode = "llm"
        monkeypatch.setattr(
            harness,
            "_planner_for",
            lambda task: Planner(MockLLMClient(responder=lambda _m: plan), max_retries=0),
        )
        (report,) = asyncio.run(harness.run(tiers=[1]))
        assert report.record.task_success  # ground truth reads {workspace}/a.txt
        assert (report.record.plan_action_match, report.record.plan_exact_match) == (True, False)

    def test_reference_mode_records_no_plan_match(self, harness: BenchmarkHarness) -> None:
        (report,) = asyncio.run(harness.run(tiers=[1]))
        assert report.record.plan_action_match is None
        assert report.record.transcript == "write a file"


class TestCleanup:
    @pytest.fixture
    def pids(self, harness: BenchmarkHarness, monkeypatch: pytest.MonkeyPatch) -> dict[str, list[object]]:
        harness.suite.tasks[0].cleanup_processes = ["fake.exe"]
        snapshots = iter([{1}, {1, 2, 3}])
        seen: dict[str, list[object]] = {"listed": [], "killed": []}

        def fake_list(names: list[str]) -> set[int]:
            seen["listed"].append(list(names))
            return next(snapshots) if names else set()

        monkeypatch.setattr(harness_module, "list_pids", fake_list)
        monkeypatch.setattr(harness_module, "kill_pids", lambda p: seen["killed"].append(set(p)))
        return seen

    def test_kills_only_processes_the_task_started(
        self, harness: BenchmarkHarness, pids: dict[str, list[object]]
    ) -> None:
        asyncio.run(harness.run(tiers=[1], cleanup=True))
        assert pids["killed"] == [{2, 3}]

    def test_does_nothing_without_the_flag(
        self, harness: BenchmarkHarness, pids: dict[str, list[object]]
    ) -> None:
        asyncio.run(harness.run(tiers=[1]))
        assert pids["killed"] == []
        assert all(names == [] for names in pids["listed"])

    def test_list_and_kill_a_real_process(self) -> None:
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        name = Path(sys.executable).name
        try:
            deadline = time.monotonic() + 10
            while proc.pid not in list_pids([name]) and time.monotonic() < deadline:
                time.sleep(0.1)
            assert proc.pid in list_pids([name.upper()])  # case-insensitive
            kill_pids([proc.pid])
            assert proc.wait(timeout=10) is not None
        finally:
            if proc.poll() is None:
                proc.kill()


class TestAudioMode:
    @staticmethod
    def make(tmp_path: Path, acoustic: dict[str, object]) -> BenchmarkHarness:
        suite = mini_suite()
        suite.tasks[0].audio_fixture = "tests/fixtures/audio/A.wav"
        config = ClavixConfig.model_validate({"modules": {"acoustic": acoustic}})
        return BenchmarkHarness(
            suite,
            config,
            planner_mode="reference",
            audio=True,
            output_dir=tmp_path,
            native=FakeNativeOps(),
            grounding=FakeGrounding(),
            feedback=RecordingFeedback(),
            verifier=StateVerifier(timeout_sec=0.5, poll_interval_sec=0.05, window_titles=lambda: []),
        )

    def test_commands_come_from_the_recording(self, tmp_path: Path) -> None:
        fake = {"class": "clavix.testing.fakes:FakeWavAcoustic", "options": {"text": "write a file"}}
        harness = self.make(tmp_path, fake)
        warmups = FakeWavAcoustic.warmups
        reports = asyncio.run(harness.run())  # B and D have no fixture and are skipped
        assert [r.record.task_id for r in reports] == ["A"]
        record = reports[0].record
        assert record.task_success and record.t_asr_ms == 12.5
        assert record.transcript == "write a file" and record.transcript_wer == 0.0
        assert FakeWavAcoustic.warmups == warmups + 1

    def test_misheard_command_has_a_wer(self, tmp_path: Path) -> None:
        fake = {"class": "clavix.testing.fakes:FakeWavAcoustic", "options": {"text": "write a fail"}}
        (report,) = asyncio.run(self.make(tmp_path, fake).run())
        assert report.record.transcript_wer == pytest.approx(1 / 3)

    def test_acoustic_class_needs_from_wav_files(self, tmp_path: Path) -> None:
        harness = self.make(tmp_path, {"class": "clavix.modules.acoustic:ConsoleAcoustic", "options": {}})
        with pytest.raises(ComponentError, match="from_wav_files"):
            asyncio.run(harness.run())


def test_cli_repeat_writes_one_row_per_run(tmp_path: Path) -> None:
    suite = {"name": "cli", "tasks": [mini_suite().tasks[0].model_dump()]}
    suite_path = tmp_path / "suite.json"
    suite_path.write_text(json.dumps(suite))
    assert main(["--suite", str(suite_path), "--output", str(tmp_path / "out"), "--repeat", "2"]) == 0
    (csv_path,) = (tmp_path / "out").glob("results_*.csv")
    assert pd.read_csv(csv_path)["repetition"].tolist() == [0, 1]
