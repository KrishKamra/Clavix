# P3 Spec: Structural Grounding (Windows UI Automation)

**Owner:** Person 3  **Phase:** 2 (milestones P3-M1, P3-M2)  **Platform:** Windows 11

## 1. Mission

Implement `UIAGrounding`, a `GroundingInterface` that finds an on-screen element by its
accessible name using the Windows UI Automation tree, and activates it. This is what moves
Tier 3 benchmark tasks from 0/3 to passing.

## 2. Read first, in this order

1. `AGENTS.md`: rules for everyone, including AI assistants.
2. `clavix/modules/grounding.py`: **your contract**. Do not edit it.
3. `clavix/execution/dispatcher.py`, method `_dispatch_click`: how the core calls you.
4. `clavix/testing/contracts.py`, class `GroundingContract`: the tests you must pass.
5. `clavix/testing/fakes.py`, class `FakeGrounding`: a minimal reference implementation.

## 3. Ownership

| Action            | Paths |
|-------------------|-------|
| Create and own    | `clavix/modules/grounding_uia.py`, `tests/test_grounding_uia.py` |
| Never edit        | `clavix/modules/grounding.py`, any other file under `clavix/` and `configs/`. Ask P1 (see CONTRIBUTING.md) |

Person 1 switches `configs/default_config.yaml` to your class at integration (Phase 3).

## 4. The contract (verbatim from `clavix/modules/grounding.py`)

```python
@dataclass(frozen=True)
class UIElement:
    label: str                                  # accessible name
    control_type: str                           # e.g. "ButtonControl", "MenuItemControl"
    bounding_rect: tuple[int, int, int, int]    # (left, top, right, bottom), screen pixels
    native: Any = field(default=None, compare=False, repr=False)  # put the uiautomation Control here
    # .center -> (x, y)

class GroundingInterface(ABC):
    async def resolve_element(self, target_label: str) -> UIElement | None: ...
    async def invoke_element(self, element: UIElement) -> bool: ...
    async def click_coordinate(self, x: int, y: int) -> bool: ...
    def list_window_titles(self) -> list[str]: ...   # optional; default raises NotImplementedError
```

How the dispatcher uses you (`OS_GROUNDING` step):
1. Calls `resolve_element(label)`. If the result is not `None`, it calls `invoke_element(element)`.
   If that returns `True`, the step succeeds.
2. Otherwise, **if** the step has `fallback_coord` **and** `execution.enable_visual_fallback` is
   true, it calls `click_coordinate(x, y)`.
3. A `None` from `resolve_element` is **not retried** by the agent (`retryable=False`). **You own
   the waiting:** poll until the element appears or your timeout expires.

## 5. Dependencies

Already declared as the `grounding` extra in `pyproject.toml`:
`uiautomation>=2.0.18` (Windows only; the lockfile resolves 2.0.29).

```powershell
uv sync --extra grounding
```

Do not add other dependencies without P1's approval. Use `difflib` (stdlib) for fuzzy matching.

## 6. Constructor and configuration

```python
class UIAGrounding(GroundingInterface):
    def __init__(
        self,
        timeout_sec: float = 3.0,        # total wait in resolve_element
        poll_interval_sec: float = 0.2,  # delay between tree searches
        max_depth: int = 12,             # WalkControl depth per search root
        min_match_score: float = 0.8,    # accept threshold, see §7.2
    ) -> None: ...
```

- The constructor **must be cheap**. It must not touch UIA, start threads or import
  `uiautomation` at module import time; import it lazily inside methods or the worker. The
  runtime builds every module at startup, and the test suite imports all of them on Linux CI.
- At integration, the config entry will be:

```yaml
modules:
  grounding:
    class: clavix.modules.grounding_uia:UIAGrounding
    options: {timeout_sec: 3.0, poll_interval_sec: 0.2, max_depth: 12, min_match_score: 0.8}
```

## 7. Required behaviour

### 7.1 Threading (mandatory)

