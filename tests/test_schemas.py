"""Schema parsing and planner validation."""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import ClassVar

import pytest
from pydantic import ValidationError

from clavix.core.planner import MockLLMClient, Planner, PlanningError, build_messages, extract_json
from clavix.schemas.actions import (
    ActionName,
    ActionStep,
    ActionType,
    ClickElementPayload,
    ExecutionPlan,
    LaunchUrlPayload,
    PostconditionKind,
    RunCommandPayload,
    parse_postcondition,
)
from clavix.schemas.telemetry import BenchmarkRecord, StepTelemetry, TaskOutcome


def step(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "step_id": 1,
        "action_type": "DETERMINISTIC",
        "action_name": "run_command",
        "payload": {"cmd": "echo hi"},
    }
    base.update(overrides)
    return base


class TestActionStep:
    def test_payload_defaults_are_filled(self) -> None:
        s = ActionStep.model_validate(step())
        assert s.payload == {"cmd": "echo hi", "timeout_sec": 10, "detach": False}
        assert isinstance(s.typed_payload(), RunCommandPayload)

    def test_url_is_validated_and_normalised(self) -> None:
        s = ActionStep.model_validate(step(action_name="launch_url", payload={"url": "https://github.com"}))
        payload = s.typed_payload()
        assert isinstance(payload, LaunchUrlPayload)
        assert payload.url.host == "github.com"
        json.dumps(s.model_dump(mode="json"))  # round-trips as plain JSON

    @pytest.mark.parametrize(
        "overrides",
        [
            {"action_name": "launch_url", "payload": {"url": "not a url"}},
            {"payload": {"cmd": "echo", "unexpected": 1}},
            {"payload": {"cmd": ""}},
            {"payload": {"cmd": "x", "timeout_sec": 0}},
            {"action_name": "open_portal", "payload": {}},
            {"action_name": "click_element", "payload": {"target_label": "OK"}},  # wrong type
            {"action_type": "FEEDBACK"},  # run_command can't be FEEDBACK
            {"expected_postcondition": "window_title:GitHub"},
            {"expected_postcondition": "exit_code:zero"},
            {"step_id": -1},
        ],
    )
    def test_invalid_steps_are_rejected(self, overrides: dict[str, object]) -> None:
        with pytest.raises(ValidationError):
            ActionStep.model_validate(step(**overrides))

    def test_click_element_coord_tuple(self) -> None:
        s = ActionStep.model_validate(
            step(
                action_type="VISUAL_FALLBACK",
                action_name="click_element",
                payload={"target_label": "OK", "fallback_coord": [10, 20]},
            )
        )
        payload = s.typed_payload()
        assert isinstance(payload, ClickElementPayload)
        assert payload.fallback_coord == (10, 20)

    def test_screenshot_region_must_have_positive_extent(self) -> None:
        with pytest.raises(ValidationError):
            ActionStep.model_validate(
                step(
                    action_name="take_screenshot",
                    payload={"filepath": "a.png", "target_region": [0, 0, 0, 10]},
                )
            )

    def test_blank_postcondition_becomes_none(self) -> None:
        assert ActionStep.model_validate(step(expected_postcondition="  ")).expected_postcondition is None


class TestPostcondition:
    def test_parse(self) -> None:
        assert parse_postcondition("window_title_contains: GitHub") == (
            PostconditionKind.WINDOW_TITLE_CONTAINS,
            "GitHub",
        )

    def test_path_argument_keeps_colons(self) -> None:
        assert parse_postcondition("file_exists:C:/tmp/a.txt") == (
            PostconditionKind.FILE_EXISTS,
            "C:/tmp/a.txt",
        )

    @pytest.mark.parametrize("expr", ["GitHub", "file_exists:", "nope:x"])
    def test_malformed(self, expr: str) -> None:
        with pytest.raises(ValueError):
            parse_postcondition(expr)


class TestExecutionPlan:
    def test_generates_uuid4(self) -> None:
        plan = ExecutionPlan(original_prompt="x", steps=[ActionStep.model_validate(step())])
        assert uuid.UUID(plan.plan_id).version == 4

    def test_rejects_non_uuid4(self) -> None:
        with pytest.raises(ValidationError):
            ExecutionPlan(plan_id=str(uuid.uuid1()), original_prompt="x", steps=[step()])

    def test_rejects_duplicate_or_unordered_ids(self) -> None:
        with pytest.raises(ValidationError):
            ExecutionPlan(original_prompt="x", steps=[step(), step()])
        with pytest.raises(ValidationError):
            ExecutionPlan(original_prompt="x", steps=[step(step_id=2), step(step_id=1)])

    def test_rejects_empty_plan(self) -> None:
        with pytest.raises(ValidationError):
            ExecutionPlan(original_prompt="x", steps=[])

    def test_json_round_trip(self) -> None:
        plan = ExecutionPlan(
            original_prompt="open github",
            steps=[step(action_name="launch_url", payload={"url": "https://github.com"})],
        )
        assert ExecutionPlan.model_validate_json(plan.model_dump_json()) == plan


