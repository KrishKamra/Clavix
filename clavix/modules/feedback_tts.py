"""Lazy speech worker, glass HUD and global double-Escape safety control."""

from __future__ import annotations

import asyncio
import atexit
import logging
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from clavix.modules.feedback import AgentEvent, FeedbackInterface
from clavix.modules.hud import SafetyHUD

logger = logging.getLogger(__name__)
_FINAL = frozenset({"IDLE", "COMPLETED", "FAILED"})


class DoubleEscape:
    """Recognise two distinct Escape presses during the same active task."""

    def __init__(self, window_sec: float = 0.6) -> None:
        self.window_sec = window_sec
        self.state = "IDLE"
        self._previous: float | None = None

    def update_state(self, state: str) -> None:
        """Forget the previous press at a task boundary."""
        self.state = state
        if state in _FINAL:
            self._previous = None

    def press(self, now: float) -> bool:
        """Return whether an Escape press at monotonic time ``now`` aborts."""
        if self.state in _FINAL:
            self._previous = None
            return False
        previous, self._previous = self._previous, now
        if previous is not None and 0 <= now - previous <= self.window_sec:
            self._previous = None
            return True
        return False


@dataclass
class _Speech:
    message: str
    done: asyncio.Future[None] | None = None
    loop: asyncio.AbstractEventLoop | None = None

    def finish(self) -> None:
        if self.done is None or self.loop is None:
            return
        future = self.done

        def resolve() -> None:
            if not future.done():
                future.set_result(None)

        try:
            self.loop.call_soon_threadsafe(resolve)
        except RuntimeError:  # Caller already closed its event loop.
            pass