UIA is COM-based. All `uiautomation` calls must run on **one dedicated worker thread** owned
by the instance:
- Create a `concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="uia")`
  lazily on first use.
- Inside that thread, enter `uiautomation.UIAutomationInitializerInThread()` once and keep it
  entered for the thread's lifetime. This was verified to work with 2.0.29.
- Async methods hand work to that executor with `loop.run_in_executor(self._executor, fn)`.
  Never call UIA from the event-loop thread or from `asyncio.to_thread`.
- `UIElement.native` controls must only be used on that same worker thread.
- Provide `close()`, which shuts down the executor. Make the thread a daemon, or shut it down
  in `close()`. A probe with a non-daemon UIA thread was observed taking over 2 minutes to
  exit the process.

### 7.2 `resolve_element(target_label)`: deterministic matching

**Normalization:** `norm(s)` = casefold, remove `&` (access-key markers), cut at the first `\t`
(shortcut suffix), strip a trailing `...` or `…`, collapse whitespace, then strip.

**Score** of a candidate name `c` against target `t` (both normalized):

| Condition | Score |
|-----------|-------|
| `c == t` | 1.00 |
| `c` starts with `t` + space | 0.90 |
| `t` is a whole-word substring of `c` | 0.85 |
| otherwise | `difflib.SequenceMatcher(None, t, c).ratio()` |

Also score the control's `AutomationId` the same way and keep the higher of the two scores.
Accept a candidate only if its score is at least `min_match_score`.

**Candidate filter.** Skip a control if any of these hold:
- `Name` and `AutomationId` are both empty.
- `IsEnabled` is false.
- `IsOffscreen` is true.
- Its bounding rect has zero width or height.
- It is inside Clavix's own HUD: the top-level window whose `Name` is **`"Clavix HUD"`**. That window
  has a "Stop" button and status text, and must never be matched or clicked. Skip that top-level
  window entirely in every scope; it never takes focus, so it is never the foreground scope
  (see `docs/design/HUD_DESIGN.md` §6).

**Search scopes**, in order. Stop at the first scope that yields an accepted candidate:
1. The foreground top-level window: `auto.GetForegroundControl().GetTopLevelControl()`.
2. The taskbar: the root child with `ClassName == "Shell_TrayWnd"`.
3. All other root children (`auto.GetRootControl().GetChildren()`), each walked with
   `auto.WalkControl(root, maxDepth=self.max_depth)`.

**Tie-break**, applied in order until one candidate remains:
1. Higher score.
2. Control-type priority: `ButtonControl`, `MenuItemControl`, `HyperlinkControl`,
   `TabItemControl`, `ListItemControl`, `TreeItemControl`, `CheckBoxControl`,
   `RadioButtonControl`, `ComboBoxControl`, `SplitButtonControl`, `EditControl`, then
   everything else.
3. Smaller rect area.
4. Smaller `(top, left)`.

**Polling.** Repeat the full scoped search every `poll_interval_sec` until `timeout_sec` has
elapsed, then return `None`. Never raise for a miss. Log at DEBUG how many candidates were
scanned and the best score seen.

**Return:** `UIElement(label=control.Name or control.AutomationId,
control_type=control.ControlTypeName, bounding_rect=(r.left, r.top, r.right, r.bottom),
native=control)`, where `r = control.BoundingRectangle`.

### 7.3 `invoke_element(element)`

Run on the worker thread. Use `element.native`; if it is `None`, or `native.Exists(0, 0)` is
false, re-resolve by `element.label` first. Then try each pattern in order and return `True` on
the first that succeeds. Pattern lookups use `control.GetPattern(auto.PatternId.X)`, which
returns `None` when the pattern is unsupported. The base `Control` class has **no**
`GetInvokePattern()`; that method exists only on some subclasses, so don't use it.

1. `InvokePattern`: `.Invoke()`.
2. `ExpandCollapsePattern`: `.Expand()` (menus, combo boxes).
3. `TogglePattern`: `.Toggle()`. **Note:** the Win11 Start button has no `InvokePattern`,
   only `TogglePattern` (verified).
