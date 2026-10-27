"""Action data contracts shared by the planner, dispatcher and verifier.

An :class:`ExecutionPlan` is an ordered list of :class:`ActionStep` objects. Each
step names an action (``action_name``), the execution path it must take
(``action_type``) and a raw ``payload`` dict. The payload is validated against
the typed model registered for that action in :data:`PAYLOAD_MODELS`, so a plan
that parses is guaranteed to be dispatchable.
"""

from __future__ import annotations

import uuid
from enum import StrEnum
from typing import Any, Optional, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    field_validator,
    model_validator,
)


class ActionType(StrEnum):
    """Execution path for a step, in priority order of the dual-path model."""

    DETERMINISTIC = "DETERMINISTIC"
    """Priority 1: native subprocesses, shell commands, protocol handlers."""

    OS_GROUNDING = "OS_GROUNDING"
    """Priority 2: accessibility-tree element resolution (UIA / AT-SPI)."""

    VISUAL_FALLBACK = "VISUAL_FALLBACK"
    """Priority 3: raw coordinate mouse/keyboard input via PyAutoGUI."""

    FEEDBACK = "FEEDBACK"
    """User-facing output (TTS, HUD) with no OS side effects."""


class ActionName(StrEnum):
    """The closed set of actions the planner may emit."""

    LAUNCH_URL = "launch_url"
    RUN_COMMAND = "run_command"
    CLICK_ELEMENT = "click_element"
    TAKE_SCREENSHOT = "take_screenshot"
    SPEAK_FEEDBACK = "speak_feedback"


