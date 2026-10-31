"""Event-driven agent loop: Ingest -> Plan -> Execute -> Verify -> Feedback.

State graph (every failure path routes through FEEDBACK so the user is told)::

    IDLE -> ACOUSTIC_INGESTION -> PLANNING -> DISPATCHING <-> VERIFYING
                    |                 |            |              |
                    +-----------------+------------+--------------+--> FEEDBACK
    FEEDBACK -> COMPLETED | FAILED -> IDLE (next run)

``DISPATCHING -> VERIFYING`` happens once per step attempt; ``VERIFYING ->
DISPATCHING`` starts the next step or a retry of the current one.
"""

from __future__ import annotations

import inspect
import logging
import time
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Any

from clavix.config import ClavixConfig
from clavix.core.planner import Planner, PlanningError
from clavix.core.verifier import StateVerifier
from clavix.execution.dispatcher import AbortRequested, Dispatcher
from clavix.modules.acoustic import AcousticInterface, Transcript
from clavix.modules.feedback import AgentEvent, FeedbackInterface
from clavix.schemas.actions import ActionStep, ExecutionResult
from clavix.schemas.telemetry import StateTransition, StepTelemetry, TaskOutcome

logger = logging.getLogger(__name__)


class AgentState(StrEnum):
    """States of the agent loop."""

    IDLE = "IDLE"
    ACOUSTIC_INGESTION = "ACOUSTIC_INGESTION"
    PLANNING = "PLANNING"
    DISPATCHING = "DISPATCHING"
    VERIFYING = "VERIFYING"
    FEEDBACK = "FEEDBACK"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


TRANSITIONS: dict[AgentState, frozenset[AgentState]] = {
    AgentState.IDLE: frozenset({AgentState.ACOUSTIC_INGESTION}),
    AgentState.ACOUSTIC_INGESTION: frozenset({AgentState.PLANNING, AgentState.FEEDBACK}),
    AgentState.PLANNING: frozenset({AgentState.DISPATCHING, AgentState.FEEDBACK}),
    AgentState.DISPATCHING: frozenset({AgentState.VERIFYING, AgentState.FEEDBACK}),
    AgentState.VERIFYING: frozenset({AgentState.DISPATCHING, AgentState.FEEDBACK}),
    AgentState.FEEDBACK: frozenset({AgentState.COMPLETED, AgentState.FAILED}),
    AgentState.COMPLETED: frozenset({AgentState.IDLE}),
    AgentState.FAILED: frozenset({AgentState.IDLE}),
}
"""Legal transitions; anything else raises :class:`InvalidTransition`."""

StateListener = Callable[[AgentState, AgentState], Awaitable[None] | None]

# How each action reads on a HUD: a verb plus the payload field that names its target.
_STEP_DISPLAY: dict[str, tuple[str, str]] = {
    "launch_url": ("Open", "url"),
    "run_command": ("Run", "cmd"),
    "click_element": ("Click", "target_label"),
    "take_screenshot": ("Screenshot", "filepath"),
    "speak_feedback": ("Say", "message"),
}


def describe_step(step: ActionStep) -> dict[str, Any]:
    """Summarise ``step`` for display, e.g. ``{"verb": "Click", "target": "File"}``."""
    verb, field = _STEP_DISPLAY.get(step.action_name, (step.action_name, ""))
    return {
        "id": step.step_id,
        "action": str(step.action_name),
        "verb": verb,
        "target": str(step.payload.get(field, "")),
    }


class InvalidTransition(RuntimeError):
    """Raised on an attempt to take an edge not in :data:`TRANSITIONS`."""


class TaskFailure(RuntimeError):
    """A task-level failure that should be reported to the user."""


