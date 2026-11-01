"""Tests for UIAGrounding (Person 3 — P3-M1, P3-M2).

Structure
---------
- ``TestNormalize`` — pure ``_normalize`` unit tests; no UIA, runs on Linux.
- ``TestScore`` — pure ``_score`` unit tests; no UIA, runs on Linux.
- ``TestTiebreak`` — tie-break ordering tests using simple stand-in objects; no UIA, runs on Linux.
- ``TestUIAGroundingContract`` — contract suite inheriting ``GroundingContract``; Windows only.
- ``TestDesktop`` — desktop integration tests, Windows only, marked ``desktop``.
"""

from __future__ import annotations

import sys

import pytest

from clavix.modules.grounding_uia import _normalize, _score
from clavix.testing.contracts import GroundingContract

# ---------------------------------------------------------------------------
# Pure-function unit tests (no UIA, runs on Linux)
# ---------------------------------------------------------------------------


class TestNormalize:
    """Unit tests for ``_normalize``."""

    def test_casefold(self) -> None:
        assert _normalize("File") == "file"

    def test_casefold_unicode(self) -> None:
        # German sharp-s casefold
        assert _normalize("STRAßE") == "strasse"

    def test_remove_ampersand_accelerator(self) -> None:
        assert _normalize("&File") == "file"

    def test_remove_ampersand_mid(self) -> None:
        assert _normalize("Sa&ve") == "save"

    def test_cut_tab_shortcut(self) -> None:
        assert _normalize("Save\tCtrl+S") == "save"

    def test_cut_tab_only_prefix(self) -> None:
        assert _normalize("Undo\tCtrl+Z") == "undo"

    def test_strip_ellipsis_ascii(self) -> None:
        assert _normalize("Open...") == "open"

    def test_strip_ellipsis_unicode(self) -> None:
        assert _normalize("Open\u2026") == "open"

    def test_collapse_whitespace(self) -> None:
        assert _normalize("  New   Tab  ") == "new tab"

    def test_strip_outer_whitespace(self) -> None:
        assert _normalize("  start  ") == "start"

    def test_combined(self) -> None:
        # Ampersand + tab + ellipsis + case
        assert _normalize("&Save As...\tCtrl+Shift+S") == "save as"

    def test_empty_string(self) -> None:
        assert _normalize("") == ""

    def test_only_ampersand(self) -> None:
        assert _normalize("&") == ""


class TestScore:
    """Unit tests for ``_score`` (both args are pre-normalised)."""

    def test_exact_match(self) -> None:
        assert _score("file", "file") == 1.00

    def test_exact_match_start(self) -> None:
        assert _score("start", "start") == 1.00

    def test_starts_with_target_plus_space(self) -> None:
        # candidate "start button" starts with "start "
        assert _score("start", "start button") == 0.90

    def test_starts_with_not_just_prefix(self) -> None:
        # "starter" does NOT start with "start " (no trailing space match)
        s = _score("start", "starter")
        assert s < 0.90

    def test_whole_word_substring(self) -> None:
        # "file" is a whole-word substring of "open file dialog"
        assert _score("file", "open file dialog") == 0.85

    def test_whole_word_boundary_respected(self) -> None:
        # "file" should NOT match "profile" as a whole-word substring
        s = _score("file", "profile")
        assert s < 0.85

    def test_difflib_fallback(self) -> None:
        import difflib

        t, c = "notepad", "note"
        expected = difflib.SequenceMatcher(None, t, c).ratio()
        assert _score(t, c) == pytest.approx(expected)

    def test_score_automation_id(self) -> None:
        # _score is applied identically to both Name and AutomationId
        assert _score("startbutton", "startbutton") == 1.00

    def test_completely_different(self) -> None:
        s = _score("zzz", "aaa")
        assert s < 0.5