class TestTelemetry:
    def test_step_durations(self) -> None:
        t = StepTelemetry(
            step_id=1, t_dispatch_start=1.0, t_execution_end=1.25, t_verification_end=1.5, success=True
        )
        assert t.execution_ms == pytest.approx(250.0)
        assert t.verification_ms == pytest.approx(250.0)
        assert "execution_ms" in t.model_dump()

    def test_benchmark_record_from_outcome(self) -> None:
        outcome = TaskOutcome(
            final_state="COMPLETED",
            success=True,
            t_asr_ms=10,
            t_llm_planning_ms=20,
            t_total_execution_ms=30,
            recovery_attempts=1,
        )
        rec = outcome.to_benchmark_record("T1-01", 1, planner="mock")
        assert rec.task_success and rec.recovery_attempts == 1 and rec.planner == "mock"

    def test_tier_bounds(self) -> None:
        with pytest.raises(ValidationError):
            BenchmarkRecord(
                task_id="x",
                tier=4,
                t_asr_ms=0,
                t_llm_planning_ms=0,
                t_total_execution_ms=0,
                task_success=True,
                recovery_attempts=0,
            )


class TestPlanner:
    PLAN: ClassVar[dict[str, object]] = {
        "steps": [step(action_name="launch_url", payload={"url": "https://github.com"})]
    }

    def test_valid_reply(self) -> None:
        planner = Planner(MockLLMClient([self.PLAN]))
        plan = asyncio.run(planner.plan("open github"))
        assert plan.original_prompt == "open github"
        assert plan.steps[0].action_name is ActionName.LAUNCH_URL

    def test_fenced_reply(self) -> None:
        raw = "```json\n" + json.dumps(self.PLAN) + "\n```"
        assert extract_json(raw) == self.PLAN

    def test_repairs_invalid_reply(self) -> None:
        llm = MockLLMClient(["not json", {"steps": [step(action_type="FEEDBACK")]}, self.PLAN])
        plan = asyncio.run(Planner(llm, max_retries=2).plan("open github"))
        assert plan.steps[0].action_type is ActionType.DETERMINISTIC
        assert len(llm.calls) == 3
        # The validation error is fed back to the model.
        assert "not a valid plan" in llm.calls[-1][-1]["content"]

    def test_gives_up_after_retries(self) -> None:
        llm = MockLLMClient(["{}", "{}"])
        with pytest.raises(PlanningError):
            asyncio.run(Planner(llm, max_retries=1).plan("x"))

    def test_model_cannot_set_plan_id(self) -> None:
        reply = {"plan_id": str(uuid.uuid4()), **self.PLAN}
        with pytest.raises(PlanningError):
            asyncio.run(Planner(MockLLMClient([reply]), max_retries=0).plan("x"))

    def test_backend_error_is_planning_error(self) -> None:
        def boom(_messages: object) -> str:
            raise ConnectionError("ollama down")

        with pytest.raises(PlanningError, match="ollama down"):
            asyncio.run(Planner(MockLLMClient(responder=boom)).plan("x"))

    def test_few_shot_examples_never_repeat_a_benchmark_command(self) -> None:
        from clavix.config import resolve_repo_path
        from clavix.core.planner import FEW_SHOT_EXAMPLES

        suite = json.loads(resolve_repo_path("configs/benchmark_suite.json").read_text(encoding="utf-8"))
        benchmark = {" ".join(task["command"].casefold().split()) for task in suite["tasks"]}
        leaked = [cmd for cmd, _ in FEW_SHOT_EXAMPLES if " ".join(cmd.casefold().split()) in benchmark]
        assert leaked == [], "few-shot examples must not reveal benchmark answers"

    def test_prompt_contains_few_shot_and_command(self) -> None:
        messages = build_messages("open github")
        assert messages[0]["role"] == "system"
        assert messages[-1] == {"role": "user", "content": "open github"}
        assert any(m["role"] == "assistant" for m in messages)
        for m in messages:
            if m["role"] == "assistant":
                ExecutionPlan(original_prompt="x", **json.loads(m["content"]))  # examples are valid
