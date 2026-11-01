"""Disposable WebView2 HUD process and non-blocking JSON-lines transport."""

from __future__ import annotations

import argparse
import ctypes
import importlib
import json
import logging
import math
import os
import queue
import subprocess
import sys
import threading
from collections.abc import Callable
from ctypes import wintypes
from pathlib import Path
from typing import Any

from clavix.modules.feedback import AgentEvent

logger = logging.getLogger(__name__)
HUD_WEB = Path(__file__).with_name("hud_web")


class SafetyHUD:
    """Send progress to a separate HUD without waiting for process or pipe I/O.

    Args:
        on_abort: Thread-safe callback for the Stop button.
        hud_capture_visible: Include the overlay in captures instead of excluding it.
        _process_factory: Private subprocess injection point for headless tests.
    """

    def __init__(
        self,
        on_abort: Callable[[], None],
        hud_capture_visible: bool = False,
        *,
        _process_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._on_abort = on_abort
        self._capture_visible = hud_capture_visible
        self._process_factory = _process_factory or subprocess.Popen
        self._process: Any = None
        self._outgoing: queue.Queue[str] = queue.Queue(maxsize=256)
        self._closed = threading.Event()
        self._failed = threading.Event()
        self._lock = threading.Lock()
        self._failure_lock = threading.Lock()
        self._writer: threading.Thread | None = None
        self._reader: threading.Thread | None = None
        self.ready = threading.Event()

    def start(self) -> None:
        """Start once, off the caller's thread; construction remains cheap."""
        with self._lock:
            if self._writer is not None or self._closed.is_set():
                return
            try:
                self._writer = threading.Thread(target=self._run, name="clavix-hud-pipe", daemon=True)
                self._writer.start()
            except Exception as exc:
                self._fail(exc)

    def update_state(self, state: str) -> None:
        """Queue a state transition."""
        self._send({"type": "state", "state": state})

    def update_message(self, text: str) -> None:
        """Queue a spoken message for the subtitle."""
        self._send({"type": "message", "text": text})

    def update_event(self, event: AgentEvent) -> None:
        """Queue an event unchanged, including any future protocol fields."""
        self._send(event)

    def _send(self, event: AgentEvent) -> None:
        if self._closed.is_set() or self._failed.is_set():
            return
        try:
            line = json.dumps(event, ensure_ascii=True, allow_nan=False) + "\n"
            self._outgoing.put_nowait(line)
        except queue.Full:
            # An unresponsive renderer must never accumulate unbounded telemetry.
            self._fail("HUD pipe backlog exceeded 256 messages")
        except Exception as exc:
            self._fail(exc)

    def _fail(self, reason: object) -> None:
        with self._failure_lock:
            if not self._failed.is_set() and not self._closed.is_set():
                self._failed.set()
                logger.warning("HUD unavailable; dropping display updates: %s", reason)

    def _run(self) -> None:
        process: Any = None
        try:
            command = [sys.executable, "-m", "clavix.modules.hud"]
            if self._capture_visible:
                command.append("--capture-visible")
            process = self._process_factory(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                bufsize=1,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            with self._lock:
                self._process = process
            if self._closed.is_set():
                return
            self._reader = threading.Thread(
                target=self._read, args=(process,), name="clavix-hud-events", daemon=True
            )
            self._reader.start()
            while not self._closed.is_set() and not self._failed.is_set():
                if process.poll() is not None:
                    self._fail("process exited")
                    break
                try:
                    line = self._outgoing.get(timeout=0.1)
                except queue.Empty:
                    continue
                process.stdin.write(line)
                process.stdin.flush()
        except Exception as exc:
            self._fail(exc)
        finally:
            if process is not None:
                self._terminate(process)

    def _read(self, process: Any) -> None:
        try:
            for line in process.stdout:
                if self._closed.is_set():
                    return
                try:
                    message = json.loads(line)
                except (ValueError, TypeError):
                    continue  # Third-party diagnostics are not protocol messages.
                if not isinstance(message, dict):
                    continue
                if message.get("type") == "ready":
                    self.ready.set()
                elif message.get("type") == "abort":
                    try:
                        self._on_abort()
                    except Exception:
                        logger.exception("HUD abort callback failed")
            self._fail("HUD event pipe closed")
        except Exception as exc:
            self._fail(exc)

    @staticmethod
    def _terminate(process: Any) -> None:
        try:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=0.5)
        except Exception:
            try:
                process.kill()
                process.wait(timeout=0.5)
            except Exception:
                logger.debug("HUD process cleanup failed", exc_info=True)

    def close(self) -> None:
        """Dispose of the process even if its renderer or pipe is stalled."""
        self._closed.set()
        with self._lock:
            process = self._process
        if process is not None:
            self._terminate(process)
        # Never close stdin on this thread: a concurrent write can hold its lock.
        if self._writer is not None and self._writer is not threading.current_thread():
            if self._writer.is_alive():
                self._writer.join(timeout=0.5)


class _Margins(ctypes.Structure):
    _fields_ = [(name, ctypes.c_int) for name in ("left", "right", "top", "bottom")]


class _WindowsGlass:
    """Native window recipe from HUD_DESIGN section 3, in its prescribed order."""

    # ctypes DLL symbols are untyped and unavailable to Linux's platform stubs.
    user32: Any
    dwm: Any
    hwnd: int
    previous: int | None

    def __init__(self, capture_visible: bool) -> None:
        self.capture_visible = capture_visible
        if sys.platform != "win32":
            raise OSError("Windows glass is only available on Windows")
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.dwm = ctypes.WinDLL("dwmapi", use_last_error=True)
        self.hwnd = 0
        self._configure_functions()
        self.previous = self.user32.GetForegroundWindow()

    def _configure_functions(self) -> None:
        # Handles are pointer-sized; ctypes' default int return truncates them on x64.
        self.user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
        self.user32.FindWindowW.restype = wintypes.HWND
        self.user32.GetForegroundWindow.restype = wintypes.HWND
        self.user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
        self.user32.GetWindowLongW.restype = wintypes.LONG
        self.user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.LONG]
        self.user32.SetWindowLongW.restype = wintypes.LONG
        self.user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        self.user32.SetForegroundWindow.restype = wintypes.BOOL
        self.user32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
        self.user32.SetWindowDisplayAffinity.restype = wintypes.BOOL
        self.user32.GetSystemMetrics.argtypes = [ctypes.c_int]
        self.user32.GetSystemMetrics.restype = ctypes.c_int
        self.user32.SetWindowPos.argtypes = [
            wintypes.HWND,
            wintypes.HWND,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.UINT,
        ]
        self.user32.SetWindowPos.restype = wintypes.BOOL
        self.dwm.DwmSetWindowAttribute.argtypes = [
            wintypes.HWND,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        self.dwm.DwmSetWindowAttribute.restype = wintypes.LONG
        self.dwm.DwmExtendFrameIntoClientArea.argtypes = [wintypes.HWND, ctypes.POINTER(_Margins)]
        self.dwm.DwmExtendFrameIntoClientArea.restype = wintypes.LONG

    def apply(self, window: Any) -> None:
        window.events.loaded.wait()
        system = importlib.import_module("System")
        drawing = importlib.import_module("System.Drawing")
        form = window.native
        form.Invoke(system.Action(lambda: setattr(form, "BackColor", drawing.Color.Black)))
        self.hwnd = self.user32.FindWindowW(None, "Clavix HUD")
        if not self.hwnd:
            raise RuntimeError("Clavix HUD native window not found")
        style = self.user32.GetWindowLongW(self.hwnd, -20)
        self.user32.SetWindowLongW(self.hwnd, -20, style | 0x08000000 | 0x80)
        for attribute, value in ((20, 1), (33, 2), (34, 0xFFFFFFFE)):
            setting = wintypes.DWORD(value)
            self.dwm.DwmSetWindowAttribute(
                self.hwnd, attribute, ctypes.byref(setting), ctypes.sizeof(setting)
            )
        margins = _Margins(-1, -1, -1, -1)
        self.dwm.DwmExtendFrameIntoClientArea(self.hwnd, ctypes.byref(margins))
        backdrop = wintypes.DWORD(3)
        result = self.dwm.DwmSetWindowAttribute(
            self.hwnd, 38, ctypes.byref(backdrop), ctypes.sizeof(backdrop)
        )
        if result != 0:
            logger.info("Acrylic unavailable; using opaque HUD tint")
            window.evaluate_js("document.documentElement.style.setProperty('--glass', 'rgba(16,18,24,0.90)')")
        affinity = 0 if self.capture_visible else 0x11
        if not self.user32.SetWindowDisplayAffinity(self.hwnd, affinity):
            # Fail closed: a default HUD must not leak into screenshot/grounding input.
            raise RuntimeError("could not set HUD capture affinity")
        if self.user32.GetForegroundWindow() == self.hwnd and self.previous:
            self.user32.SetForegroundWindow(self.previous)

    def resize(self, width: float, height: float, dpr: float) -> None:
        if not self.hwnd or not all(math.isfinite(value) and value > 0 for value in (width, height, dpr)):
            return
        w, h = round(width * dpr), round(height * dpr)
        cx = self.user32.GetSystemMetrics(0) // 2
        self.user32.SetWindowPos(self.hwnd, 0, cx - w // 2, 16, w, h, 0x0004 | 0x0010)


class _PageAPI:
    def __init__(self, glass: _WindowsGlass) -> None:
        self._glass = glass
        self._output_lock = threading.Lock()

    def emit(self, kind: str) -> None:
        with self._output_lock:
            print(json.dumps({"type": kind}), flush=True)

    def abort(self) -> None:
        """Called by the page's Stop button."""
        self.emit("abort")

    def resize(self, width: float, height: float, dpr: float) -> None:
        """Resize in physical pixels, without activation or DPI double-scaling."""
        self._glass.resize(width, height, dpr)


def _serve(window: Any, glass: _WindowsGlass, api: _PageAPI) -> None:
    incoming: queue.Queue[AgentEvent] = queue.Queue(maxsize=256)

    def read_stdin() -> None:
        try:
            for line in sys.stdin:
                try:
                    event = json.loads(line)
                    if isinstance(event, dict):
                        incoming.put_nowait(event)
                except (ValueError, queue.Full):
                    continue
        finally:
            # EOF means the agent is gone. This also works during a stuck page load.
            os._exit(0)

    threading.Thread(target=read_stdin, name="clavix-hud-stdin", daemon=True).start()
    try:
        glass.apply(window)
        window.evaluate_js("window.hudResize?.()")
        api.emit("ready")
        while True:
            event = incoming.get()
            payload = json.dumps(event, ensure_ascii=True, allow_nan=False)
            window.evaluate_js(f"window.hud.apply({payload})")
    except Exception:
        logger.info("HUD renderer unavailable; continuing without a HUD", exc_info=True)
        window.destroy()


def main() -> None:
    """Own pywebview's main thread in the disposable HUD subprocess."""
    parser = argparse.ArgumentParser(description="Clavix glass HUD (JSON lines on stdin/stdout)")
    parser.add_argument("--capture-visible", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    if sys.platform != "win32":
        logger.info("The glass HUD requires Windows; continuing without a HUD")
        return
    try:
        import webview

        glass = _WindowsGlass(args.capture_visible)
        api = _PageAPI(glass)
        cx = glass.user32.GetSystemMetrics(0) // 2
        window = webview.create_window(
            "Clavix HUD",
            url=str(HUD_WEB / "index.html"),
            js_api=api,
            width=420,
            height=56,
            x=cx - 210,
            y=16,
            frameless=True,
            on_top=True,
            focus=False,
            shadow=False,
            transparent=True,
            easy_drag=False,
            min_size=(80, 30),
        )
        webview.start(_serve, (window, glass, api))
    except Exception:
        logger.info("WebView2 HUD unavailable; continuing without a HUD", exc_info=True)


if __name__ == "__main__":
    main()