class _Payload(BaseModel):
    """Base class for typed payloads; unknown keys are rejected."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class LaunchUrlPayload(_Payload):
    """Open a URL with the OS default protocol handler."""

    url: HttpUrl


class RunCommandPayload(_Payload):
    """Run a shell command.

    When ``detach`` is true the process is spawned without waiting for it
    (e.g. launching a GUI application); otherwise the call blocks up to
    ``timeout_sec`` and captures stdout/stderr.
    """

    cmd: str = Field(min_length=1)
    timeout_sec: int = Field(default=10, ge=1, le=600)
    detach: bool = False


class ClickElementPayload(_Payload):
    """Click a UI element by its accessible label, with optional raw coordinates."""

    target_label: str = Field(min_length=1)
    fallback_coord: Optional[tuple[int, int]] = None


class TakeScreenshotPayload(_Payload):
    """Capture the screen (or a region) to ``filepath``.

    ``target_region`` is ``(left, top, width, height)`` in screen pixels, matching
    the PyAutoGUI region convention.
    """

    filepath: str = Field(min_length=1)
    target_region: Optional[tuple[int, int, int, int]] = None

    @field_validator("target_region")
    @classmethod
    def _positive_extent(
        cls, region: Optional[tuple[int, int, int, int]]
    ) -> Optional[tuple[int, int, int, int]]:
        if region is not None and (region[2] <= 0 or region[3] <= 0):
            raise ValueError("target_region width and height must be positive")
        return region


class SpeakFeedbackPayload(_Payload):
    """Speak (or display) a message to the user."""

    message: str = Field(min_length=1)
    blocking: bool = False


ActionPayload: TypeAlias = (
    LaunchUrlPayload | RunCommandPayload | ClickElementPayload | TakeScreenshotPayload | SpeakFeedbackPayload
)

PAYLOAD_MODELS: dict[ActionName, type[_Payload]] = {
    ActionName.LAUNCH_URL: LaunchUrlPayload,
    ActionName.RUN_COMMAND: RunCommandPayload,
    ActionName.CLICK_ELEMENT: ClickElementPayload,
    ActionName.TAKE_SCREENSHOT: TakeScreenshotPayload,
    ActionName.SPEAK_FEEDBACK: SpeakFeedbackPayload,
}
"""Maps each action to the payload model that validates it."""

ALLOWED_ACTION_TYPES: dict[ActionName, frozenset[ActionType]] = {
    ActionName.LAUNCH_URL: frozenset({ActionType.DETERMINISTIC}),
    ActionName.RUN_COMMAND: frozenset({ActionType.DETERMINISTIC}),
    ActionName.TAKE_SCREENSHOT: frozenset({ActionType.DETERMINISTIC}),
    ActionName.CLICK_ELEMENT: frozenset({ActionType.OS_GROUNDING, ActionType.VISUAL_FALLBACK}),
    ActionName.SPEAK_FEEDBACK: frozenset({ActionType.FEEDBACK}),
}
"""Maps each action to the execution paths it may be routed through."""


class PostconditionKind(StrEnum):
    """Supported ``expected_postcondition`` predicates (``"<kind>:<argument>"``)."""

    WINDOW_TITLE_CONTAINS = "window_title_contains"
    PROCESS_RUNNING = "process_running"
    FILE_EXISTS = "file_exists"
    EXIT_CODE = "exit_code"
    STDOUT_CONTAINS = "stdout_contains"


def parse_postcondition(expr: str) -> tuple[PostconditionKind, str]:
    """Split a postcondition expression into its kind and argument.

    Args:
        expr: An expression such as ``"window_title_contains:GitHub"``.

    Returns:
        The parsed kind and the (unstripped beyond outer whitespace) argument.

    Raises:
        ValueError: If the expression is malformed or the kind is unknown.
    """
    kind, sep, arg = expr.partition(":")
    if not sep or not arg.strip():
        raise ValueError(f"postcondition must look like '<kind>:<argument>', got {expr!r}")
    try:
        parsed_kind = PostconditionKind(kind.strip())
    except ValueError:
        allowed = ", ".join(k.value for k in PostconditionKind)
        raise ValueError(f"unknown postcondition kind {kind!r}; expected one of: {allowed}") from None
    if parsed_kind is PostconditionKind.EXIT_CODE:
        try:
            int(arg.strip())
        except ValueError:
            raise ValueError(f"exit_code postcondition needs an integer, got {arg!r}") from None
    return parsed_kind, arg.strip()


class ActionStep(BaseModel):
    """A single executable step of a plan."""

    model_config = ConfigDict(extra="forbid")

    step_id: int = Field(ge=0)
    action_type: ActionType
    action_name: ActionName
    payload: dict[str, Any] = Field(default_factory=dict)
    expected_postcondition: Optional[str] = None

    @field_validator("expected_postcondition")
    @classmethod
    def _check_postcondition(cls, expr: Optional[str]) -> Optional[str]:
        if expr is None or not expr.strip():
            return None
        parse_postcondition(expr)
        return expr.strip()

    @model_validator(mode="after")
    def _check_payload(self) -> ActionStep:
        allowed = ALLOWED_ACTION_TYPES[self.action_name]
        if self.action_type not in allowed:
            names = ", ".join(sorted(t.value for t in allowed))
            raise ValueError(
                f"action '{self.action_name}' cannot use action_type '{self.action_type}'; allowed: {names}"
            )
        typed = PAYLOAD_MODELS[self.action_name].model_validate(self.payload)
        # Store the normalised, JSON-safe form so plans round-trip cleanly.
        self.payload = typed.model_dump(mode="json")
        return self

    def typed_payload(self) -> ActionPayload:
        """Return the payload parsed into its action-specific model."""
        return PAYLOAD_MODELS[self.action_name].model_validate(self.payload)  # type: ignore[return-value]


class ExecutionPlan(BaseModel):
    """An ordered, validated plan produced from one user command."""

    model_config = ConfigDict(extra="forbid")

    plan_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    original_prompt: str
    steps: list[ActionStep] = Field(min_length=1)

    @field_validator("plan_id")
    @classmethod
    def _check_uuid4(cls, value: str) -> str:
        try:
            parsed = uuid.UUID(value)
        except ValueError:
            raise ValueError(f"plan_id must be a UUID4 string, got {value!r}") from None
        if parsed.version != 4:
            raise ValueError(f"plan_id must be a UUID4, got version {parsed.version}")
        return str(parsed)

    @field_validator("steps")
    @classmethod
    def _check_step_order(cls, steps: list[ActionStep]) -> list[ActionStep]:
        ids = [s.step_id for s in steps]
        if len(set(ids)) != len(ids):
            raise ValueError(f"step_id values must be unique, got {ids}")
        if ids != sorted(ids):
            raise ValueError(f"steps must be ordered by step_id, got {ids}")
        return steps


class ExecutionResult(BaseModel):
    """Outcome of dispatching one step, consumed by the verifier."""

    success: bool
    route: ActionType
    pid: Optional[int] = None
    return_code: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    artifact_path: Optional[str] = None
    fallback_used: bool = False
    error_message: Optional[str] = None
    retryable: bool = True
    """False when no OS action was attempted and re-dispatching cannot change the
    outcome (blocked by policy, disabled path, element not found after the
    grounding module's own wait). The agent skips retries for such failures."""
    details: dict[str, Any] = Field(default_factory=dict)
