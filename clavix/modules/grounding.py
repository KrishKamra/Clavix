"""Structural grounding contract (owner: Person 3 — Accessibility Tree / UI Automation).

The dispatcher routes ``OS_GROUNDING`` and ``VISUAL_FALLBACK`` steps here.
Person 3 replaces :class:`StubGrounding` with a UIA (Windows) / AT-SPI (Linux)
implementation of :meth:`GroundingInterface.resolve_element` and
:meth:`GroundingInterface.invoke_element`.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class UIElement:
    """An interactive element resolved from the accessibility tree.

    Attributes:
        label: Accessible name of the element.
        control_type: Role / control type (e.g. ``"Button"``, ``"MenuItem"``).
        bounding_rect: ``(left, top, right, bottom)`` in screen pixels.
        native: Backend-specific handle (e.g. a ``uiautomation`` Control) that
            :meth:`GroundingInterface.invoke_element` can use. The core never
            reads it, and it is excluded from equality and ``repr``.
    """

    label: str
    control_type: str
    bounding_rect: tuple[int, int, int, int]
    native: Any = field(default=None, compare=False, repr=False)

    @property
    def center(self) -> tuple[int, int]:
        """Centre point of the bounding rect."""
        left, top, right, bottom = self.bounding_rect
        return (left + right) // 2, (top + bottom) // 2


class GroundingInterface(ABC):
    """Resolves and actuates UI elements."""

    @abstractmethod
    async def resolve_element(self, target_label: str) -> UIElement | None:
        """Find the best-matching interactive element for ``target_label``.

        Implementations own the waiting: poll until the element appears or their
        own timeout expires. The agent does not retry a ``None`` result.

        Returns:
            The element, or ``None`` if nothing matched. Must not raise for a miss.
        """

    @abstractmethod
    async def invoke_element(self, element: UIElement) -> bool:
        """Activate ``element`` (InvokePattern, or a click at its centre).

        Returns:
            True if the element was activated.
        """

    @abstractmethod
    async def click_coordinate(self, x: int, y: int) -> bool:
        """Visual fallback: click raw screen coordinates.

        Returns:
            True if the click was issued.
        """

    def list_window_titles(self) -> list[str]:
        """Optional: titles of visible top-level windows, for the state verifier.

        Override to supply titles from the accessibility tree (required for
        ``window_title_contains`` on Linux). The default defers to the verifier's
        built-in Windows implementation.

        Raises:
            NotImplementedError: If this backend does not provide window titles.
        """
        raise NotImplementedError


class StubGrounding(GroundingInterface):
    """Placeholder until the accessibility module is integrated.

    Element resolution always misses, so ``click_element`` steps fall through to
    coordinates when the visual fallback is enabled. Coordinate clicks are real
    PyAutoGUI clicks with ``FAILSAFE`` on (move the mouse to a screen corner to
    abort).
    """

    async def resolve_element(self, target_label: str) -> UIElement | None:
        """Always returns ``None``: no accessibility backend yet."""
        logger.warning("grounding not integrated; cannot resolve %r", target_label)
        return None

    async def invoke_element(self, element: UIElement) -> bool:
        """Clicks the centre of ``element`` via the coordinate path."""
        x, y = element.center
        return await self.click_coordinate(x, y)

    async def click_coordinate(self, x: int, y: int) -> bool:
        """Click ``(x, y)`` with PyAutoGUI in a worker thread."""
        import pyautogui

        pyautogui.FAILSAFE = True
        await asyncio.to_thread(pyautogui.click, x, y)
        return True
