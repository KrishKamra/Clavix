"""User feedback contract (owner: Person 4 — TTS & Safety HUD).

The agent calls :meth:`FeedbackInterface.speak` for ``speak_feedback`` steps
and end-of-task summaries, :meth:`FeedbackInterface.on_state_change` on every
state transition, and :meth:`FeedbackInterface.on_event` with structured
progress, so a HUD can mirror the agent. A HUD abort button or hotkey calls
:meth:`FeedbackInterface.request_abort`.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

AgentEvent = dict[str, Any]
"""A JSON-serialisable progress event; ``event["type"]`` selects the shape:

- ``{"type": "transcript", "text": str, "asr_ms": float, "confidence": float | None}``
- ``{"type": "plan", "steps": [{"id": int, "action": str, "verb": str, "target": str}], "llm_ms": float}``
- ``{"type": "step", "id": int, "status": "running" | "ok" | "retry" | "failed", "attempt": int}``
- ``{"type": "timing", "asr_ms": float, "llm_ms": float, "exec_ms": float, "success": bool}``

Consumers must ignore unknown types and keys, so events can grow without a contract change.
"""


class FeedbackInterface(ABC):
    """Speaks to and displays state for the user."""

    _abort: Callable[[], None] | None = None

    @abstractmethod
    async def speak(self, message: str, blocking: bool = False) -> None:
        """Deliver ``message`` to the user.

        Args:
            message: Text to speak / display.
            blocking: Wait until playback finishes before returning.
        """

    async def on_state_change(self, previous: str, current: str) -> None:
        """Hook called on every agent state transition (default: no-op)."""
        return None

    async def on_event(self, event: AgentEvent) -> None:
        """Hook called with structured progress (see :data:`AgentEvent`; default: no-op).

        Must return quickly and never raise; the agent logs and swallows exceptions.
        """
        return None

    def bind_abort(self, abort: Callable[[], None]) -> None:
        """Receive the dispatcher's thread-safe abort callback.

        Called once by :func:`clavix.runtime.build_runtime`. A HUD abort button or
        global Escape hotkey should call :meth:`request_abort`.
        """
        self._abort = abort

    def request_abort(self) -> None:
        """Abort the running task (no-op if no abort callback is bound)."""
        if self._abort is not None:
            self._abort()


class ConsoleFeedback(FeedbackInterface):
    """Prints messages to stdout; a stand-in until TTS lands."""

    def __init__(self, show_states: bool = False) -> None:
        self._show_states = show_states

    async def speak(self, message: str, blocking: bool = False) -> None:
        """Print ``message``."""
        print(f"[clavix] {message}")

    async def on_state_change(self, previous: str, current: str) -> None:
        """Log transitions, and print them when ``show_states`` is set."""
        logger.debug("state %s -> %s", previous, current)
        if self._show_states:
            print(f"[clavix] {previous} -> {current}")
