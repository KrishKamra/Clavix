"""Dispatch routing, verification and the full agent loop."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from clavix.core.state_machine import TRANSITIONS, AgentState, ClavixAgent, InvalidTransition
from clavix.core.verifier import StateVerifier
from clavix.execution.dispatcher import AbortRequested, CommandSafetyPolicy, Dispatcher
from clavix.execution.native_ops import run_cli
from clavix.modules.acoustic import ScriptedAcoustic
from clavix.modules.grounding import UIElement
from clavix.schemas.actions import ActionStep, ActionType, ExecutionResult
from clavix.schemas.telemetry import VerificationStatus
from clavix.testing.fakes import FakeGrounding, FakeNativeOps, RecordingFeedback


def mk(step_id: int = 1, **kw: object) -> ActionStep:
    data: dict[str, object] = {
        "step_id": step_id,
        "action_type": "DETERMINISTIC",
        "action_name": "run_command",
        "payload": {"cmd": "echo clavix"},
    }
    data.update(kw)
    return ActionStep.model_validate(data)


def plan(*steps: ActionStep) -> dict[str, object]:
    return {"steps": [s.model_dump(mode="json") for s in steps]}


class TestNativeOps:
    def test_run_cli_captures_stdout(self) -> None:
        result = run_cli("echo clavix")
        assert result.success and result.return_code == 0
        assert "clavix" in result.stdout

    def test_run_cli_nonzero_exit(self) -> None:
        result = run_cli("exit 3")
        assert not result.success and result.return_code == 3

    def test_run_cli_timeout(self) -> None:
        cmd = f'"{sys.executable}" -c "import time; time.sleep(5)"'
        result = run_cli(cmd, timeout_sec=1)
        assert not result.success
        assert "timed out" in (result.error_message or "")

    def test_take_screenshot_with_region(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from PIL import Image, ImageGrab

        from clavix.execution import native_ops

        seen = {}

        def fake_grab(bbox=None):  # type: ignore[no-untyped-def]
            seen["bbox"] = bbox
            return Image.new("RGB", (bbox[2] - bbox[0], bbox[3] - bbox[1]))

        monkeypatch.setattr(ImageGrab, "grab", fake_grab)
        out = tmp_path / "sub" / "shot.png"
        result = native_ops.take_screenshot(str(out), (10, 20, 30, 40))
        assert result.success and out.exists()
        assert seen["bbox"] == (10, 20, 40, 60)
        assert result.details == {"width": 30, "height": 40}


class TestDispatcher:
    def run(self, coro):  # type: ignore[no-untyped-def]
        return asyncio.run(coro)

    def test_deterministic_url_routes_to_native(self, dispatcher: Dispatcher, native: FakeNativeOps) -> None:
        step = mk(action_name="launch_url", payload={"url": "https://github.com"})
        result = self.run(dispatcher.dispatch(step))
        assert result.success and result.route is ActionType.DETERMINISTIC
        assert native.calls == [("launch_url", ("https://github.com/",))]

    def test_detached_command_spawns(self, dispatcher: Dispatcher, native: FakeNativeOps) -> None:
        result = self.run(dispatcher.dispatch(mk(payload={"cmd": "notepad", "detach": True})))
        assert result.pid == 4242
        assert native.calls[0][0] == "spawn_process"

    def test_safety_policy_blocks(self, dispatcher: Dispatcher, native: FakeNativeOps) -> None:
        result = self.run(dispatcher.dispatch(mk(payload={"cmd": "shutdown /s /t 0"})))
        assert not result.success and "safety policy" in (result.error_message or "")
        assert native.calls == []

    def test_grounding_resolves_element(self, dispatcher: Dispatcher, grounding: FakeGrounding) -> None:
        grounding.elements["File"] = UIElement("File", "MenuItem", (0, 0, 40, 20))
        step = mk(action_type="OS_GROUNDING", action_name="click_element", payload={"target_label": "File"})
        result = self.run(dispatcher.dispatch(step))
        assert result.success and result.route is ActionType.OS_GROUNDING and not result.fallback_used
        assert grounding.invoked == ["File"] and grounding.clicks == []

    def test_grounding_falls_back_to_coordinates(
        self, dispatcher: Dispatcher, grounding: FakeGrounding
    ) -> None:
        step = mk(
            action_type="OS_GROUNDING",
            action_name="click_element",
            payload={"target_label": "Missing", "fallback_coord": [5, 6]},
        )
        result = self.run(dispatcher.dispatch(step))
        assert result.success and result.fallback_used
        assert result.route is ActionType.VISUAL_FALLBACK
        assert grounding.clicks == [(5, 6)]

    def test_unresolved_without_coord_fails(self, dispatcher: Dispatcher) -> None:
        step = mk(
            action_type="OS_GROUNDING", action_name="click_element", payload={"target_label": "Missing"}
        )
        result = self.run(dispatcher.dispatch(step))
        assert not result.success and not result.fallback_used

    def test_visual_fallback_disabled(self, grounding: FakeGrounding, feedback: RecordingFeedback) -> None:
        d = Dispatcher(grounding, feedback, FakeNativeOps(), enable_visual_fallback=False)
        step = mk(
            action_type="VISUAL_FALLBACK",
            action_name="click_element",
            payload={"target_label": "OK", "fallback_coord": [1, 2]},
        )
        result = self.run(d.dispatch(step))
        assert not result.success and "disabled" in (result.error_message or "")
        assert grounding.clicks == []

    def test_feedback_routes_to_feedback_module(
        self, dispatcher: Dispatcher, feedback: RecordingFeedback
    ) -> None:
        step = mk(action_type="FEEDBACK", action_name="speak_feedback", payload={"message": "hi"})
        assert self.run(dispatcher.dispatch(step)).success
        assert feedback.messages == ["hi"]

    def test_abort(self, dispatcher: Dispatcher) -> None:
        dispatcher.abort()
        with pytest.raises(AbortRequested):
            self.run(dispatcher.dispatch(mk()))
        dispatcher.reset_abort()
        assert self.run(dispatcher.dispatch(mk())).success

    def test_failsafe_exception_aborts(self, feedback: RecordingFeedback) -> None:
        class FailSafeException(Exception):
            pass

        class Cornered(FakeGrounding):
            async def click_coordinate(self, x: int, y: int) -> bool:
                raise FailSafeException("corner")

        d = Dispatcher(Cornered(), feedback, FakeNativeOps(), enable_visual_fallback=True)
        step = mk(
            action_type="VISUAL_FALLBACK",
            action_name="click_element",
            payload={"target_label": "OK", "fallback_coord": [1, 2]},
        )
        with pytest.raises(AbortRequested):
            self.run(d.dispatch(step))
        assert d.aborted

    def test_safety_policy_patterns(self) -> None:
        policy = CommandSafetyPolicy([r"\brm\s+-[a-z]*r[a-z]*f"])
        assert policy.check("rm -rf /") is not None
        assert policy.check("echo rm") is None


class TestVerifier:
    def verify(self, verifier: StateVerifier, step: ActionStep, result: ExecutionResult):  # type: ignore[no-untyped-def]
        return asyncio.run(verifier.verify(step, result))

    ok = ExecutionResult(success=True, route=ActionType.DETERMINISTIC, return_code=0, stdout="Clavix\n")

    @pytest.mark.parametrize(
        ("postcondition", "status"),
        [
            (None, VerificationStatus.SKIPPED),
            ("window_title_contains:github", VerificationStatus.PASSED),
            ("window_title_contains:Spotify", VerificationStatus.FAILED),
            ("process_running:notepad", VerificationStatus.PASSED),
            ("process_running:chrome", VerificationStatus.FAILED),
            ("exit_code:0", VerificationStatus.PASSED),
            ("exit_code:1", VerificationStatus.FAILED),
            ("stdout_contains:clavix", VerificationStatus.PASSED),
        ],
    )
    def test_predicates(
        self, verifier: StateVerifier, postcondition: str | None, status: VerificationStatus
    ) -> None:
        assert self.verify(verifier, mk(expected_postcondition=postcondition), self.ok).status is status

    def test_file_exists_polls_until_created(self, tmp_path: Path) -> None:
        target = tmp_path / "late.txt"
        verifier = StateVerifier(timeout_sec=2.0, poll_interval_sec=0.05)

        async def scenario() -> VerificationStatus:
            async def create_later() -> None:
                await asyncio.sleep(0.2)
                target.write_text("x")

            task = asyncio.create_task(create_later())
            res = await verifier.verify(mk(expected_postcondition=f"file_exists:{target}"), self.ok)
            await task
            return res.status

        assert asyncio.run(scenario()) is VerificationStatus.PASSED

    def test_file_exists_expands_env_vars(
        self, verifier: StateVerifier, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "f.txt").write_text("x")
        monkeypatch.setenv("CLAVIX_TEST_DIR", str(tmp_path))
        token = "%CLAVIX_TEST_DIR%" if sys.platform == "win32" else "$CLAVIX_TEST_DIR"
        step = mk(expected_postcondition=f"file_exists:{token}/f.txt")
        assert self.verify(verifier, step, self.ok).status is VerificationStatus.PASSED

    def test_execution_failure_fails_verification(self, verifier: StateVerifier) -> None:
        bad = ExecutionResult(success=False, route=ActionType.DETERMINISTIC, error_message="boom")
        res = self.verify(verifier, mk(), bad)
        assert res.status is VerificationStatus.FAILED and "boom" in res.detail

    def test_unsupported_platform(self) -> None:
        def no_windows() -> list[str]:
            raise NotImplementedError("no window enumeration")

        v = StateVerifier(timeout_sec=0.1, window_titles=no_windows)
        res = self.verify(v, mk(expected_postcondition="window_title_contains:x"), self.ok)
        assert res.status is VerificationStatus.UNSUPPORTED and not res.passed


class TestAgentLoop:
    def test_full_loop_from_mock_command_to_verified_execution(
        self, make_agent, feedback: RecordingFeedback, tmp_path: Path
    ) -> None:
        out = tmp_path / "hello.txt"
        agent, _ = make_agent(
            plan(
                mk(1, payload={"cmd": "echo clavix"}, expected_postcondition="stdout_contains:clavix"),
                mk(
                    2,
                    payload={"cmd": f'echo hello> "{out.as_posix()}"'},
                    expected_postcondition=f"file_exists:{out.as_posix()}",
                ),
                mk(3, action_type="FEEDBACK", action_name="speak_feedback", payload={"message": "written"}),
            )
        )
        outcome = asyncio.run(agent.run("say clavix and write hello.txt"))

        assert outcome.success, outcome.error_message
        assert outcome.final_state == AgentState.COMPLETED
        assert out.read_text().strip() == "hello"
        assert outcome.state_trace == [
            "IDLE",
            "ACOUSTIC_INGESTION",
            "PLANNING",
            "DISPATCHING",
            "VERIFYING",
            "DISPATCHING",
            "VERIFYING",
            "DISPATCHING",
            "VERIFYING",
            "FEEDBACK",
            "COMPLETED",
        ]
        assert [t.step_id for t in outcome.steps] == [1, 2, 3]
        assert all(t.t_dispatch_start <= t.t_execution_end <= t.t_verification_end for t in outcome.steps)
        assert outcome.steps[0].verification is VerificationStatus.PASSED
        assert outcome.t_llm_planning_ms >= 0 and outcome.t_total_execution_ms > 0
        assert feedback.messages == ["written", "Done. Completed 3 steps."]
        assert agent.state is AgentState.COMPLETED

    def test_failed_verification_retries_then_fails(self, make_agent, native: FakeNativeOps) -> None:
        agent, _ = make_agent(
            plan(mk(1, expected_postcondition="stdout_contains:never-printed"), mk(2)),
            max_step_retries=2,
        )
        outcome = asyncio.run(agent.run("x"))
        assert not outcome.success and outcome.final_state == AgentState.FAILED
        assert outcome.recovery_attempts == 2
        assert [t.attempt for t in outcome.steps] == [0, 1, 2]
        assert len(native.calls) == 3  # step 2 never ran
        assert "step 1" in (outcome.error_message or "")

    def test_non_retryable_failure_is_not_retried(self, make_agent, grounding: FakeGrounding) -> None:
        agent, _ = make_agent(
            plan(
                mk(
                    1,
                    action_type="OS_GROUNDING",
                    action_name="click_element",
                    payload={"target_label": "Missing"},
                )
            ),
            max_step_retries=3,
        )
        outcome = asyncio.run(agent.run("click missing"))
        assert not outcome.success
        assert len(outcome.steps) == 1 and outcome.recovery_attempts == 0
        assert outcome.results[1].retryable is False

    def test_safety_block_is_not_retried(self, make_agent, native: FakeNativeOps) -> None:
        agent, _ = make_agent(plan(mk(1, payload={"cmd": "shutdown /s"})), max_step_retries=3)
        outcome = asyncio.run(agent.run("x"))
        assert not outcome.success and len(outcome.steps) == 1 and native.calls == []

    def test_fallback_counts_as_recovery(self, make_agent) -> None:
        agent, _ = make_agent(
            plan(
                mk(
                    1,
                    action_type="OS_GROUNDING",
                    action_name="click_element",
                    payload={"target_label": "Nope", "fallback_coord": [1, 1]},
                )
            )
        )
        outcome = asyncio.run(agent.run("click nope"))
        assert outcome.success and outcome.recovery_attempts == 1
        assert outcome.steps[0].fallback_used

    def test_planning_failure_goes_through_feedback(self, make_agent, feedback: RecordingFeedback) -> None:
        agent, _ = make_agent("garbage", "garbage", planner_retries=1)
        outcome = asyncio.run(agent.run("x"))
        assert not outcome.success
        assert outcome.state_trace == ["IDLE", "ACOUSTIC_INGESTION", "PLANNING", "FEEDBACK", "FAILED"]
        assert feedback.messages[-1].startswith("Sorry")

    def test_abort_stops_task(self, make_agent, dispatcher: Dispatcher) -> None:
        agent, _ = make_agent(plan(mk(1)))
        dispatcher.abort()
        outcome = asyncio.run(agent.run("x"))
        assert not outcome.success and "aborted" in (outcome.error_message or "")

    def test_acoustic_ingestion(
        self, dispatcher: Dispatcher, verifier: StateVerifier, feedback: RecordingFeedback
    ) -> None:
        from clavix.core.planner import MockLLMClient, Planner

        llm = MockLLMClient([plan(mk(1))])
        agent = ClavixAgent(
            planner=Planner(llm),
            dispatcher=dispatcher,
            verifier=verifier,
            feedback=feedback,
            acoustic=ScriptedAcoustic(["echo it"], t_asr_ms=123.0),
        )
        outcome = asyncio.run(agent.run())
        assert outcome.success and outcome.transcript == "echo it" and outcome.t_asr_ms == 123.0
        assert llm.calls[0][-1]["content"] == "echo it"

    def test_agent_is_reusable(self, make_agent) -> None:
        agent, _ = make_agent(plan(mk(1)), plan(mk(1)))
        assert asyncio.run(agent.run("a")).success
        second = asyncio.run(agent.run("b"))
        assert second.success and second.state_trace[0] == "IDLE"

    def test_empty_command_fails(self, make_agent) -> None:
        agent, llm = make_agent()
        outcome = asyncio.run(agent.run("   "))
        assert not outcome.success and llm.calls == []

    def test_feedback_sees_every_transition(self, make_agent, feedback: RecordingFeedback) -> None:
        agent, _ = make_agent(plan(mk(1)))
        outcome = asyncio.run(agent.run("x"))
        assert feedback.states == [(t.from_state, t.to_state) for t in outcome.transitions]

    def test_progress_events_describe_a_successful_run(
        self, make_agent, feedback: RecordingFeedback, grounding: FakeGrounding
    ) -> None:
        click = mk(
            2, action_type="OS_GROUNDING", action_name="click_element", payload={"target_label": "File"}
        )
        agent, _ = make_agent(plan(mk(1, payload={"cmd": "notepad", "detach": True}), click))
        grounding.elements["File"] = UIElement("File", "MenuItem", (0, 0, 40, 20))
        outcome = asyncio.run(agent.run("open notepad and click the file menu"))
        assert outcome.success
        kinds = [(e["type"], e.get("id"), e.get("status")) for e in feedback.events]
        assert kinds == [
            ("transcript", None, None),
            ("plan", None, None),
            ("step", 1, "running"),
            ("step", 1, "ok"),
            ("step", 2, "running"),
            ("step", 2, "ok"),
            ("timing", None, None),
        ]
        assert feedback.events[0]["text"] == "open notepad and click the file menu"
        assert feedback.events[1]["steps"] == [
            {"id": 1, "action": "run_command", "verb": "Run", "target": "notepad"},
            {"id": 2, "action": "click_element", "verb": "Click", "target": "File"},
        ]
        assert feedback.events[-1] == {
            "type": "timing",
            "asr_ms": outcome.t_asr_ms,
            "llm_ms": outcome.t_llm_planning_ms,
            "exec_ms": outcome.t_total_execution_ms,
            "success": True,
        }

    def test_progress_events_report_retries_and_failure(
        self, make_agent, feedback: RecordingFeedback
    ) -> None:
        agent, _ = make_agent(
            plan(mk(1, expected_postcondition="stdout_contains:never-printed")), max_step_retries=1
        )
        outcome = asyncio.run(agent.run("x"))
        assert not outcome.success
        steps = [(e["status"], e["attempt"]) for e in feedback.events if e["type"] == "step"]
        assert steps == [("running", 0), ("retry", 0), ("running", 1), ("failed", 1)]
        assert feedback.events[-1]["type"] == "timing" and feedback.events[-1]["success"] is False

    def test_a_failing_event_hook_does_not_break_the_run(
        self, make_agent, feedback: RecordingFeedback
    ) -> None:
        async def broken(event: object) -> None:
            raise RuntimeError("HUD crashed")

        feedback.on_event = broken  # type: ignore[method-assign]
        agent, _ = make_agent(plan(mk(1)))
        assert asyncio.run(agent.run("x")).success

    def test_transition_table_is_closed(self) -> None:
        for state, targets in TRANSITIONS.items():
            assert targets, state
            assert targets <= set(AgentState)

    def test_illegal_transition_raises(self, make_agent) -> None:
        agent, _ = make_agent()
        with pytest.raises(InvalidTransition):
            asyncio.run(agent._transition(AgentState.VERIFYING))
