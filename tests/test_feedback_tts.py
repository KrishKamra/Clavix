"""Headless feedback/transport tests and explicitly marked Windows smoke tests."""

from __future__ import annotations

import asyncio
import io
import json
import queue
import sys
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from clavix.modules.feedback_tts import DoubleEscape, TTSFeedback
from clavix.modules.hud import SafetyHUD
from clavix.testing.contracts import FeedbackContract


class FakeEngine:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self.calls: list[tuple[int, str, object]] = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self.stopped = threading.Event()
        self.callback: Callable[..., None] | None = None
        self.current = ""

    def record(self, method: str, value: object = None) -> None:
        self.calls.append((threading.get_ident(), method, value))

    def connect(self, topic: str, callback: Callable[..., None]) -> None:
        assert topic == "started-word"
        self.record("connect", topic)
        self.callback = callback

    def setProperty(self, key: str, value: object) -> None:
        self.record("property", (key, value))

    def say(self, message: str) -> None:
        self.record("say", message)
        self.current = message
        self.messages.append(message)

    def runAndWait(self) -> None:
        self.record("run", self.current)
        if self.current:
            self.entered.set()
            while not self.release.wait(0.005):
                if self.callback is not None:
                    self.callback("word", 0, 4)

    def stop(self) -> None:
        self.record("stop")
        self.stopped.set()
        self.release.set()


@pytest.fixture
def engine() -> FakeEngine:
    return FakeEngine()


@pytest.fixture
def feedback(engine: FakeEngine) -> Iterator[TTSFeedback]:
    instance = TTSFeedback(abort_hotkey=False, _engine_factory=lambda: engine)
    yield instance
    instance.close()


class TestFakeFeedbackContract(FeedbackContract):
    @pytest.fixture
    def impl(self, feedback: TTSFeedback) -> TTSFeedback:
        return feedback


@pytest.mark.desktop
@pytest.mark.skipif(sys.platform != "win32", reason="Windows SAPI speech engine")
class TestTTSFeedbackContract(FeedbackContract):
    @pytest.fixture
    def impl(self) -> Iterator[TTSFeedback]:
        pytest.importorskip("pyttsx3")
        instance = TTSFeedback(volume=0.0, show_hud=False, abort_hotkey=False)
        yield instance
        instance.close()


def test_constructor_and_blank_speech_are_lazy() -> None:
    calls: list[int] = []
    instance = TTSFeedback(_engine_factory=lambda: calls.append(1))
    assert instance._worker is None
    assert instance._controls is None
    assert instance._hud is None
    asyncio.run(instance.speak(" \n "))
    assert not calls
    assert instance._worker is None
    instance.close()
    instance.close()


def test_nonblocking_is_under_50_ms(feedback: TTSFeedback, engine: FakeEngine) -> None:
    engine.release.clear()

    async def check() -> None:
        start = time.perf_counter()
        await feedback.speak("A long utterance")
        assert time.perf_counter() - start < 0.05

    asyncio.run(check())
    assert engine.entered.wait(1)
    assert not engine.release.is_set()


def test_warmup_configuration_and_single_thread(engine: FakeEngine) -> None:
    factory_threads: list[int] = []

    def factory() -> FakeEngine:
        factory_threads.append(threading.get_ident())
        return engine

    instance = TTSFeedback(
        rate=180, volume=0.4, voice_id="voice", abort_hotkey=False, _engine_factory=factory
    )
    try:
        asyncio.run(instance.speak("One", blocking=True))
        asyncio.run(instance.speak("Two", blocking=True))
    finally:
        instance.close()
    assert engine.messages == ["", "One", "Two"]
    assert factory_threads != [threading.get_ident()]
    assert {thread for thread, _, _ in engine.calls} == set(factory_threads)
    sequence = [(method, value) for _, method, value in engine.calls]
    assert sequence[:7] == [
        ("connect", "started-word"),
        ("property", ("rate", 180)),
        ("property", ("voice", "voice")),
        ("property", ("volume", 0.0)),
        ("say", ""),
        ("run", ""),
        ("property", ("volume", 0.4)),
    ]


def test_overflow_drops_oldest_nonblocking_but_keeps_blocking(engine: FakeEngine) -> None:
    engine.release.clear()
    instance = TTSFeedback(max_queue=2, abort_hotkey=False, _engine_factory=lambda: engine)

    async def check() -> None:
        await instance.speak("current")
        assert await asyncio.to_thread(engine.entered.wait, 1)
        await instance.speak("oldest")
        blocking = asyncio.create_task(instance.speak("must keep", blocking=True))
        await asyncio.sleep(0)
        await instance.speak("newer")
        await instance.speak("newest")
        engine.release.set()
        await asyncio.wait_for(blocking, 1)
        await instance.speak("barrier", blocking=True)

    try:
        asyncio.run(check())
        assert engine.messages == ["", "current", "must keep", "newer", "newest", "barrier"]
    finally:
        instance.close()