4. `SelectionItemPattern`: `.Select()`.
5. Last resort: `self.click_coordinate(*element.center)`.

Catch COM errors per pattern and move on to the next one. Return `False` only if every option
fails.

### 7.4 `click_coordinate(x, y)`

Keep the stub's behaviour: `import pyautogui`, set `pyautogui.FAILSAFE = True`, then call
`pyautogui.click(x, y)` off the event loop. Let `pyautogui.FailSafeException` propagate; the
dispatcher turns it into an abort.

### 7.5 `list_window_titles()`

Synchronous, run on the worker thread (block on the future). Return the `Name` of every root
child that is a `WindowControl` or `PaneControl`, has a non-empty name, and is not offscreen.
The state verifier uses this for `window_title_contains:` postconditions.

## 8. Verified reference facts (uiautomation 2.0.29, Windows 11)

| Fact | Value |
|------|-------|
| Taskbar | root child `PaneControl`, `ClassName "Shell_TrayWnd"`, `Name "Taskbar"` |
| Start button | `ButtonControl`, `Name "Start"`, `AutomationId "StartButton"`; supports Toggle, not Invoke |
| Taskbar walk, depth 8 | about 56 ms |
| `WalkControl` | `(control, includeTop=False, maxDepth=...)`, yields `(Control, depth)` |
| `Control.Click` | `(x=None, y=None, ratioX=0.5, ratioY=0.5, simulateMove=True, waitTime=0.5)` |
| `BoundingRectangle` | `Rect` with `.left .top .right .bottom` attributes; `.width()` and `.height()` are **methods** |
| Pattern methods | `InvokePattern.Invoke`, `ExpandCollapsePattern.Expand/Collapse`, `TogglePattern.Toggle`, `SelectionItemPattern.Select` |

If you need any other API, check it in the installed package before using it, e.g.
`uv run python -c "import uiautomation as a, inspect; print(inspect.signature(a.Control.X))"`.
Don't rely on memory.

## 9. Tests (`tests/test_grounding_uia.py`)

```python
import sys
import pytest
from clavix.testing.contracts import GroundingContract

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows UI Automation")

class TestUIAGroundingContract(GroundingContract):
    resolve_timeout_sec = 1.0
    known_label = "Start"

    @pytest.fixture
    def impl(self):
        from clavix.modules.grounding_uia import UIAGrounding
        g = UIAGrounding(timeout_sec=self.resolve_timeout_sec)
        yield g
        g.close()
```

Also add:
- **Unit tests (no UIA):** test the normalization and scoring rules as pure functions, so put
  them at module level (`_normalize`, `_score`). Cover every row of §7.2, accelerators
  (`"&File"`), shortcut suffixes (`"Save\tCtrl+S"`) and the tie-break order, using simple
  stand-in objects.
- **Desktop tests** marked `@pytest.mark.desktop`:
  1. Launch `notepad`, resolve `"File"`, invoke it, assert that a `MenuItemControl` named
     "New tab" or "New" becomes resolvable, press Esc, then kill the Notepad process you
     started (by PID).
  2. Resolve and invoke `"Start"`, assert the Start menu opened, then invoke it again to close it.

## 10. Definition of done

- [ ] `uv run pytest tests/test_grounding_uia.py` passes on Windows, including the `desktop`-marked tests.
- [ ] `uv run pytest -m "not desktop"` passes on Windows and imports cleanly on Linux.
- [ ] `uv run ruff check . && uv run ruff format --check . && uv run mypy` are clean.
- [ ] A miss returns `None` within `timeout_sec + 1 s`; a hit on "Start" takes under 500 ms.
- [ ] With your class configured, `uv run clavix-bench --planner reference --tiers 3` passes T3-01 and T3-02.
- [ ] No file outside §3 is modified.

## 11. Out of scope (Phase 2)

- Linux AT-SPI and macOS backends.
- Typing text or keyboard input. No action exists for it yet; P1 will add a `type_text` action in a later phase.
- Image or OCR based grounding.
- Changing the contract or the dispatcher.