class TTSFeedback(FeedbackInterface):
    """Queue speech without blocking asyncio; all engine calls share one thread.

    Args:
        rate: Words per minute.
        volume: Speech volume, from zero to one.
        voice_id: System voice identifier, or the default voice.
        max_queue: Maximum pending non-blocking utterances; oldest are dropped.
        show_hud: Enable the separate glass HUD process.
        abort_hotkey: Enable global double-Escape while a task is active.
        double_press_window_sec: Maximum gap between Escape presses.
        hud_capture_visible: Include the HUD in captures (default: excluded).
        _engine_factory: Private injection point for headless tests.
    """

    def __init__(
        self,
        rate: int = 190,
        volume: float = 1.0,
        voice_id: str | None = None,
        max_queue: int = 5,
        show_hud: bool = False,
        abort_hotkey: bool = True,
        double_press_window_sec: float = 0.6,
        hud_capture_visible: bool = False,
        *,
        _engine_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._rate = rate
        self._volume = volume
        self._voice_id = voice_id
        self._max_queue = max(1, max_queue)
        self._engine_factory = _engine_factory
        self._show_hud = show_hud
        self._hud_capture_visible = hud_capture_visible
        self._abort_hotkey = abort_hotkey
        self._escape = DoubleEscape(double_press_window_sec)
        self._escape_down = False
        self._jobs: queue.Queue[_Speech] = queue.Queue()
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._interrupt = threading.Event()
        self._worker_failed = False
        self._worker: threading.Thread | None = None
        self._controls: threading.Thread | None = None
        self._listener: Any = None  # pynput is an optional, untyped boundary.
        self._hud: SafetyHUD | None = None
        self._closed = False
        self._draining = False
        self._controls_started = False
        self._exit_registered = False

    async def speak(self, message: str, blocking: bool = False) -> None:
        """Enqueue speech; optionally await completion without blocking the loop."""
        if not message.strip() or self._closed:
            return
        if self._hud is not None:
            self._hud.update_message(message)
        loop = asyncio.get_running_loop() if blocking else None
        done = loop.create_future() if loop is not None else None
        self._enqueue(_Speech(message, done, loop))
        if done is not None:
            await done

    def _enqueue(self, job: _Speech) -> None:
        with self._lock:
            if self._closed or self._draining:
                job.finish()
                return
            if self._worker_failed:
                print(f"[clavix] {job.message}")
                job.finish()
                return
            if job.done is None:
                pending = self._take_pending()
                if sum(item.done is None for item in pending) >= self._max_queue:
                    oldest = next(i for i, item in enumerate(pending) if item.done is None)
                    pending.pop(oldest)
                    logger.warning("speech queue full; dropped oldest pending message")
                for item in pending:
                    self._jobs.put_nowait(item)
            self._jobs.put_nowait(job)
            self._wake.set()
            if self._worker is None:
                try:
                    self._worker = threading.Thread(
                        target=self._speech_loop, name="clavix-speech", daemon=True
                    )
                    self._worker.start()
                    self._register_exit()
                except Exception:
                    logger.exception("could not start speech worker; using console feedback")
                    self._worker_failed = True
                    for item in self._take_pending():
                        print(f"[clavix] {item.message}")
                        item.finish()

    def _take_pending(self) -> list[_Speech]:
        # Caller holds _lock; the worker also takes jobs under this lock.
        pending = []
        while True:
            try:
                pending.append(self._jobs.get_nowait())
            except queue.Empty:
                return pending

    def _speech_loop(self) -> None:
        engine: Any = None
        try:
            try:
                if self._engine_factory is None:
                    import pyttsx3

                    engine = pyttsx3.init()
                else:
                    engine = self._engine_factory()

                def on_word(name: str, location: int, length: int) -> None:
                    # SAPI delivers word callbacks inside runAndWait on this thread.
                    # The hook/button only sets an Event; it never touches the engine.
                    if self._interrupt.is_set():
                        engine.stop()

                engine.connect("started-word", on_word)
                engine.setProperty("rate", self._rate)
                if self._voice_id is not None:
                    engine.setProperty("voice", self._voice_id)
                engine.setProperty("volume", 0.0)
                engine.say("")
                engine.runAndWait()
                engine.setProperty("volume", self._volume)
            except Exception:
                logger.exception("speech engine unavailable; using console feedback")
                if engine is not None:
                    try:
                        engine.stop()
                    except Exception:
                        logger.debug("failed engine cleanup", exc_info=True)
                engine = None

            while True:
                self._wake.wait()
                if self._interrupt.is_set():
                    if engine is not None:
                        engine.stop()
                    self._interrupt.clear()
                with self._lock:
                    if self._closed:
                        break
                    try:
                        job = self._jobs.get_nowait()
                    except queue.Empty:
                        if self._draining:
                            break
                        self._wake.clear()
                        continue
                try:
                    if engine is None:
                        print(f"[clavix] {job.message}")
                    else:
                        engine.say(job.message)
                        engine.runAndWait()
                except Exception:
                    logger.exception("speech playback failed; using console feedback")
                    print(f"[clavix] {job.message}")
                finally:
                    job.finish()
        except Exception:
            logger.exception("speech worker stopped unexpectedly")
            with self._lock:
                self._worker_failed = True
        finally:
            if engine is not None:
                try:
                    engine.stop()
                except Exception:
                    logger.debug("speech engine cleanup failed", exc_info=True)
                engine = None  # Release the callback/engine cycle on its owning thread.
            with self._lock:
                for job in self._take_pending():
                    if self._worker_failed:
                        print(f"[clavix] {job.message}")
                    job.finish()

    async def on_state_change(self, previous: str, current: str) -> None:
        """Start controls lazily and forward the live state to the HUD."""
        with self._lock:
            if self._closed:
                return
            self._escape.update_state(current)
            if not self._controls_started:
                self._controls_started = True
                self._register_exit()
                if self._show_hud:
                    self._hud = SafetyHUD(self._handle_abort, hud_capture_visible=self._hud_capture_visible)
                    self._hud.start()
                if self._abort_hotkey:
                    try:
                        self._controls = threading.Thread(
                            target=self._start_hotkey, name="clavix-hotkey-start", daemon=True
                        )
                        self._controls.start()
                    except Exception:
                        logger.info("global abort hotkey unavailable", exc_info=True)
            if self._hud is not None:
                self._hud.update_state(current)

    async def on_event(self, event: AgentEvent) -> None:
        """Forward structured progress without changing the protocol payload."""
        if self._hud is not None and not self._closed:
            self._hud.update_event(event)

    def _start_hotkey(self) -> None:
        try:
            from pynput import keyboard

            def on_press(key: Any) -> None:
                if key == keyboard.Key.esc:
                    self._on_escape(True)

            def on_release(key: Any) -> None:
                if key == keyboard.Key.esc:
                    self._on_escape(False)

            listener = keyboard.Listener(on_press=on_press, on_release=on_release)
            with self._lock:
                if self._closed:
                    return
                self._listener = listener
                listener.start()
        except Exception:
            logger.info("global abort hotkey unavailable", exc_info=True)

    def _on_escape(self, pressed: bool) -> None:
        with self._lock:
            if self._closed:
                return
            if not pressed:
                self._escape_down = False
                return
            if self._escape_down:  # OS auto-repeat is not a second keypress.
                return
            self._escape_down = True
            abort = self._escape.press(time.monotonic())
        if abort:
            self._handle_abort()

    def _handle_abort(self) -> None:
        with self._lock:
            if self._closed or self._escape.state in _FINAL:
                return
            self._interrupt.set()
            for job in self._take_pending():
                if job.done is not None:
                    self._jobs.put_nowait(job)
            self._wake.set()
        try:
            self.request_abort()
        except Exception:
            logger.exception("abort callback failed")
        if self._hud is not None:
            self._hud.update_message("Aborted.")
        self._enqueue(_Speech("Aborted."))

    def _register_exit(self) -> None:
        if not self._exit_registered:
            atexit.register(self._at_exit)
            self._exit_registered = True

    def _at_exit(self) -> None:
        # The one-shot CLI exits just after enqueueing its summary. Give that
        # short queue a bounded chance to finish before tearing down the controls.
        with self._lock:
            self._draining = True
            self._wake.set()
        if self._worker is not None and self._worker.is_alive():
            self._worker.join(timeout=10.0)
        self.close()

    def close(self) -> None:
        """Stop controls and speech; safe to call repeatedly or after failed startup."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._interrupt.set()
            self._wake.set()
            for job in self._take_pending():
                job.finish()
            listener, self._listener = self._listener, None
        if listener is not None:
            try:
                listener.stop()
            except Exception:
                logger.debug("hotkey cleanup failed", exc_info=True)
        if self._hud is not None:
            self._hud.close()
        if self._worker is not None and self._worker is not threading.current_thread():
            if self._worker.is_alive():
                self._worker.join(timeout=1.0)
        if self._exit_registered:
            atexit.unregister(self._at_exit)
