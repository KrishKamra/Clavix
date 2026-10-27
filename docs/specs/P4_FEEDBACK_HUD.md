# P4 Spec: Feedback (TTS) and Safety HUD

**Owner:** Person 4  **Phase:** 2 (milestones P4-M1, P4-M2, P4-M3)  **Platform:** Windows 11 first

## 1. Mission

Implement `TTSFeedback`, a `FeedbackInterface` that:
- speaks messages without stalling the agent loop;
- optionally shows an always-on-top HUD with the agent's live state and an **Abort** button;
- listens for a global double-Escape hotkey that aborts the running task.

## 2. Read first, in this order

1. `AGENTS.md`.
2. `clavix/modules/feedback.py`: **your contract**. Do not edit it.
3. `clavix/core/state_machine.py`: `AgentState`, `TRANSITIONS`, and where `speak` and
   `on_state_change` are called (`_announce`, `_transition`).
4. `clavix/execution/dispatcher.py`: `abort()`, and the `speak_feedback` route (`_dispatch_feedback`).
5. `clavix/testing/contracts.py`, class `FeedbackContract`.

## 3. Ownership

| Action            | Paths |
|-------------------|-------|
| Create and own    | `clavix/modules/feedback_tts.py`, `clavix/modules/hud.py`, `tests/test_feedback_tts.py` |
| Never edit        | `clavix/modules/feedback.py`, any other file under `clavix/` and `configs/` |

## 4. The contract (verbatim from `clavix/modules/feedback.py`)

```python
class FeedbackInterface(ABC):
    @abstractmethod
    async def speak(self, message: str, blocking: bool = False) -> None: ...
    async def on_state_change(self, previous: str, current: str) -> None: ...  # default no-op
    def bind_abort(self, abort: Callable[[], None]) -> None: ...   # stores the callback; do NOT override
    def request_abort(self) -> None: ...                            # calls it; do NOT override
```

- `speak(message, blocking=False)`: called for `speak_feedback` steps and for the end-of-task
  summary. When `blocking=False` it **must return in at most 0.5 s** (the contract enforces this).
- `on_state_change(previous, current)`: called on every transition with `AgentState` values
  as strings. The states are `IDLE`, `ACOUSTIC_INGESTION`, `PLANNING`, `DISPATCHING`,
  `VERIFYING`, `FEEDBACK`, `COMPLETED` and `FAILED`. It must return quickly and must never raise;
  exceptions are logged and swallowed by the agent, but they are bugs.
- `bind_abort` is called once by `clavix/runtime.py:build_runtime` with `Dispatcher.abort`,
  which is thread-safe. Your HUD button and your hotkey call `self.request_abort()`.

## 5. Dependencies

Already declared as the `feedback` extra: `pyttsx3>=2.90` (lockfile: 2.99) and `pynput>=1.7.6`
(lockfile: 1.8.2). The HUD uses `tkinter` from the standard library.

```powershell
uv sync --extra feedback
```

## 6. Constructor and configuration

```python
class TTSFeedback(FeedbackInterface):
    def __init__(
        self,
        rate: int = 190,              # words per minute (pyttsx3 "rate"; engine default is 200)
        volume: float = 1.0,          # 0.0 to 1.0
        voice_id: str | None = None,  # pyttsx3 voice id; None = system default
        max_queue: int = 5,           # pending non-blocking messages kept; oldest dropped
        show_hud: bool = False,
        abort_hotkey: bool = True,    # global double-Escape
        double_press_window_sec: float = 0.6,
    ) -> None: ...
```

The constructor must be cheap: no engine, thread, window or listener is created in
`__init__`. Start the speech worker lazily on the first `speak` call. Start the HUD and the
hotkey listener on the first `on_state_change` call. Provide `close()`, which stops all of
them and is safe to call twice.

Config entry at integration:

```yaml
modules:
  feedback:
    class: clavix.modules.feedback_tts:TTSFeedback
    options: {rate: 190, volume: 1.0, show_hud: true, abort_hotkey: true}
```

## 7. Required behaviour

### 7.1 Speech worker (P4-M1)

- One daemon thread owns the `pyttsx3` engine. Never call the engine from any other thread.
- The worker takes jobs `(message, done: asyncio.Future | None, loop)` from a
  `queue.Queue`. For each job it runs `engine.say(message)` then `engine.runAndWait()`, and
  afterwards, if `done` is set, calls `loop.call_soon_threadsafe(done.set_result, None)`.
- `speak(msg, blocking=False)`: enqueue and return immediately. If more than `max_queue`
  non-blocking jobs are pending, drop the oldest one and log a warning.
- `speak(msg, blocking=True)`: enqueue with a future and `await` it.
- Empty or whitespace-only messages return immediately without speaking.
- **Warm-up:** when the worker starts, run `engine.say("")` and `runAndWait()` with volume 0,
  then restore the configured volume. The first real utterance otherwise pays about 2–3 s of
  engine start-up.
