"""Windows UI Automation grounding backend (Person 3 — P3-M1, P3-M2).

Implements :class:`UIAGrounding`, a :class:`GroundingInterface` that resolves
on-screen elements by accessible name using the Windows UI Automation tree and
activates them via UIA interaction patterns.

Threading model
---------------
All ``uiautomation`` calls run on dedicated daemon worker thread(s) owned by
the instance.  A ``threading.Thread`` (daemon=True) with a ``queue.SimpleQueue``
is used instead of ``ThreadPoolExecutor`` because non-daemon executor threads
were observed delaying process exit by more than two minutes—even after
``executor.shutdown(wait=True)``—due to the COM apartment kept open by the
UIA initializer context manager.  A daemon thread allows the process to exit
promptly, and ``close()`` signals it with a sentinel for clean shutdown.

Desktop environment handling
----------------------------
In standard Windows environments, all applications and the shell taskbar run on
the interactive desktop (``"Default"``).  In sandboxed test or subshell
environments, the process may run on an isolated virtual desktop (e.g.
``exebox-...``), while the system Taskbar (and Start button) remains on
``"Default"``.  When running in such an environment, :class:`UIAGrounding`
lazily spawns a secondary auxiliary worker attached to ``"Default"`` so that
system controls like the Taskbar and Start button can always be found and
invoked seamlessly.

Platform
--------
``uiautomation`` is Windows-only.  This module imports cleanly on Linux/macOS
because ``uiautomation`` is never imported at module level.  The pure helpers
``_normalize`` and ``_score`` work on any platform.
"""

from __future__ import annotations

import asyncio
import difflib
import logging
import queue
import re
import sys
import threading
from concurrent.futures import Future
from typing import Any

from clavix.modules.grounding import GroundingInterface, UIElement

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Control-type tie-break priority (lower index = higher priority).
# ---------------------------------------------------------------------------
_CONTROL_TYPE_PRIORITY: list[str] = [
    "ButtonControl",
    "MenuItemControl",
    "HyperlinkControl",
    "TabItemControl",
    "ListItemControl",
    "TreeItemControl",
    "CheckBoxControl",
    "RadioButtonControl",
    "ComboBoxControl",
    "SplitButtonControl",
    "EditControl",
]

# Sentinel used to stop worker threads.
_STOP = object()


# ---------------------------------------------------------------------------
# Pure helpers — no uiautomation import; work on Linux too.
# ---------------------------------------------------------------------------


