"""Dual-path router from validated :class:`ActionStep` objects to OS effects.

Routing:
    * ``DETERMINISTIC``  -> :mod:`clavix.execution.native_ops`
    * ``OS_GROUNDING``   -> grounding module (accessibility tree), falling back to
      coordinates when the element cannot be resolved and fallback is enabled
    * ``VISUAL_FALLBACK``-> grounding module coordinate click (PyAutoGUI)
    * ``FEEDBACK``       -> feedback module

Fail-safe: :meth:`Dispatcher.abort` stops all further dispatches, and PyAutoGUI
``FAILSAFE`` is forced on before any coordinate input, so slamming the mouse
into a screen corner raises :class:`AbortRequested`.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
from collections.abc import Iterable

from clavix.execution.native_ops import NativeOps
from clavix.modules.feedback import FeedbackInterface
from clavix.modules.grounding import GroundingInterface
from clavix.schemas.actions import (
    ActionStep,
    ActionType,
    ClickElementPayload,
    ExecutionResult,
    LaunchUrlPayload,
    RunCommandPayload,
    SpeakFeedbackPayload,
    TakeScreenshotPayload,
)

logger = logging.getLogger(__name__)


class AbortRequested(RuntimeError):
    """Raised when the user (or PyAutoGUI fail-safe) aborts execution."""


class CommandSafetyPolicy:
    """Rejects shell commands matching any configured regex (case-insensitive)."""

    def __init__(self, blocked_patterns: Iterable[str] = ()) -> None:
        self._patterns = [re.compile(p, re.IGNORECASE) for p in blocked_patterns]

    def check(self, cmd: str) -> str | None:
        """Return the matching pattern if ``cmd`` is blocked, else ``None``."""
        for pattern in self._patterns:
            if pattern.search(cmd):
                return pattern.pattern
        return None


class Dispatcher:
    """Executes one step at a time along its declared execution path."""

    def __init__(
        self,
        grounding: GroundingInterface,
        feedback: FeedbackInterface,
        native: NativeOps | None = None,
        *,
        enable_visual_fallback: bool = False,
        safety: CommandSafetyPolicy | None = None,
    ) -> None:
        self._grounding = grounding
        self._feedback = feedback
        self._native = native or NativeOps()
        self._enable_visual_fallback = enable_visual_fallback
        self._safety = safety or CommandSafetyPolicy()
        self._abort = threading.Event()
        self._failsafe_armed = False

    def abort(self) -> None:
        """Request an abort; thread-safe, callable from a HUD or key hook."""
        self._abort.set()

    def reset_abort(self) -> None:
        """Clear a previous abort so new tasks can run."""
        self._abort.clear()

    @property
    def aborted(self) -> bool:
        """True if an abort has been requested and not reset."""
        return self._abort.is_set()

    async def dispatch(self, step: ActionStep) -> ExecutionResult:
        """Execute ``step`` and report the outcome.

        Errors from handlers are converted into a failed :class:`ExecutionResult`.

        Raises:
            AbortRequested: If an abort was requested or the PyAutoGUI fail-safe fired.
        """
        if self._abort.is_set():
            raise AbortRequested("execution aborted by user")
        try:
            match step.action_type:
                case ActionType.DETERMINISTIC:
                    return await self._dispatch_deterministic(step)
                case ActionType.OS_GROUNDING | ActionType.VISUAL_FALLBACK:
                    return await self._dispatch_click(step)
                case ActionType.FEEDBACK:
                    return await self._dispatch_feedback(step)
        except AbortRequested:
            raise
        except Exception as exc:
            if type(exc).__name__ == "FailSafeException":
                self._abort.set()
                raise AbortRequested("PyAutoGUI fail-safe triggered") from exc
            logger.exception("step %s failed in dispatch", step.step_id)
            return ExecutionResult(
                success=False,
                route=step.action_type,
                error_message=f"{type(exc).__name__}: {exc}",
            )
        raise AssertionError(f"unhandled action_type {step.action_type}")  # pragma: no cover

    async def _dispatch_deterministic(self, step: ActionStep) -> ExecutionResult:
        payload = step.typed_payload()
        match payload:
            case LaunchUrlPayload(url=url):
                return await asyncio.to_thread(self._native.launch_url, str(url))
            case RunCommandPayload(cmd=cmd, timeout_sec=timeout, detach=detach):
                blocked = self._safety.check(cmd)
                if blocked is not None:
                    return ExecutionResult(
                        success=False,
                        route=ActionType.DETERMINISTIC,
                        error_message=f"command blocked by safety policy ({blocked})",
                        retryable=False,
                    )
                if detach:
                    return await asyncio.to_thread(self._native.spawn_process, cmd)
                return await asyncio.to_thread(self._native.run_cli, cmd, timeout)
            case TakeScreenshotPayload(filepath=path, target_region=region):
                return await asyncio.to_thread(self._native.take_screenshot, path, region)
        raise ValueError(f"'{step.action_name}' is not a deterministic action")

    async def _dispatch_click(self, step: ActionStep) -> ExecutionResult:
        payload = step.typed_payload()
        if not isinstance(payload, ClickElementPayload):
            raise ValueError(f"'{step.action_name}' is not a click action")

        fallback_used = step.action_type is ActionType.OS_GROUNDING
        if step.action_type is ActionType.OS_GROUNDING:
            element = await self._grounding.resolve_element(payload.target_label)
            if element is not None and await self._grounding.invoke_element(element):
                return ExecutionResult(
                    success=True,
                    route=ActionType.OS_GROUNDING,
                    details={"element": element.label, "rect": list(element.bounding_rect)},
                )
            reason = (
                f"could not resolve element {payload.target_label!r}"
                if element is None
                else f"could not invoke element {payload.target_label!r}"
            )
            if payload.fallback_coord is None:
                return self._click_failure(reason + " and no fallback_coord given")
        else:
            reason = "visual fallback requested"

        if not self._enable_visual_fallback:
            return self._click_failure(reason + "; visual fallback is disabled")
        if payload.fallback_coord is None:
            return self._click_failure("VISUAL_FALLBACK requires fallback_coord")

        self._arm_failsafe()
        x, y = payload.fallback_coord
        clicked = await self._grounding.click_coordinate(x, y)
        return ExecutionResult(
            success=clicked,
            route=ActionType.VISUAL_FALLBACK,
            fallback_used=fallback_used,
            error_message=None if clicked else f"coordinate click at ({x}, {y}) failed",
            details={"coord": [x, y]},
        )

    async def _dispatch_feedback(self, step: ActionStep) -> ExecutionResult:
        payload = step.typed_payload()
        if not isinstance(payload, SpeakFeedbackPayload):
            raise ValueError(f"'{step.action_name}' is not a feedback action")
        await self._feedback.speak(payload.message, blocking=payload.blocking)
        return ExecutionResult(success=True, route=ActionType.FEEDBACK)

    def _arm_failsafe(self) -> None:
        """Force PyAutoGUI's corner fail-safe on before any coordinate input."""
        if self._failsafe_armed:
            return
        try:
            import pyautogui

            pyautogui.FAILSAFE = True
            self._failsafe_armed = True
        except Exception as exc:  # no display / backend unavailable
            logger.warning("could not arm PyAutoGUI fail-safe: %s", exc)

    @staticmethod
    def _click_failure(message: str) -> ExecutionResult:
        # No input was sent, and resolve_element already waited for the UI to settle.
        return ExecutionResult(
            success=False, route=ActionType.OS_GROUNDING, error_message=message, retryable=False
        )