- If `pyttsx3.init()` fails (no audio device or SAPI), log one error and fall back to printing
  `[clavix] <message>`. Never raise from `speak`.
- On abort (see 7.3), clear pending non-blocking jobs and call `engine.stop()`.

### 7.2 HUD (P4-M2), `clavix/modules/hud.py`

- Class `SafetyHUD(on_abort: Callable[[], None])` with methods `start()`,
  `update_state(state: str)`, `update_message(text: str)` and `close()`.
- Tk is **not** thread-safe. Run the whole Tk mainloop on its own daemon thread, and create
  every widget on that thread. Other threads only put updates on a `queue.Queue`, which the
  Tk thread drains every 50 ms with `root.after(50, ...)`.
- Window: always on top (`root.attributes("-topmost", True)`), no taskbar entry
  (`root.overrideredirect(True)`), about 280×90 px, top-right corner, `-alpha 0.9`.
- Contents: the state name, colour-coded (IDLE grey; PLANNING, DISPATCHING and VERIFYING
  blue; COMPLETED green; FAILED red), the last spoken message (truncated to 80 characters),
  and a red **Abort** button. The button is enabled only in non-terminal states; pressing it
  calls `on_abort`.
- `TTSFeedback.on_state_change` forwards to `update_state`, and `speak` forwards to
  `update_message`.

### 7.3 Global abort hotkey (P4-M3)

- Use `pynput.keyboard.Listener(on_press=...)`. It runs on its own thread, so start it with
  `.start()` and stop it with `.stop()`.
- Abort when `keyboard.Key.esc` is pressed twice within `double_press_window_sec`, and only
  while the last state seen is not `IDLE`, `COMPLETED` or `FAILED`. A single Esc is common in
  normal app use and must not abort.
- On abort: call `self.request_abort()`, stop speech (7.1), and speak "Aborted." non-blocking.

### 7.4 Verified reference facts

| Fact | Value |
|------|-------|
| pyttsx3 2.99 engine methods | `say`, `runAndWait`, `stop`, `startLoop`, `endLoop`, `iterate`, `connect`, `setProperty`, `getProperty`, `isBusy` |
| Default rate / voices on the dev machine | 200 wpm, 2 voices |
| Shared engine, 3 consecutive `runAndWait()` calls on one worker thread | Works (no hang): 2919 ms cold, then 648 ms and 211 ms |
| `pynput.keyboard.Key.esc` | exists in 1.8.2 |

Before using any other API, check it in the installed version with `inspect`.

## 8. Tests (`tests/test_feedback_tts.py`)

```python
import pytest
from clavix.testing.contracts import FeedbackContract

class TestTTSFeedbackContract(FeedbackContract):
    @pytest.fixture
    def impl(self):
        from clavix.modules.feedback_tts import TTSFeedback
        fb = TTSFeedback(volume=0.0, show_hud=False, abort_hotkey=False)
        yield fb
        fb.close()
```

Mark the contract class `@pytest.mark.desktop`, since it starts a real speech engine (muted).

Also write **unmarked** unit tests that inject a fake engine. Give the constructor a private
`_engine_factory: Callable[[], Any] | None = None` keyword argument. `None` means
"import `pyttsx3` lazily and call `pyttsx3.init()`". CI installs no extras, so `pyttsx3` and
`pynput` must never be imported at module top level. The tests then need no audio:
- Non-blocking calls return in under 50 ms with the fake engine.
- Queue overflow drops the oldest message.
- A blocking call resolves after the fake `runAndWait` returns.
- An `init` failure falls back to printing.
- The double-Esc logic, as a pure function or class: two presses within the window abort;
  presses further apart don't; nothing aborts in a terminal state.
- `close()` is idempotent.

Do not unit-test the HUD's Tk widgets in CI. Add one `desktop` smoke test that starts the HUD,
sends three state updates and closes it.

## 9. Definition of done

- [ ] `FeedbackContract` passes (`uv run pytest tests/test_feedback_tts.py`).
- [ ] `uv run pytest -m "not desktop"` passes on Linux without audio.
- [ ] `ruff`, `ruff format --check` and `mypy` are clean.
- [ ] Manual check: with `show_hud: true`, `uv run clavix "open github"` shows
      PLANNING, then DISPATCHING, then COMPLETED in the HUD, and speaks the summary.
- [ ] Manual check: during `uv run clavix-bench --planner reference --tiers 3`, pressing Abort
      or double-Esc ends the task as FAILED with "aborted" in the error.
- [ ] No file outside §3 is modified.

## 10. Out of scope (Phase 2)

- Confirmation prompts before risky actions (a contract change; raise it with P1 for Phase 3).
- Neural or online TTS voices.
- Step-level HUD details (current action). That needs new contract events; request them from P1 (backlog P1-6).