class ClavixAgent:
    """Runs one command at a time through the full closed loop."""

    def __init__(
        self,
        *,
        planner: Planner,
        dispatcher: Dispatcher,
        verifier: StateVerifier,
        feedback: FeedbackInterface,
        acoustic: AcousticInterface | None = None,
        max_step_retries: int = 1,
        speak_summary: bool = True,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        """
        Args:
            planner: Command -> plan.
            dispatcher: Step -> OS effect.
            verifier: Postcondition checker.
            feedback: TTS / HUD sink; also notified of state changes.
            acoustic: Speech source used when :meth:`run` gets no text command.
            max_step_retries: Re-dispatches allowed per step after a failed verification.
            speak_summary: Announce the task result in the FEEDBACK state.
            clock: Monotonic clock in seconds (injectable for tests).
        """
        self._planner = planner
        self._dispatcher = dispatcher
        self._verifier = verifier
        self._feedback = feedback
        self._acoustic = acoustic
        self._max_step_retries = max_step_retries
        self._speak_summary = speak_summary
        self._clock = clock
        self._state = AgentState.IDLE
        self._listeners: list[StateListener] = []
        self._transitions: list[StateTransition] = []

    @classmethod
    def from_config(
        cls,
        config: ClavixConfig,
        *,
        planner: Planner,
        dispatcher: Dispatcher,
        feedback: FeedbackInterface,
        acoustic: AcousticInterface | None = None,
        verifier: StateVerifier | None = None,
    ) -> ClavixAgent:
        """Build an agent using the ``agent`` and ``verifier`` config sections."""
        return cls(
            planner=planner,
            dispatcher=dispatcher,
            verifier=verifier
            or StateVerifier(config.verifier.timeout_sec, config.verifier.poll_interval_sec),
            feedback=feedback,
            acoustic=acoustic,
            max_step_retries=config.agent.max_step_retries,
            speak_summary=config.agent.speak_summary,
        )

    @property
    def state(self) -> AgentState:
        """Current state."""
        return self._state

    def add_listener(self, listener: StateListener) -> None:
        """Register a sync or async ``(previous, current)`` transition callback."""
        self._listeners.append(listener)

    async def run(self, command: str | None = None) -> TaskOutcome:
        """Handle one command end to end.

        Args:
            command: Text command. If omitted, one utterance is taken from the
                acoustic module.

        Returns:
            The full outcome; failures are reported in it rather than raised.

        Raises:
            RuntimeError: If called while a run is in progress.
        """
        if self._state in (AgentState.COMPLETED, AgentState.FAILED):
            await self._transition(AgentState.IDLE)
        if self._state is not AgentState.IDLE:
            raise RuntimeError(f"agent is busy (state {self._state})")
        self._transitions = []

        outcome = TaskOutcome(final_state=AgentState.IDLE, success=False)
        try:
            await self._transition(AgentState.ACOUSTIC_INGESTION)
            transcript = await self._ingest(command)
            outcome.transcript = transcript.text
            outcome.t_asr_ms = transcript.t_asr_ms
            await self._emit(
                {
                    "type": "transcript",
                    "text": transcript.text,
                    "asr_ms": transcript.t_asr_ms,
                    "confidence": transcript.confidence,
                }
            )

            await self._transition(AgentState.PLANNING)
            t0 = self._clock()
            plan = await self._planner.plan(transcript.text)
            outcome.t_llm_planning_ms = (self._clock() - t0) * 1000.0
            outcome.plan = plan
            await self._emit(
                {
                    "type": "plan",
                    "steps": [describe_step(step) for step in plan.steps],
                    "llm_ms": outcome.t_llm_planning_ms,
                }
            )

            t_exec_start = self._clock()
            try:
                for step in plan.steps:
                    await self._execute_step(step, outcome)
            finally:
                outcome.t_total_execution_ms = (self._clock() - t_exec_start) * 1000.0
            outcome.success = True
        except (TaskFailure, PlanningError, AbortRequested, EOFError) as exc:
            outcome.error_message = str(exc)
        except Exception as exc:
            logger.exception("unexpected error in agent loop")
            outcome.error_message = f"{type(exc).__name__}: {exc}"

        await self._emit(
            {
                "type": "timing",
                "asr_ms": outcome.t_asr_ms,
                "llm_ms": outcome.t_llm_planning_ms,
                "exec_ms": outcome.t_total_execution_ms,
                "success": outcome.success,
            }
        )
        await self._transition(AgentState.FEEDBACK)
        if self._speak_summary:
            await self._announce(outcome)
        await self._transition(AgentState.COMPLETED if outcome.success else AgentState.FAILED)
        outcome.final_state = self._state
        outcome.transitions = list(self._transitions)
        return outcome

    async def _ingest(self, command: str | None) -> Transcript:
        if command is not None:
            transcript = Transcript(text=command)
        elif self._acoustic is not None:
            transcript = await self._acoustic.listen()
        else:
            raise TaskFailure("no command given and no acoustic module configured")
        if not transcript.text.strip():
            raise TaskFailure("empty command")
        return transcript

    async def _execute_step(self, step: ActionStep, outcome: TaskOutcome) -> None:
        """Dispatch and verify ``step``, retrying up to ``max_step_retries`` times.

        Failures whose result is not ``retryable`` are not retried.

        Raises:
            TaskFailure: If every attempt fails verification.
        """
        detail = ""
        for attempt in range(self._max_step_retries + 1):
            if attempt > 0:
                outcome.recovery_attempts += 1
            await self._transition(AgentState.DISPATCHING)
            await self._emit({"type": "step", "id": step.step_id, "status": "running", "attempt": attempt})
            t_dispatch = self._clock()
            result: ExecutionResult = await self._dispatcher.dispatch(step)
            t_exec = self._clock()
            if result.fallback_used:
                outcome.recovery_attempts += 1

            await self._transition(AgentState.VERIFYING)
            verification = await self._verifier.verify(step, result)
            t_verify = self._clock()

            detail = verification.detail
            outcome.results[step.step_id] = result
            outcome.steps.append(
                StepTelemetry(
                    step_id=step.step_id,
                    t_dispatch_start=t_dispatch,
                    t_execution_end=t_exec,
                    t_verification_end=t_verify,
                    success=verification.passed,
                    error_message=None if verification.passed else detail,
                    attempt=attempt,
                    action_name=step.action_name,
                    route=result.route,
                    fallback_used=result.fallback_used,
                    verification=verification.status,
                )
            )
            if verification.passed:
                await self._emit({"type": "step", "id": step.step_id, "status": "ok", "attempt": attempt})
                return
            logger.info("step %s attempt %s failed: %s", step.step_id, attempt, detail)
            if not result.retryable or attempt == self._max_step_retries:
                await self._emit({"type": "step", "id": step.step_id, "status": "failed", "attempt": attempt})
                break
            await self._emit({"type": "step", "id": step.step_id, "status": "retry", "attempt": attempt})
        raise TaskFailure(f"step {step.step_id} ({step.action_name}) failed: {detail}")

    async def _announce(self, outcome: TaskOutcome) -> None:
        if outcome.success:
            n = len(outcome.plan.steps) if outcome.plan else 0
            message = f"Done. Completed {n} step{'s' if n != 1 else ''}."
        else:
            message = f"Sorry, I couldn't do that: {outcome.error_message}"
        try:
            await self._feedback.speak(message)
        except Exception:
            logger.exception("feedback module failed to speak summary")

    async def _emit(self, event: AgentEvent) -> None:
        try:
            await self._feedback.on_event(event)
        except Exception:
            logger.exception("feedback module failed on %r event", event.get("type"))

    async def _transition(self, target: AgentState) -> None:
        if target not in TRANSITIONS[self._state]:
            raise InvalidTransition(f"{self._state} -> {target} is not allowed")
        previous, self._state = self._state, target
        self._transitions.append(StateTransition(from_state=previous, to_state=target, t=self._clock()))
        for listener in (self._feedback.on_state_change, *self._listeners):
            try:
                maybe = listener(previous, target)
                if inspect.isawaitable(maybe):
                    await maybe
            except Exception:
                logger.exception("state listener failed on %s -> %s", previous, target)
