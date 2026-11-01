"""Tests for UIAGrounding (Person 3 — P3-M1, P3-M2).

Structure
---------
- ``TestNormalize`` — pure ``_normalize`` unit tests; no UIA, runs on Linux.
- ``TestScore`` — pure ``_score`` unit tests; no UIA, runs on Linux.
- ``TestTiebreak`` — tie-break ordering tests using simple stand-in objects; no UIA, runs on Linux.
- ``TestDeadlines`` — searches stop at their deadline; a fake ``uiautomation``; runs on Linux.
- ``TestUIAGroundingContract`` — contract suite inheriting ``GroundingContract``; Windows only.
- ``TestDesktop`` — desktop integration tests, Windows only, marked ``desktop``.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import sys
import time
import types
from typing import Any

import pytest

from clavix.modules import grounding_uia
from clavix.modules.grounding import UIElement
from clavix.modules.grounding_uia import UIAGrounding, _normalize, _score
from clavix.testing.contracts import GroundingContract

# Only the UIA-backed suites need Windows and the grounding extra; the pure tests run everywhere,
# including CI, which installs no extras.
windows_only = pytest.mark.skipif(
    sys.platform != "win32" or importlib.util.find_spec("uiautomation") is None,
    reason="needs Windows UI Automation (uv sync --extra grounding)",
)

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

    def test_only_a_trailing_ellipsis_is_stripped(self) -> None:
        assert _normalize("Save as... ") == "save as"
        assert _normalize("Wait...for it") == "wait...for it"

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


class _SlowWalkAuto:
    """Stand-in for ``uiautomation``: every walked node takes ``node_sec``."""

    UIAutomationInitializerInThread = staticmethod(contextlib.nullcontext)

    def __init__(self, nodes: int, node_sec: float) -> None:
        self.nodes, self.node_sec = nodes, node_sec

    def WalkControl(self, root: Any, includeTop: bool = False, maxDepth: int = 0) -> Any:
        for depth in range(self.nodes):
            time.sleep(self.node_sec)
            yield root, depth


class TestDeadlines:
    """Slow UIA providers must not push a search past its deadline (no real UIA needed)."""

    def test_scope_walk_stops_at_the_deadline(self) -> None:
        g = UIAGrounding()
        fake = _SlowWalkAuto(nodes=1000, node_sec=0.01)
        start = time.monotonic()
        with pytest.raises(grounding_uia._DeadlineExceeded):
            g._try_scope([object()], "file", fake, [0], [0.0], deadline=time.monotonic() + 0.1)
        assert time.monotonic() - start < 0.5

    def test_search_returns_none_instead_of_raising(self, monkeypatch: pytest.MonkeyPatch) -> None:
        g = UIAGrounding()

        def slow_scopes(*args: Any) -> Any:
            raise grounding_uia._DeadlineExceeded

        monkeypatch.setattr(g, "_search_scopes", slow_scopes)
        assert g._search_once("file", object(), deadline=0.0) == (None, 0, 0.0)

    def test_resolve_returns_on_time_even_if_a_uia_call_stalls(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = types.ModuleType("uiautomation")
        fake.UIAutomationInitializerInThread = contextlib.nullcontext  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "uiautomation", fake)
        g = UIAGrounding(timeout_sec=0.3, poll_interval_sec=0.05)
        monkeypatch.setattr(g, "_search_once", lambda *a, **k: time.sleep(3) or (None, 0, 0.0))
        start = time.monotonic()
        try:
            assert asyncio.run(g.resolve_element("File")) is None
            assert time.monotonic() - start < 0.3 + grounding_uia._DEADLINE_SLACK_SEC + 0.4
        finally:
            g.close()

    def test_resolve_returns_a_hit_immediately(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = types.ModuleType("uiautomation")
        fake.UIAutomationInitializerInThread = contextlib.nullcontext  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "uiautomation", fake)
        g = UIAGrounding(timeout_sec=5.0)
        hit = UIElement("File", "MenuItemControl", (0, 0, 40, 20))
        monkeypatch.setattr(g, "_search_once", lambda *a, **k: (hit, 1, 1.0))
        start = time.monotonic()
        try:
            assert asyncio.run(g.resolve_element("&File")) == hit
            assert time.monotonic() - start < 1.0
        finally:
            g.close()


class TestWorkerStartupFailure:
    """Without uiautomation (e.g. the grounding extra not installed) nothing may hang."""

    @pytest.fixture
    def no_uia(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "uiautomation", None)  # any import of it now fails

    def test_list_window_titles_returns_empty_instead_of_hanging(self, no_uia: None) -> None:
        g = UIAGrounding(timeout_sec=0.5)
        start = time.monotonic()
        try:
            assert g.list_window_titles() == []
            assert g.list_window_titles() == []  # and again, after the worker has died
        finally:
            g.close()
        assert time.monotonic() - start < 2.0

    def test_resolve_element_raises_promptly(self, no_uia: None) -> None:
        g = UIAGrounding(timeout_sec=5.0)
        start = time.monotonic()
        try:
            with pytest.raises(ImportError):
                asyncio.run(g.resolve_element("Start"))
        finally:
            g.close()
        assert time.monotonic() - start < 2.0


class _Rect:
    def __init__(self, left: int, top: int, right: int, bottom: int) -> None:
        self.left, self.top, self.right, self.bottom = left, top, right, bottom

    def width(self) -> int:
        return self.right - self.left

    def height(self) -> int:
        return self.bottom - self.top


class _FakeControl:
    def __init__(self, name: str, control_type: str, class_name: str = "", children: Any = ()) -> None:
        self.Name, self.ControlTypeName, self.ClassName = name, control_type, class_name
        self.AutomationId, self.IsEnabled, self.IsOffscreen, self.IsControlElement = "", True, False, True
        self.BoundingRectangle = _Rect(0, 0, 40, 20)
        self.NativeWindowHandle = id(self)
        self.children = list(children)


class _FakeDesktopAuto:
    """No foreground window and no taskbar; only the windows given to scope 3."""

    def __init__(self) -> None:
        self.walked: list[str] = []

    def GetForegroundControl(self) -> None:
        return None

    def ControlFromHandle(self, hwnd: int) -> None:
        return None

    def WalkControl(self, root: _FakeControl, includeTop: bool = False, maxDepth: int = 0) -> Any:
        self.walked.append(root.Name)
        for child in root.children:
            yield child, 1


class TestScopeThreeOrder:
    def test_front_window_wins_and_windows_behind_are_not_walked(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        notepad = _FakeControl(
            "Untitled - Notepad", "WindowControl", children=[_FakeControl("File", "MenuItemControl")]
        )
        browser = _FakeControl(
            "Report.pdf - Brave", "WindowControl", children=[_FakeControl("File", "ButtonControl")]
        )
        desktop = _FakeControl("Program Manager", "PaneControl", class_name="Progman")
        monkeypatch.setattr(grounding_uia, "_top_level_controls", lambda auto: [desktop, notepad, browser])
        fake = _FakeDesktopAuto()

        element, _, score = UIAGrounding()._search_once("file", fake)

        assert element is not None and score == 1.0
        # A button outranks a menu item within one window, but Notepad is in front.
        assert element.control_type == "MenuItemControl"
        assert fake.walked == ["Untitled - Notepad"]  # desktop sorted last; Brave never walked

    @staticmethod
    def taskbar(*names: str) -> _FakeControl:
        buttons = [_FakeControl(name, "ButtonControl") for name in names]
        return _FakeControl("Taskbar", "PaneControl", class_name="Shell_TrayWnd", children=buttons)

    def test_exact_match_behind_beats_a_partial_taskbar_match(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Seen on Windows 11: "file" is a 0.90 prefix match for the taskbar's File Explorer button.
        notepad = _FakeControl(
            "Untitled - Notepad", "WindowControl", children=[_FakeControl("File", "MenuItemControl")]
        )
        windows = [self.taskbar("File Explorer - 1 running window"), notepad]
        monkeypatch.setattr(grounding_uia, "_top_level_controls", lambda auto: windows)

        element, _, _ = UIAGrounding()._search_once("file", _FakeDesktopAuto())

        assert element is not None and (element.label, element.control_type) == ("File", "MenuItemControl")

    def test_without_an_exact_match_the_first_partial_in_scope_order_wins(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        other = _FakeControl(
            "Settings", "WindowControl", children=[_FakeControl("Open firefox", "ButtonControl")]
        )
        windows = [self.taskbar("Firefox - 1 running window pinned"), other]
        monkeypatch.setattr(grounding_uia, "_top_level_controls", lambda auto: windows)

        element, _, _ = UIAGrounding()._search_once("firefox", _FakeDesktopAuto())

        assert element is not None and element.label == "Firefox - 1 running window pinned"

    def test_deadline_keeps_the_partial_match_found_so_far(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class SlowAfterTaskbar(_FakeDesktopAuto):
            def WalkControl(self, root: _FakeControl, includeTop: bool = False, maxDepth: int = 0) -> Any:
                if root.Name == "Slow window":
                    raise grounding_uia._DeadlineExceeded
                yield from super().WalkControl(root, includeTop, maxDepth)

        windows = [
            self.taskbar("Firefox - 1 running window pinned"),
            _FakeControl("Slow window", "WindowControl"),
        ]
        monkeypatch.setattr(grounding_uia, "_top_level_controls", lambda auto: windows)

        element, _, _ = UIAGrounding()._search_once("firefox", SlowAfterTaskbar())

        assert element is not None and element.label == "Firefox - 1 running window pinned"


# ---------------------------------------------------------------------------
# Contract suite — Windows only
# ---------------------------------------------------------------------------


@windows_only
class TestUIAGroundingContract(GroundingContract):
    """GroundingContract conformance suite wired to UIAGrounding."""

    resolve_timeout_sec = 3.0  # the production default; a large foreground app makes 1 s borderline
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


@windows_only
@pytest.mark.desktop
class TestDesktop:
    """Desktop tests: drive real Windows UI elements."""

    @pytest.fixture
    def grounding(self) -> Any:
        g = UIAGrounding(timeout_sec=5.0, poll_interval_sec=0.2)
        yield g
        g.close()

    def test_notepad_file_menu(self, grounding: UIAGrounding) -> None:
        """Launch Notepad, open File, see a menu item appear, then close only that Notepad."""
        import subprocess

        import pyautogui

        from clavix.evaluation.harness import kill_pids, list_pids

        before = list_pids(["notepad.exe"])
        subprocess.Popen(["notepad.exe"])
        try:
            # Wait for the window first, as the agent does (T3-01's window_title_contains:Notepad).
            # Resolving earlier matches another app's exact "File" (a PDF viewer's, in testing).
            deadline = time.monotonic() + 10
            while not any("Notepad" in title for title in grounding.list_window_titles()):
                assert time.monotonic() < deadline, "Notepad did not open"
                time.sleep(0.2)
            file_elem = asyncio.run(grounding.resolve_element("File"))
            assert file_elem is not None, "Could not find 'File' in Notepad"
            assert asyncio.run(grounding.invoke_element(file_elem)), (
                "invoke_element returned False for 'File'"
            )

            # "New tab" is ambiguous (Windows 11 Notepad also has a "+" tab-strip button with
            # that name), so probe labels that exist only in the open File menu.
            item = asyncio.run(grounding.resolve_element("New window"))
            if item is None:
                item = asyncio.run(grounding.resolve_element("Save as"))
            assert item is not None, "Expected 'New window' or 'Save as' after opening the File menu"
            assert item.control_type == "MenuItemControl"

            old_failsafe = pyautogui.FAILSAFE
            try:
                pyautogui.FAILSAFE = False
                pyautogui.press("escape")
            finally:
                pyautogui.FAILSAFE = old_failsafe
        finally:
            # Windows 11 Notepad may hand the window to another process: kill every new one.
            time.sleep(0.3)
            kill_pids(list_pids(["notepad.exe"]) - before)

    def test_start_button_toggle(self, grounding: UIAGrounding) -> None:
        """Invoke Start, check the menu is open (toggle state On), invoke again to close it."""

        def toggle_state(element: UIElement) -> int:
            # UIElement.native may only be used on the grounding's UIA worker thread.
            import uiautomation as auto

            pattern = element.native.GetPattern(auto.PatternId.TogglePattern)
            return int(pattern.ToggleState)

        start_elem = asyncio.run(grounding.resolve_element("Start"))
        assert start_elem is not None, "Could not resolve 'Start' button"
        assert asyncio.run(grounding.invoke_element(start_elem)), "invoke_element returned False for 'Start'"
        time.sleep(0.8)  # menu animation
        try:
            assert asyncio.run(grounding._run_on_worker(lambda: toggle_state(start_elem))) == 1, (
                "menu not open"
            )
        finally:
            asyncio.run(grounding.invoke_element(start_elem))
            time.sleep(0.5)
        assert asyncio.run(grounding._run_on_worker(lambda: toggle_state(start_elem))) == 0, "menu not closed"
