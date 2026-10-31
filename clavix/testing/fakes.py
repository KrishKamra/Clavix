"""In-memory fakes for the module interfaces and native OS operations.

None of these touch the real desktop, except :meth:`FakeNativeOps.run_cli`,
which really runs the shell command (keep test commands harmless, e.g. ``echo``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from clavix.execution.native_ops import NativeOps
from clavix.modules.feedback import AgentEvent, FeedbackInterface
from clavix.modules.grounding import GroundingInterface, UIElement
from clavix.schemas.actions import ActionType, ExecutionResult


class FakeNativeOps(NativeOps):
    """Records calls; URLs, spawns and screenshots are faked, shell commands run."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def launch_url(self, url: str) -> ExecutionResult:
        self.calls.append(("launch_url", (url,)))
        return ExecutionResult(success=True, route=ActionType.DETERMINISTIC, details={"url": url})

    def spawn_process(self, cmd: str) -> ExecutionResult:
        self.calls.append(("spawn_process", (cmd,)))
        return ExecutionResult(success=True, route=ActionType.DETERMINISTIC, pid=4242)

    def run_cli(self, cmd: str, timeout_sec: int = 10) -> ExecutionResult:
        self.calls.append(("run_cli", (cmd, timeout_sec)))
        return super().run_cli(cmd, timeout_sec)

    def take_screenshot(
        self, filepath: str, target_region: Optional[tuple[int, int, int, int]] = None
    ) -> ExecutionResult:
        """Write a blank image of the requested region size (default 64x48)."""
        from PIL import Image

        self.calls.append(("take_screenshot", (filepath, target_region)))
        Path(filepath).parent.mkdir(parents=True, exist_ok=True)
        size = (target_region[2], target_region[3]) if target_region else (64, 48)
        Image.new("RGB", size, "white").save(filepath)
        return ExecutionResult(success=True, route=ActionType.DETERMINISTIC, artifact_path=filepath)


class RecordingFeedback(FeedbackInterface):
    """Collects spoken messages, state transitions and progress events."""

    def __init__(self) -> None:
        self.messages: list[str] = []
        self.states: list[tuple[str, str]] = []
        self.events: list[AgentEvent] = []

    async def speak(self, message: str, blocking: bool = False) -> None:
        self.messages.append(message)

    async def on_state_change(self, previous: str, current: str) -> None:
        self.states.append((previous, current))

    async def on_event(self, event: AgentEvent) -> None:
        self.events.append(event)


class FakeGrounding(GroundingInterface):
    """Resolves only labels in ``elements``; records invocations and clicks."""

    def __init__(
        self, elements: dict[str, UIElement] | None = None, window_titles: list[str] | None = None
    ) -> None:
        self.elements = elements or {}
        self.window_titles = window_titles
        self.invoked: list[str] = []
        self.clicks: list[tuple[int, int]] = []

    async def resolve_element(self, target_label: str) -> UIElement | None:
        return self.elements.get(target_label)

    async def invoke_element(self, element: UIElement) -> bool:
        self.invoked.append(element.label)
        return True

    async def click_coordinate(self, x: int, y: int) -> bool:
        self.clicks.append((x, y))
        return True

    def list_window_titles(self) -> list[str]:
        if self.window_titles is None:
            raise NotImplementedError
        return list(self.window_titles)
