"""Telemetry contracts: per-step timings, verification states and benchmark records.

All ``t_*`` timestamps are ``time.perf_counter()`` readings in seconds. They are
only meaningful relative to each other within one process; durations derived
from them are reported in milliseconds (``*_ms``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Optional

from pydantic import BaseModel, Field, computed_field

from clavix.schemas.actions import ActionType, ExecutionPlan, ExecutionResult


class VerificationStatus(StrEnum):
    """Result of asserting a step's postcondition."""

    PASSED = "PASSED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    """The step declared no postcondition; execution success is accepted."""
    UNSUPPORTED = "UNSUPPORTED"
    """The postcondition cannot be checked on this platform (counts as failure)."""


class VerificationResult(BaseModel):
    """Outcome of the state verifier for one step attempt."""

    status: VerificationStatus
    postcondition: Optional[str] = None
    detail: str = ""

    @property
    def passed(self) -> bool:
        """True when the step may be considered successful."""
        return self.status in (VerificationStatus.PASSED, VerificationStatus.SKIPPED)


class StepTelemetry(BaseModel):
    """Timing and outcome of one dispatch + verify attempt of a step."""

    step_id: int
    t_dispatch_start: float
    t_execution_end: float
    t_verification_end: float
    success: bool
    error_message: Optional[str] = None
    attempt: int = 0
    action_name: Optional[str] = None
    route: Optional[ActionType] = None
    fallback_used: bool = False
    verification: Optional[VerificationStatus] = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def execution_ms(self) -> float:
        """Dispatch-to-execution-end latency in milliseconds."""
        return (self.t_execution_end - self.t_dispatch_start) * 1000.0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def verification_ms(self) -> float:
        """Time spent in the state verifier in milliseconds."""
        return (self.t_verification_end - self.t_execution_end) * 1000.0


class StateTransition(BaseModel):
    """One edge taken by the agent state machine."""

    from_state: str
    to_state: str
    t: float


class TaskOutcome(BaseModel):
    """Everything the agent observed while handling one command."""

    transcript: str = ""
    plan: Optional[ExecutionPlan] = None
    final_state: str
    success: bool
    error_message: Optional[str] = None
    t_asr_ms: float = 0.0
    t_llm_planning_ms: float = 0.0
    t_total_execution_ms: float = 0.0
    recovery_attempts: int = 0
    steps: list[StepTelemetry] = Field(default_factory=list)
    results: dict[int, ExecutionResult] = Field(default_factory=dict)
    """Final execution result per ``step_id`` (last attempt wins)."""
    transitions: list[StateTransition] = Field(default_factory=list)

    @property
    def state_trace(self) -> list[str]:
        """Ordered list of states visited, starting with the initial state."""
        if not self.transitions:
            return []
        return [self.transitions[0].from_state] + [t.to_state for t in self.transitions]

    def to_benchmark_record(self, task_id: str, tier: int, **extra: Any) -> BenchmarkRecord:
        """Flatten this outcome into a :class:`BenchmarkRecord`.

        Args:
            task_id: Benchmark task identifier.
            tier: Task tier (1-3).
            **extra: Overrides for optional record fields (e.g. ``planner``).
        """
        fields: dict[str, Any] = {
            "task_id": task_id,
            "tier": tier,
            "t_asr_ms": self.t_asr_ms,
            "t_llm_planning_ms": self.t_llm_planning_ms,
            "t_total_execution_ms": self.t_total_execution_ms,
            "task_success": self.success,
            "recovery_attempts": self.recovery_attempts,
            "n_steps": len(self.plan.steps) if self.plan else 0,
            "error_message": self.error_message,
        }
        fields.update(extra)
        return BenchmarkRecord(**fields)


class BenchmarkRecord(BaseModel):
    """One row of benchmark output (one task run)."""

    task_id: str
    tier: int = Field(ge=1, le=3)
    """1: Deterministic, 2: Sequential, 3: UI-Grounding."""
    t_asr_ms: float
    t_llm_planning_ms: float
    t_total_execution_ms: float
    task_success: bool
    recovery_attempts: int
    n_steps: int = 0
    planner: str = ""
    error_message: Optional[str] = None
    timestamp: str = Field(default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds"))