class TestTiebreak:
    """Tests for tie-break ordering using stand-in objects."""

    def _make_control(
        self,
        name: str = "X",
        control_type: str = "TextControl",
        rect: tuple[int, int, int, int] = (0, 0, 100, 50),
        is_control_element: bool = True,
    ) -> object:
        """Create a minimal mock control object."""

        class _Rect:
            def __init__(self, lt: int, t: int, r: int, b: int) -> None:
                self.left, self.top, self.right, self.bottom = lt, t, r, b

            def width(self) -> int:
                return self.right - self.left

            def height(self) -> int:
                return self.bottom - self.top

        class _Ctrl:
            Name = name
            AutomationId = ""
            ControlTypeName = control_type
            BoundingRectangle = _Rect(*rect)
            IsControlElement = is_control_element

        return _Ctrl()

    def test_higher_score_wins(self) -> None:
        from clavix.modules.grounding_uia import UIAGrounding

        g = UIAGrounding()
        key_a = g._tiebreak_key((0.95, self._make_control("a"), 1))
        key_b = g._tiebreak_key((0.85, self._make_control("b"), 1))
        assert key_a < key_b

    def test_control_element_preferred_over_template_part(self) -> None:
        from clavix.modules.grounding_uia import UIAGrounding

        g = UIAGrounding()
        key_ctrl = g._tiebreak_key(
            (1.0, self._make_control("File", "MenuItemControl", is_control_element=True), 4)
        )
        key_part = g._tiebreak_key(
            (1.0, self._make_control("File", "ButtonControl", is_control_element=False), 5)
        )
        assert key_ctrl < key_part

    def test_control_type_priority_button_beats_text(self) -> None:
        from clavix.modules.grounding_uia import UIAGrounding

        g = UIAGrounding()
        key_button = g._tiebreak_key((0.90, self._make_control("x", "ButtonControl"), 1))
        key_text = g._tiebreak_key((0.90, self._make_control("x", "TextControl"), 1))
        assert key_button < key_text

    def test_control_type_menu_item_beats_hyperlink(self) -> None:
        from clavix.modules.grounding_uia import UIAGrounding

        g = UIAGrounding()
        key_menu = g._tiebreak_key((0.90, self._make_control("x", "MenuItemControl"), 1))
        key_link = g._tiebreak_key((0.90, self._make_control("x", "HyperlinkControl"), 1))
        assert key_menu < key_link

    def test_smaller_area_wins(self) -> None:
        from clavix.modules.grounding_uia import UIAGrounding

        g = UIAGrounding()
        key_small = g._tiebreak_key((0.90, self._make_control("x", "ButtonControl", (0, 0, 10, 10)), 1))
        key_large = g._tiebreak_key((0.90, self._make_control("x", "ButtonControl", (0, 0, 200, 200)), 1))
        assert key_small < key_large

    def test_smaller_top_left_wins_on_equal_area(self) -> None:
        from clavix.modules.grounding_uia import UIAGrounding

        g = UIAGrounding()
        key_high = g._tiebreak_key((0.90, self._make_control("x", "ButtonControl", (0, 0, 50, 50)), 1))
        key_low = g._tiebreak_key((0.90, self._make_control("x", "ButtonControl", (0, 100, 50, 150)), 1))
        assert key_high < key_low

    def test_shallower_depth_wins_on_all_else_equal(self) -> None:
        from clavix.modules.grounding_uia import UIAGrounding

        g = UIAGrounding()
        key_shallow = g._tiebreak_key((0.90, self._make_control("x", "ButtonControl", (0, 0, 50, 50)), 2))
        key_deep = g._tiebreak_key((0.90, self._make_control("x", "ButtonControl", (0, 0, 50, 50)), 5))
        assert key_shallow < key_deep


# ---------------------------------------------------------------------------
# Contract suite — Windows only (skipped on Linux at collection time)
# ---------------------------------------------------------------------------

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows UI Automation")


class TestUIAGroundingContract(GroundingContract):
    """GroundingContract conformance suite wired to UIAGrounding."""

    resolve_timeout_sec = 1.0
    known_label = "Start"

    @pytest.fixture
    def impl(self):  # type: ignore[override]
        from clavix.modules.grounding_uia import UIAGrounding

        g = UIAGrounding(timeout_sec=self.resolve_timeout_sec)
        yield g
        g.close()


# ---------------------------------------------------------------------------
# Desktop integration tests — Windows only, marked ``desktop``
# ---------------------------------------------------------------------------


@pytest.mark.desktop
class TestDesktop:
    """Desktop tests: drive real Windows UI elements."""

    @pytest.fixture
    def grounding(self):
        from clavix.modules.grounding_uia import UIAGrounding

        g = UIAGrounding(timeout_sec=5.0, poll_interval_sec=0.2)
        yield g
        g.close()

    def test_notepad_file_menu(self, grounding: object) -> None:
        """Launch Notepad, open File menu, assert a child item appears, then clean up."""
        import asyncio
        import subprocess
        import time

        from clavix.modules.grounding_uia import UIAGrounding

        assert isinstance(grounding, UIAGrounding)

        proc = subprocess.Popen(["notepad.exe"])
        try:
            # Wait for the window to appear.
            time.sleep(1.5)

            file_elem = asyncio.run(grounding.resolve_element("File"))
            assert file_elem is not None, "Could not find 'File' in Notepad"
            ok = asyncio.run(grounding.invoke_element(file_elem))
            assert ok, "invoke_element returned False for 'File'"

            # After opening the menu, "New tab" or "New" should appear.
            new_elem = asyncio.run(grounding.resolve_element("New tab"))
            if new_elem is None:
                new_elem = asyncio.run(grounding.resolve_element("New"))
            assert new_elem is not None, "Expected 'New tab' or 'New' to appear after opening File menu"
            assert new_elem.control_type == "MenuItemControl"

            import pyautogui

            old_failsafe = pyautogui.FAILSAFE
            try:
                pyautogui.FAILSAFE = False
                pyautogui.press("escape")
            finally:
                pyautogui.FAILSAFE = old_failsafe
            time.sleep(0.3)
        finally:
            proc.terminate()
            proc.wait(timeout=5)

    def test_start_button_toggle(self, grounding: object) -> None:
        """Resolve and invoke Start, assert the Start menu opened, invoke again to close."""
        import asyncio
        import time

        from clavix.modules.grounding_uia import UIAGrounding

        assert isinstance(grounding, UIAGrounding)

        # Open Start menu.
        start_elem = asyncio.run(grounding.resolve_element("Start"))
        assert start_elem is not None, "Could not resolve 'Start' button"
        ok = asyncio.run(grounding.invoke_element(start_elem))
        assert ok, "invoke_element returned False for 'Start'"
        time.sleep(0.8)  # allow the menu animation to complete

        titles_open = grounding.list_window_titles()
        assert isinstance(titles_open, list)

        # Close the Start menu by invoking Start again.
        start_elem2 = asyncio.run(grounding.resolve_element("Start"))
        assert start_elem2 is not None
        asyncio.run(grounding.invoke_element(start_elem2))
        time.sleep(0.5)