def test_blocking_waits_for_playback(feedback: TTSFeedback, engine: FakeEngine) -> None:
    engine.release.clear()

    async def check() -> None:
        task = asyncio.create_task(feedback.speak("wait", blocking=True))
        assert await asyncio.to_thread(engine.entered.wait, 1)
        assert not task.done()
        engine.release.set()
        await asyncio.wait_for(task, 1)

    asyncio.run(check())


def test_cancelled_future_does_not_break_worker(feedback: TTSFeedback, engine: FakeEngine) -> None:
    engine.release.clear()

    async def check() -> None:
        task = asyncio.create_task(feedback.speak("cancel wait", blocking=True))
        assert await asyncio.to_thread(engine.entered.wait, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        engine.release.set()
        await asyncio.wait_for(feedback.speak("next", blocking=True), 1)

    asyncio.run(check())
    assert "next" in engine.messages


def test_init_failure_prints_and_logs_once(
    capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    def broken() -> None:
        raise RuntimeError("no SAPI")

    instance = TTSFeedback(abort_hotkey=False, _engine_factory=broken)
    try:
        asyncio.run(instance.speak("One", blocking=True))
        asyncio.run(instance.speak("Two", blocking=True))
        assert capsys.readouterr().out == "[clavix] One\n[clavix] Two\n"
        assert caplog.text.count("speech engine unavailable") == 1
    finally:
        instance.close()


@pytest.mark.parametrize("state", ["IDLE", "COMPLETED", "FAILED"])
def test_terminal_states_never_abort(state: str) -> None:
    escape = DoubleEscape()
    escape.update_state(state)
    assert not escape.press(1.0)
    assert not escape.press(1.1)


def test_double_escape_window_and_task_boundary() -> None:
    escape = DoubleEscape(0.6)
    escape.update_state("PLANNING")
    assert not escape.press(0.0)
    assert escape.press(0.5)
    assert not escape.press(1.0)
    assert not escape.press(2.0)
    escape.update_state("DISPATCHING")
    assert escape.press(2.1)
    assert not escape.press(3.0)
    escape.update_state("FAILED")
    escape.update_state("PLANNING")
    assert not escape.press(3.1)


def test_abort_interrupts_on_owner_thread_and_clears_queue(feedback: TTSFeedback, engine: FakeEngine) -> None:
    calls: list[int] = []
    feedback.bind_abort(lambda: calls.append(1))
    engine.release.clear()

    async def check() -> None:
        await feedback.on_state_change("IDLE", "DISPATCHING")
        await feedback.speak("speaking")
        assert await asyncio.to_thread(engine.entered.wait, 1)
        await feedback.speak("obsolete")
        feedback._on_escape(True)
        feedback._on_escape(True)  # held key / repeat
        assert not calls
        feedback._on_escape(False)
        feedback._on_escape(True)
        assert calls == [1]
        await asyncio.wait_for(feedback.speak("barrier", blocking=True), 1)

    asyncio.run(check())
    assert engine.stopped.is_set()
    assert "obsolete" not in engine.messages
    assert "Aborted." in engine.messages
    assert len({thread for thread, _, _ in engine.calls}) == 1
    assert engine.calls[0][0] != threading.get_ident()


def test_close_resolves_pending_waiters_and_is_idempotent(feedback: TTSFeedback, engine: FakeEngine) -> None:
    engine.release.clear()

    async def check() -> None:
        playing = asyncio.create_task(feedback.speak("playing", blocking=True))
        assert await asyncio.to_thread(engine.entered.wait, 1)
        pending = asyncio.create_task(feedback.speak("pending", blocking=True))
        await asyncio.sleep(0)
        feedback.close()
        feedback.close()
        await asyncio.wait_for(asyncio.gather(playing, pending), 1)
        await feedback.speak("after close", blocking=True)

    asyncio.run(check())
    assert "pending" not in engine.messages
    assert "after close" not in engine.messages
    assert feedback._worker is not None and not feedback._worker.is_alive()


@pytest.mark.parametrize("source", ["stop", "escape"])
def test_abort_reaches_runtime_and_fails_task(feedback: TTSFeedback, source: str) -> None:
    from clavix.config import ClavixConfig
    from clavix.core.planner import MockLLMClient, Planner
    from clavix.modules.acoustic import ScriptedAcoustic
    from clavix.runtime import build_runtime
    from clavix.testing.fakes import FakeGrounding, FakeNativeOps

    native = FakeNativeOps()
    runtime = build_runtime(
        ClavixConfig(),
        acoustic=ScriptedAcoustic([]),
        grounding=FakeGrounding(),
        feedback=feedback,
        native=native,
    )
    planner = Planner(
        MockLLMClient(
            [
                {
                    "steps": [
                        {
                            "step_id": 1,
                            "action_type": "DETERMINISTIC",
                            "action_name": "launch_url",
                            "payload": {"url": "https://github.com"},
                        }
                    ]
                }
            ]
        )
    )
    agent = runtime.make_agent(planner)

    async def abort_before_dispatch(previous: str, current: str) -> None:
        if current == "DISPATCHING":
            if source == "stop":
                feedback._handle_abort()
            else:
                feedback._on_escape(True)
                feedback._on_escape(False)
                feedback._on_escape(True)

    agent.add_listener(abort_before_dispatch)
    outcome = asyncio.run(agent.run("open github"))
    assert runtime.dispatcher.aborted
    assert not outcome.success
    assert outcome.final_state == "FAILED"
    assert "aborted" in (outcome.error_message or "")
    assert not native.calls


class FakeOutput:
    def __init__(self) -> None:
        self.lines: queue.Queue[str | None] = queue.Queue()

    def __iter__(self) -> Iterator[str]:
        while (line := self.lines.get()) is not None:
            yield line


class FakeInput(io.StringIO):
    def __init__(self) -> None:
        super().__init__()
        self.written = threading.Event()
        self.allow_write = threading.Event()
        self.allow_write.set()

    def write(self, value: str) -> int:
        self.allow_write.wait()
        result = super().write(value)
        self.written.set()
        return result


class FakeProcess:
    def __init__(self) -> None:
        self.stdin = FakeInput()
        self.stdout = FakeOutput()
        self.returncode: int | None = None
        self.dead = threading.Event()

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.returncode = 0
        self.stdin.allow_write.set()
        self.stdout.lines.put(None)
        self.dead.set()

    def kill(self) -> None:
        self.terminate()

    def wait(self, timeout: float) -> int:
        assert self.dead.wait(timeout)
        return 0


@pytest.fixture
def process() -> Iterator[FakeProcess]:
    instance = FakeProcess()
    yield instance
    instance.terminate()


def eventually(predicate: Callable[[], bool]) -> None:
    deadline = time.monotonic() + 2
    while not predicate():
        assert time.monotonic() < deadline, "condition did not become true"
        time.sleep(0.005)


def test_hud_json_and_abort(process: FakeProcess) -> None:
    aborted = threading.Event()
    hud = SafetyHUD(aborted.set, _process_factory=lambda *args, **kwargs: process)
    try:
        hud.start()
        hud.start()
        event = {"type": "transcript", "text": 'quoted "text"\nand café', "asr_ms": 12, "future": [1]}
        hud.update_state("PLANNING")
        hud.update_message("Hello\nWorld")
        hud.update_event(event)
        eventually(lambda: len(process.stdin.getvalue().splitlines()) == 3)
        assert [json.loads(line) for line in process.stdin.getvalue().splitlines()] == [
            {"type": "state", "state": "PLANNING"},
            {"type": "message", "text": "Hello\nWorld"},
            event,
        ]
        for line in ["diagnostic\n", "[]\n", '{"type":"future"}\n', '{"type":"ready"}\n']:
            process.stdout.lines.put(line)
        assert hud.ready.wait(1)
        process.stdout.lines.put('{"type":"abort"}\n')
        assert aborted.wait(1)
    finally:
        hud.close()
        hud.close()
    assert process.poll() == 0


@pytest.mark.parametrize("visible", [False, True])
def test_capture_visibility_argument(process: FakeProcess, visible: bool) -> None:
    commands: list[list[str]] = []

    def factory(command: list[str], **kwargs: Any) -> FakeProcess:
        commands.append(command)
        return process

    hud = SafetyHUD(lambda: None, hud_capture_visible=visible, _process_factory=factory)
    try:
        hud.start()
        eventually(lambda: bool(commands))
        assert ("--capture-visible" in commands[0]) is visible
        assert commands[0][:3] == [sys.executable, "-m", "clavix.modules.hud"]
    finally:
        hud.close()


def test_dead_hud_drops_messages_once(process: FakeProcess, caplog: pytest.LogCaptureFixture) -> None:
    process.terminate()
    hud = SafetyHUD(lambda: None, _process_factory=lambda *args, **kwargs: process)
    try:
        hud.start()
        eventually(hud._failed.is_set)
        start = time.perf_counter()
        for _ in range(1000):
            hud.update_state("PLANNING")
        assert time.perf_counter() - start < 0.05
        assert caplog.text.count("HUD unavailable") == 1
    finally:
        hud.close()


def test_stalled_pipe_cannot_block_agent_or_close(process: FakeProcess) -> None:
    process.stdin.allow_write.clear()
    hud = SafetyHUD(lambda: None, _process_factory=lambda *args, **kwargs: process)
    hud.start()
    start = time.perf_counter()
    for _ in range(1000):
        hud.update_message("bounded backlog")
    assert time.perf_counter() - start < 0.05
    start = time.perf_counter()
    hud.close()
    assert time.perf_counter() - start < 2


def test_close_during_slow_process_start(process: FakeProcess) -> None:
    entered, release = threading.Event(), threading.Event()

    def factory(*args: Any, **kwargs: Any) -> FakeProcess:
        entered.set()
        release.wait(2)
        return process

    hud = SafetyHUD(lambda: None, _process_factory=factory)
    hud.start()
    assert entered.wait(1)
    hud.close()
    release.set()
    assert process.dead.wait(1)


def test_hud_start_failure_is_nonfatal(caplog: pytest.LogCaptureFixture) -> None:
    def broken(*args: Any, **kwargs: Any) -> None:
        raise OSError("no executable")

    hud = SafetyHUD(lambda: None, _process_factory=broken)
    hud.start()
    eventually(hud._failed.is_set)
    hud.update_state("PLANNING")
    hud.close()
    assert caplog.text.count("HUD unavailable") == 1


def test_feedback_forwards_hud_events_and_stop(
    monkeypatch: pytest.MonkeyPatch, feedback: TTSFeedback, process: FakeProcess
) -> None:
    import clavix.modules.feedback_tts as module

    def factory(callback: Callable[[], None], hud_capture_visible: bool = False) -> SafetyHUD:
        assert hud_capture_visible
        return SafetyHUD(callback, _process_factory=lambda *args, **kwargs: process)

    monkeypatch.setattr(module, "SafetyHUD", factory)
    feedback._show_hud = True
    feedback._hud_capture_visible = True
    aborted = threading.Event()
    feedback.bind_abort(aborted.set)

    async def send() -> None:
        await feedback.on_state_change("IDLE", "PLANNING")
        await feedback.on_event({"type": "step", "id": 1, "status": "ok", "attempt": 0})
        await feedback.speak("summary", blocking=True)

    asyncio.run(send())
    eventually(lambda: len(process.stdin.getvalue().splitlines()) == 3)
    assert [json.loads(line)["type"] for line in process.stdin.getvalue().splitlines()] == [
        "state",
        "step",
        "message",
    ]
    process.stdout.lines.put('{"type":"abort"}\n')
    assert aborted.wait(1)


@pytest.mark.desktop
@pytest.mark.skipif(sys.platform != "win32", reason="Windows WebView2 glass HUD")
@pytest.mark.parametrize("capture_visible", [False, True])
def test_desktop_hud_smoke(capture_visible: bool) -> None:
    pytest.importorskip("webview")
    hud = SafetyHUD(lambda: None, hud_capture_visible=capture_visible)
    try:
        hud.start()
        assert hud.ready.wait(10), "HUD did not report ready"
        for state in (
            "IDLE",
            "ACOUSTIC_INGESTION",
            "PLANNING",
            "DISPATCHING",
            "VERIFYING",
            "FEEDBACK",
            "COMPLETED",
            "FAILED",
        ):
            hud.update_state(state)
        for event in (
            {"type": "transcript", "text": "open notepad", "asr_ms": 395, "confidence": 0.9},
            {
                "type": "plan",
                "steps": [{"id": 1, "action": "run_command", "verb": "Run", "target": "notepad"}],
                "llm_ms": 440,
            },
            {"type": "step", "id": 1, "status": "running", "attempt": 0},
            {"type": "step", "id": 1, "status": "retry", "attempt": 1},
            {"type": "step", "id": 1, "status": "ok", "attempt": 1},
            {"type": "timing", "asr_ms": 395, "llm_ms": 440, "exec_ms": 100, "success": True},
            {"type": "future-event", "unknown": True},
        ):
            hud.update_event(event)
        hud.update_message("Done. Completed 1 step.")
        assert not hud._failed.is_set()
    finally:
        start = time.perf_counter()
        hud.close()
        assert time.perf_counter() - start < 2