def _normalize(s: str) -> str:
    """Normalise a control name or target label for comparison.

    Steps (in order):
    1. Casefold.
    2. Remove ``&`` access-key markers.
    3. Cut at the first ``\\t`` (keyboard-shortcut suffix).
    4. Strip a trailing ``...`` or ``…``.
    5. Collapse runs of whitespace to a single space, then strip.
    """
    s = s.casefold()
    s = s.replace("&", "")
    s = s.split("\t", 1)[0]
    s = re.sub(r"\.\.\.|…", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _score(target: str, candidate: str) -> float:
    """Score *candidate* against *target* (both already normalised).

    +-------------------------------------------+-------+
    | Condition                                 | Score |
    +===========================================+=======+
    | ``c == t``                                | 1.00  |
    +-------------------------------------------+-------+
    | ``c`` starts with ``t`` + space           | 0.90  |
    +-------------------------------------------+-------+
    | ``t`` is a whole-word substring of ``c``  | 0.85  |
    +-------------------------------------------+-------+
    | otherwise                                 | difflib ratio |
    +-------------------------------------------+-------+
    """
    t, c = target, candidate
    if c == t:
        return 1.00
    if c.startswith(t + " "):
        return 0.90
    # Whole-word substring: t must appear in c surrounded by word boundaries.
    if re.search(r"(?<!\w)" + re.escape(t) + r"(?!\w)", c):
        return 0.85
    return difflib.SequenceMatcher(None, t, c).ratio()


# ---------------------------------------------------------------------------
# Worker thread wrapper
# ---------------------------------------------------------------------------


class _UIAWorker:
    """Dedicated worker thread running in a UIA COM apartment."""

    def __init__(self, name: str, desktop_name: str | None = None) -> None:
        self.name = name
        self.desktop_name = desktop_name
        self._work_queue: queue.SimpleQueue[Any] = queue.SimpleQueue()
        self._thread: threading.Thread | None = None
        self._thread_lock = threading.Lock()

    def ensure_started(self) -> None:
        """Start the worker thread lazily on first use."""
        if self._thread is not None and self._thread.is_alive():
            return
        with self._thread_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            t = threading.Thread(
                target=self._run,
                name=self.name,
                daemon=True,
            )
            t.start()
            self._thread = t

    def _run(self) -> None:
        """Worker thread entry point."""
        if sys.platform == "win32" and self.desktop_name:
            try:
                import ctypes

                u32 = ctypes.windll.user32
                hdesk = u32.OpenDesktopW(self.desktop_name, 0, False, 0x01FF)
                if hdesk:
                    u32.SetThreadDesktop(hdesk)
            except Exception:
                logger.debug("Failed to set thread desktop to %s", self.desktop_name, exc_info=True)

        import uiautomation as auto

        with auto.UIAutomationInitializerInThread():
            while True:
                item = self._work_queue.get()
                if item is _STOP:
                    break
                future, fn = item
                if future.set_running_or_notify_cancel():
                    try:
                        result = fn()
                        future.set_result(result)
                    except Exception as exc:
                        future.set_exception(exc)

    def submit(self, fn: Any) -> Future[Any]:
        """Post a callable to this worker thread and return a Future for its result."""
        self.ensure_started()
        fut: Future[Any] = Future()
        self._work_queue.put((fut, fn))
        return fut

    def close(self) -> None:
        """Signal the worker thread to exit and join it."""
        if self._thread is not None and self._thread.is_alive():
            self._work_queue.put(_STOP)
            self._thread.join(timeout=3.0)


# ---------------------------------------------------------------------------
# UIAGrounding
# ---------------------------------------------------------------------------


class UIAGrounding(GroundingInterface):
    """Windows UI Automation grounding backend.

    All ``uiautomation`` calls run on dedicated daemon worker thread(s)
    to satisfy COM-apartment rules.  Async public methods post callables to
    the worker thread and await the result via a ``concurrent.futures.Future``.

    Args:
        timeout_sec: Total time to poll for an element before giving up.
        poll_interval_sec: Delay between tree-search iterations.
        max_depth: ``WalkControl`` depth per search root.
        min_match_score: Minimum score to accept a candidate.
    """

    def __init__(
        self,
        timeout_sec: float = 3.0,
        poll_interval_sec: float = 0.2,
        max_depth: int = 12,
        min_match_score: float = 0.8,
    ) -> None:
        self.timeout_sec = timeout_sec
        self.poll_interval_sec = poll_interval_sec
        self.max_depth = max_depth
        self.min_match_score = min_match_score

        # Primary worker on current desktop.
        self._primary_worker = _UIAWorker("uia-worker")

        # Auxiliary worker on "Default" desktop (used only in sandboxed runner environments).
        self._aux_worker: _UIAWorker | None = None
        if sys.platform == "win32":
            try:
                import ctypes

                u32 = ctypes.windll.user32
                hdesk = u32.GetThreadDesktop(ctypes.windll.kernel32.GetCurrentThreadId())
                buf = ctypes.create_unicode_buffer(256)
                u32.GetUserObjectInformationW(hdesk, 2, buf, ctypes.sizeof(buf), None)
                cur_desk = buf.value
                if cur_desk and cur_desk.casefold() != "default":
                    hdef = u32.OpenDesktopW("Default", 0, False, 0x01FF)
                    if hdef:
                        u32.CloseDesktop(hdef)
                        self._aux_worker = _UIAWorker("uia-worker-default", "Default")
            except Exception:
                logger.debug("Failed checking desktop name in __init__", exc_info=True)

    # ------------------------------------------------------------------
    # Worker thread lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Shut down worker thread(s). Safe to call multiple times."""
        self._primary_worker.close()
        if self._aux_worker is not None:
            self._aux_worker.close()

    async def _run_on_worker(self, fn: Any, aux: bool = False) -> Any:
        """Run *fn* on the appropriate worker thread and await the result."""
        loop = asyncio.get_running_loop()
        worker = self._aux_worker if (aux and self._aux_worker is not None) else self._primary_worker
        fut = worker.submit(fn)
        return await loop.run_in_executor(None, fut.result)

    # ------------------------------------------------------------------
    # Internal search helpers (run on worker thread)
    # ------------------------------------------------------------------

    def _is_hud(self, control: Any) -> bool:
        """Return True if *control* is the Clavix HUD top-level window."""
        try:
            return bool(control.Name == "Clavix HUD")
        except Exception:
            logger.debug("_is_hud: could not read Name", exc_info=True)
            return False

    def _filter_ok(self, control: Any) -> bool:
        """Return True if *control* passes the candidate filter."""
        try:
            name = control.Name or ""
            aid = control.AutomationId or ""
            if not name and not aid:
                return False
            if not control.IsEnabled:
                return False
            if control.IsOffscreen:
                return False
            r = control.BoundingRectangle
            if r.width() == 0 or r.height() == 0:
                return False
        except Exception:
            logger.debug("_filter_ok: skipping control (property error)", exc_info=True)
            return False
        return True

    def _score_control(self, target_norm: str, control: Any) -> float:
        """Return the best score of Name and AutomationId against *target_norm*."""
        try:
            name_score = _score(target_norm, _normalize(control.Name or ""))
            aid_score = _score(target_norm, _normalize(control.AutomationId or ""))
        except Exception:
            logger.debug("_score_control: property error", exc_info=True)
            return 0.0
        return max(name_score, aid_score)

    def _tiebreak_key(self, item: tuple[float, Any, int]) -> tuple[float, int, int, int, int, int, int]:
        """Sorting key implementing the spec tie-break order (ascending = better).

        Order:
        1. Higher score (negated).
        2. IsControlElement (control elements preferred over private template parts).
        3. Control-type priority (ButtonControl, MenuItemControl, HyperlinkControl, ...).
        4. Smaller rect area.
        5. Smaller (top, left).
        6. Shallower depth.
        """
        score, ctrl, depth = item
        try:
            is_ctrl = 0 if getattr(ctrl, "IsControlElement", True) else 1
            ct = ctrl.ControlTypeName
            r = ctrl.BoundingRectangle
            area = r.width() * r.height()
            top = r.top
            left = r.left
        except Exception:
            logger.debug("_tiebreak_key: property error", exc_info=True)
            is_ctrl = 0
            ct = ""
            area = 0
            top = 0
            left = 0

        priority = (
            _CONTROL_TYPE_PRIORITY.index(ct) if ct in _CONTROL_TYPE_PRIORITY else len(_CONTROL_TYPE_PRIORITY)
        )
        return (-score, is_ctrl, priority, area, top, left, depth)

    def _try_scope(
        self,
        roots: list[Any],
        target_norm: str,
        auto: Any,
        scanned: list[int],
        best_score: list[float],
        is_aux: bool = False,
    ) -> UIElement | None:
        """Walk *roots* and return the best-matching element, or ``None``."""
        found: list[tuple[float, Any, int]] = []
        for root in roots:
            if root is None:
                continue
            if self._is_hud(root):
                continue
            controls_to_check: list[tuple[Any, int]] = [(root, 0)]
            try:
                for ctrl, depth in auto.WalkControl(root, includeTop=False, maxDepth=self.max_depth):
                    controls_to_check.append((ctrl, depth))
            except Exception:
                logger.debug("WalkControl error on %r", root, exc_info=True)

            for ctrl, depth in controls_to_check:
                if not self._filter_ok(ctrl):
                    continue
                scanned[0] += 1
                s = self._score_control(target_norm, ctrl)
                if s > best_score[0]:
                    best_score[0] = s
                if s >= self.min_match_score:
                    found.append((s, ctrl, depth))

        if not found:
            return None

        found.sort(key=self._tiebreak_key)
        _score_val, winner, _depth = found[0]
        try:
            r = winner.BoundingRectangle
            elem = UIElement(
                label=winner.Name or winner.AutomationId,
                control_type=winner.ControlTypeName,
                bounding_rect=(r.left, r.top, r.right, r.bottom),
                native=winner,
            )
            # Store aux flag in an internal attribute so invoke_element routes correctly.
            object.__setattr__(elem, "_from_aux", is_aux)
            return elem
        except Exception:
            logger.debug("_try_scope: could not build UIElement from winner", exc_info=True)
            return None

    def _search_once(
        self, target_norm: str, auto: Any, is_aux: bool = False
    ) -> tuple[UIElement | None, int, float]:
        """Run one full scoped search on the active desktop.

        Returns:
            ``(best_element, candidates_scanned, best_score)``
        """
        scanned: list[int] = [0]
        best_score: list[float] = [0.0]

        # Scope 1: foreground top-level window.
        try:
            fg = auto.GetForegroundControl()
            if fg is not None:
                top = fg.GetTopLevelControl()
                result = self._try_scope(
                    [top] if top is not None else [],
                    target_norm,
                    auto,
                    scanned,
                    best_score,
                    is_aux=is_aux,
                )
                if result is not None:
                    return result, scanned[0], best_score[0]
        except Exception:
            logger.debug("Scope 1 (foreground) error", exc_info=True)

        # Scope 2: taskbar.
        try:
            taskbar_roots: list[Any] = []
            if sys.platform == "win32":
                import ctypes

                hwnd = ctypes.windll.user32.FindWindowW("Shell_TrayWnd", None)
                if hwnd:
                    tray = auto.ControlFromHandle(hwnd)
                    if tray is not None:
                        taskbar_roots.append(tray)
            if not taskbar_roots:
                taskbar_roots = [
                    c for c in auto.GetRootControl().GetChildren() if c.ClassName == "Shell_TrayWnd"
                ]
            result = self._try_scope(taskbar_roots, target_norm, auto, scanned, best_score, is_aux=is_aux)
            if result is not None:
                return result, scanned[0], best_score[0]
        except Exception:
            logger.debug("Scope 2 (taskbar) error", exc_info=True)

        if is_aux:
            # Auxiliary worker is only queried for the system Taskbar / Start button
            return None, scanned[0], best_score[0]

        # Scope 3: all other root children.
        try:
            fg_top: Any = None
            try:
                fg = auto.GetForegroundControl()
                if fg is not None:
                    fg_top = fg.GetTopLevelControl()
            except Exception:
                logger.debug("Scope 3: could not get foreground top", exc_info=True)

            others: list[Any] = []
            for c in auto.GetRootControl().GetChildren():
                if c.ClassName == "Shell_TrayWnd":
                    continue
                if fg_top is not None:
                    try:
                        if c.NativeWindowHandle == fg_top.NativeWindowHandle:
                            continue
                    except Exception:
                        logger.debug("Scope 3: NativeWindowHandle compare failed", exc_info=True)
                others.append(c)

            result = self._try_scope(others, target_norm, auto, scanned, best_score, is_aux=is_aux)
            if result is not None:
                return result, scanned[0], best_score[0]
        except Exception:
            logger.debug("Scope 3 (other roots) error", exc_info=True)

        return None, scanned[0], best_score[0]

    # ------------------------------------------------------------------
    # GroundingInterface implementation
    # ------------------------------------------------------------------

    async def resolve_element(self, target_label: str) -> UIElement | None:
        """Find the best-matching element for *target_label* via the UIA tree.

        Polls every ``poll_interval_sec`` until ``timeout_sec`` elapses.

        Returns:
            The resolved :class:`UIElement`, or ``None`` on a miss.
        """
        target_norm = _normalize(target_label)

        def _search_primary() -> tuple[UIElement | None, int, float]:
            import uiautomation as auto

            return self._search_once(target_norm, auto, is_aux=False)

        def _search_aux() -> tuple[UIElement | None, int, float]:
            import uiautomation as auto

            return self._search_once(target_norm, auto, is_aux=True)

        import time as _time

        deadline = _time.monotonic() + self.timeout_sec
        while True:
            # First search on primary worker thread.
            elem, scanned, best = await self._run_on_worker(_search_primary, aux=False)
            if elem is not None:
                logger.debug("resolve %r: found on primary (score=%.3f)", target_label, best)
                return elem  # type: ignore[no-any-return]

            # If not found and aux worker exists, search on Default desktop.
            if self._aux_worker is not None:
                aux_elem, aux_scanned, aux_best = await self._run_on_worker(_search_aux, aux=True)
                scanned += aux_scanned
                best = max(best, aux_best)
                if aux_elem is not None:
                    logger.debug("resolve %r: found on aux (score=%.3f)", target_label, aux_best)
                    return aux_elem  # type: ignore[no-any-return]

            remaining = deadline - _time.monotonic()
            if remaining <= 0:
                logger.debug("resolve %r: timed out (best=%.3f)", target_label, best)
                return None
            await asyncio.sleep(min(self.poll_interval_sec, remaining))

    async def invoke_element(self, element: UIElement) -> bool:
        """Activate *element* using UIA interaction patterns.

        Tries InvokePattern, ExpandCollapsePattern, TogglePattern,
        SelectionItemPattern in order, then falls back to a coordinate click.
        COM errors for each pattern are caught and the next pattern is tried.

        Returns:
            ``True`` if any invocation succeeded, ``False`` otherwise.
        """
        is_aux = getattr(element, "_from_aux", False)

        def _invoke() -> bool:
            import comtypes  # type: ignore[import-untyped]
            import uiautomation as auto

            native = element.native

            # Re-resolve if stale.
            if native is None or not native.Exists(0, 0):
                target_norm = _normalize(element.label)
                resolved, _, _ = self._search_once(target_norm, auto, is_aux=is_aux)
                if resolved is None or resolved.native is None:
                    return False
                native = resolved.native

            # 1. InvokePattern
            try:
                pat = native.GetPattern(auto.PatternId.InvokePattern)
                if pat is not None and pat.Invoke():
                    return True
            except comtypes.COMError:
                logger.debug("InvokePattern.Invoke COM error", exc_info=True)
            except Exception:
                logger.debug("InvokePattern.Invoke error", exc_info=True)

            # 2. ExpandCollapsePattern
            try:
                pat = native.GetPattern(auto.PatternId.ExpandCollapsePattern)
                if pat is not None and pat.Expand():
                    return True
            except comtypes.COMError:
                logger.debug("ExpandCollapsePattern.Expand COM error", exc_info=True)
            except Exception:
                logger.debug("ExpandCollapsePattern.Expand error", exc_info=True)

            # 3. TogglePattern (Win11 Start button uses this, not Invoke)
            try:
                pat = native.GetPattern(auto.PatternId.TogglePattern)
                if pat is not None and pat.Toggle():
                    return True
            except comtypes.COMError:
                logger.debug("TogglePattern.Toggle COM error", exc_info=True)
            except Exception:
                logger.debug("TogglePattern.Toggle error", exc_info=True)

            # 4. SelectionItemPattern
            try:
                pat = native.GetPattern(auto.PatternId.SelectionItemPattern)
                if pat is not None and pat.Select():
                    return True
            except comtypes.COMError:
                logger.debug("SelectionItemPattern.Select COM error", exc_info=True)
            except Exception:
                logger.debug("SelectionItemPattern.Select error", exc_info=True)

            return False

        succeeded = await self._run_on_worker(_invoke, aux=is_aux)
        if succeeded:
            return True

        # 5. Last resort: coordinate click.
        return await self.click_coordinate(*element.center)

    async def click_coordinate(self, x: int, y: int) -> bool:
        """Click screen coordinates ``(x, y)`` using PyAutoGUI.

        Runs off the event-loop thread via ``asyncio.to_thread``.
        ``pyautogui.FailSafeException`` propagates to the dispatcher.

        Returns:
            ``True`` after issuing the click.
        """
        import pyautogui

        pyautogui.FAILSAFE = True
        await asyncio.to_thread(pyautogui.click, x, y)
        return True

    def list_window_titles(self) -> list[str]:
        """Return the names of all visible top-level windows.

        Runs on the worker thread (blocking).  Returns only ``WindowControl``
        and ``PaneControl`` children of the root that have a non-empty name
        and are not offscreen.
        """

        def _list() -> list[str]:
            import uiautomation as auto

            titles: list[str] = []
            try:
                for child in auto.GetRootControl().GetChildren():
                    try:
                        ct = child.ControlTypeName
                        if ct not in ("WindowControl", "PaneControl"):
                            continue
                        name = child.Name
                        if not name:
                            continue
                        if child.IsOffscreen:
                            continue
                        titles.append(name)
                    except Exception:
                        logger.debug("list_window_titles: child property error", exc_info=True)
            except Exception:
                logger.debug("list_window_titles: GetChildren error", exc_info=True)
            return titles

        fut = self._primary_worker.submit(_list)
        titles: list[str] = fut.result()  # type: ignore[no-any-return]

        # If primary has no windows and aux worker exists, collect from Default desktop.
        if not titles and self._aux_worker is not None:
            aux_fut = self._aux_worker.submit(_list)
            titles = aux_fut.result()  # type: ignore[no-any-return]

        return titles
