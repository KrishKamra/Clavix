# Clavix HUD: design system and build spec

**Status:** decided and prototyped on Windows 11 26H2 (2026-11-01). **Builder:** Person 4
(milestone P4-M2). **Reference implementation:** [`hud_reference/index.html`](hud_reference/index.html);
open it with `?demo` in a browser to watch a full task.

Everything below marked *verified* was measured on the dev machine with the prototype. Don't
re-decide it; if something doesn't hold on your machine, tell P1.

---

## 1. Decisions

### 1.1 Surfaces

| Surface | Who it's for | Status |
|---|---|---|
| **Voice + glass HUD overlay** | Everyone using Clavix | **The product.** Built by P4 from this document. |
| **CLI** (`clavix`, `clavix-bench`) | Developers, benchmarks, CI | Kept as is: plain, scriptable, logs to stdout. |
| A windowed app or dashboard | — | **Rejected.** Clavix is voice-first: the user's eyes are on the app being driven, not on Clavix. A main window competes for focus, which an automation agent must never take. |

### 1.2 Technology: pywebview (WebView2) in its own process

| Option | Verdict |
|---|---|
| tkinter (the old spec) | No blur, no gradients, no anti-aliased shapes, no animation. Can't look premium. |
| Qt / PySide6 | About 100 MB of wheels; QML glass on Windows still needs the same DWM work. |
| Electron | Bundles a second Chromium (250–400 MB RAM) and a Node toolchain. |
| Tauri | Lean, but adds Rust, a build step and a second language to a Python project. |
| **pywebview + WebView2** | **Chosen.** WebView2 ships with Windows 11, so HTML/CSS/JS renders with no bundled browser and no build step. Real Acrylic comes from DWM. |

Measured cost (*verified*): cold start **1.2 s** (once, at launch, off the critical path),
**157 MB** private memory across 7 processes, **0.16 %** idle CPU. For scale: the Whisper model
alone is about 300 MB.

### 1.3 Visual direction: "semantic glass"

Gradients and glass carry **meaning**, not decoration. The HUD's colour *is* the pipeline
stage, using the same three hues as the paper's figures. A reader of the paper and a user of the
product see one visual language:

| Stage | Colour | Where it appears |
|---|---|---|
| **Hear** (ASR) | blue | core while listening; first ribbon segment |
| **Think** (LLM planning) | amber | core while planning; second segment |
| **Act** (execution) | teal | core while acting; third segment |

Signature elements, in order of importance:
1. **The aperture:** a conic-gradient ring around a glowing stage-coloured centre. It breathes while
   listening and rotates while working. It's the brand mark; keep it exact.
2. **The latency ribbon:** the paper's latency waterfall, live. Segments grow as each stage
   finishes, with values in the legend.
3. **Stage-tinted glass:** a soft radial of the stage colour in the top-left of real Acrylic.

### 1.4 Anti-slop rules

Don't:
- use purple-to-pink "AI" gradients, sparkle icons, emoji, robot or brain metaphors, or "✨ AI" copy;
- add a gradient that doesn't encode a stage;
- use web fonts (Inter, Geist, Poppins), drop shadows on text, bouncy or spring easing, or confetti;
- show the internal state names (`DISPATCHING`). Use the human labels in §4;
- use colour alone for status: every status has an icon or text as well.

Do:
- use system fonts: they render with ClearType, cost 0 KB and match Windows;
- keep tabular numerals for every number, and short, plain-English copy;
- default to quiet: the HUD is compact unless a task is running, and collapses itself afterwards.

---

## 2. Architecture

```text
agent process (asyncio)                          HUD process (python -m clavix.modules.hud)
───────────────────────                          ──────────────────────────────────────────
TTSFeedback.on_state_change ─┐                   stdin reader thread
TTSFeedback.speak ───────────┼─ SafetyHUD ──JSON lines──► window.evaluate_js("hud.apply(…)")
TTSFeedback.on_event ────────┘    (pipe)         pywebview main thread + WebView2
        ▲                                                     │ Stop button
        └──────── request_abort() ◄── {"type":"abort"} ◄── js_api.abort()
```

- **Separate process, always.** pywebview must own its process's main thread, and the agent's
  main thread runs asyncio. A crash or hang in the HUD can never take the agent down.
- `SafetyHUD` keeps the API from the P4 spec (`start`, `update_state`, `update_message`, `close`,
  `on_abort`) and adds `update_event(event)`. Its methods **only write a line to a pipe**: they never
  block, never raise, and drop messages if the HUD process has died (log a warning once).
- Start the HUD process on the first `on_state_change`, as the spec says. Kill it in `close()`.
- The HUD process exits when its stdin closes, so it never outlives the agent.

### 2.1 Protocol: one JSON object per line

Agent to HUD (`hud.apply(msg)` receives exactly these):

| `type` | Fields | Source |
|---|---|---|
| `state` | `state`: an `AgentState` value | `on_state_change` |
| `message` | `text` | `speak` |
| `transcript` | `text`, `asr_ms`, `confidence` | `on_event` |
| `plan` | `steps: [{id, action, verb, target}]`, `llm_ms` | `on_event` |
| `step` | `id`, `status`: `running` / `ok` / `retry` / `failed`, `attempt` | `on_event` |
| `timing` | `asr_ms`, `llm_ms`, `exec_ms`, `success` | `on_event` |

The last four are `FeedbackInterface.on_event` payloads (`clavix.modules.feedback.AgentEvent`),
forwarded unchanged. **Ignore unknown types and keys.**

HUD to agent: `{"type": "ready"}` once the page has loaded, and `{"type": "abort"}` when Stop is pressed.

---

## 3. Window recipe (*verified*, pywebview 6.2.1, WebView2 156, Windows 11 26H2)

Every line here fixes a real failure seen in the prototype. Don't drop any.

```python
window = webview.create_window(
    "Clavix HUD", url=str(HUD_WEB / "index.html"), js_api=api,
    width=420, height=56, x=cx - 210, y=16,     # top-centre of the primary screen, 16 px down
    frameless=True, on_top=True, focus=False, shadow=False, transparent=True,
    easy_drag=False, min_size=(80, 30),          # default min_size (200, 100) blocks the compact pill
)
webview.start(on_start, (window,))               # on_start runs on a worker thread
```

In `on_start`, in this order:

1. `window.events.loaded.wait()`. pywebview **activates transparent windows on navigation**,
   so style the window only after that.
2. Set the WinForms form's `BackColor` to black on the UI thread:
   `form.Invoke(Action(lambda: setattr(form, "BackColor", Color.Black)))` with
   `form = window.native`. Without it, the "transparent" form paints control-grey and the glass
   reads washed-out light grey. DWM renders black in an extended frame as glass.
3. Get the window handle: `hwnd = user32.FindWindowW(None, "Clavix HUD")`.
4. Extended style: `GWL_EXSTYLE |= WS_EX_NOACTIVATE (0x08000000) | WS_EX_TOOLWINDOW (0x80)`.
   This means no focus and no taskbar or Alt-Tab entry.
5. DWM, via `DwmSetWindowAttribute`:
   - `20` (immersive dark mode) = 1;
   - `33` (corner preference) = 2 (round, 8 px);
   - `34` (border colour) = `0xFFFFFFFE` (none);
   - `DwmExtendFrameIntoClientArea(hwnd, MARGINS(-1, -1, -1, -1))`;
   - `38` (system backdrop) = 3 (**Acrylic**).
6. `SetWindowDisplayAffinity(hwnd, 0x11)` (`WDA_EXCLUDEFROMCAPTURE`). The HUD stays visible
   to the user but is **absent from every screenshot**, so `take_screenshot` steps and the visual
   fallback never capture it. *Verified* with `PIL.ImageGrab`.
7. If `GetForegroundWindow() == hwnd`, give focus back to the previous foreground window.
   *Verified:* after this, the HUD never takes focus again.
8. Send `{"type": "ready"}`.

**Sizing.** The page reports its card size with a `ResizeObserver`, and Python calls
`SetWindowPos(hwnd, 0, x, y, round(w * dpr), round(h * dpr), SWP_NOZORDER | SWP_NOACTIVATE)`,
using `window.devicePixelRatio` from the page and keeping the top-centre anchor.
`window.resize()` double-scales at 150 % DPI. Card sizes: compact 420×56, expanded up to about 420×240 CSS px.

**Fallbacks** (log once at INFO, never raise):
- If `DwmSetWindowAttribute(38)` fails (Windows 10), keep the window and raise `--glass` to
  `rgba(16, 18, 24, 0.90)`. Readable, without blur.
- If pywebview or WebView2 is missing, run without a HUD; speech and the hotkey still work.

---

## 4. States

| `AgentState` | Title | Subtitle | Colour | Aperture motion | Layout |
|---|---|---|---|---|---|
| `IDLE` | Ready | Say a command | neutral grey | still | compact |
| `ACOUSTIC_INGESTION` | Listening | Go ahead, I'm listening | Hear | breathe (2.4 s) | compact |
| `PLANNING` | Thinking | Planning the steps | Think | rotate (1.6 s) | expanded; clock starts |
| `DISPATCHING` | Acting | Working on it | Act | rotate | expanded |
| `VERIFYING` | Checking | Confirming it worked | Check (violet) | rotate | expanded |
| `FEEDBACK` | Wrapping up | (keep the last one) | Act | still | expanded |
| `COMPLETED` | Done | spoken summary, without a repeated "Done." | OK green | still | expanded, then **compact after 4 s** |
| `FAILED` | Couldn't finish | error, 2 lines max | Fail red | still | expanded, then **compact after 8 s** |

- **Stop** (button and the <kbd>Esc</kbd> <kbd>Esc</kbd> hint) is shown only in non-final states.
- **Sections without data stay hidden:** no transcript, no ribbon, no steps.
- Step rows read `{verb} {target}` (e.g. "Click **File**"), with the target in mono. Row states:
  - pending: an empty ring;
  - running: a spinner in the stage colour;
  - retry: a spinner plus "retrying";
  - ok: a green ✓ disc;
  - failed: a red ✗ disc.

---

## 5. Tokens

### Colour (dark glass)
| Token | Value | Use |
|---|---|---|
| `--hear` | `#4A90E2` | ASR stage |
| `--think` | `#C4831C` | planning stage |
| `--act` | `#1FA884` | execution stage |
| `--check` | `#A784DB` | verification: **aperture only**, never beside `--hear` |
| `--ok` / `--fail` | `#3FBF78` / `#F0625A` | status, always with an icon |
| `--idle` | `#9AA3B2` | idle aperture |
| `--ink-1/2/3` | white at 96 / 82 / 68 % | primary, secondary and hint text: 10.0 / 6.9 / 4.8 : 1 on the lightest glass measured (over a white window) |
| `--glass` | `rgba(16, 18, 24, 0.50)` | tint over Acrylic |

The three stage hues pass the palette validator on a dark surface: lightness band, chroma, and
all-pairs colour-blind separation (worst ΔE 11.6). The ribbon also has 2 px gaps and a labelled
legend, so stages never depend on colour alone. In print, the paper uses the same hues one step
darker (`#1F5FA8`, `#C8740A`, `#0E8A6D`).

### Type
| Role | Font | Size and weight |
|---|---|---|
| State title | Segoe UI Variable Display | 14 px / 600 |
| Transcript | Segoe UI Variable Text | 15 px / 400, in curly quotes |
| Body and subtitle | Segoe UI Variable Text | 12 px / 400 |
| Numbers, keycaps, step targets | Cascadia Mono | 11–12 px / 500, tabular |

### Shape and motion
- **Radius:** 8 px. This matches DWM's rounded corners exactly; the CSS radius must equal the window's.
- **Edge:** a 1 px gradient hairline, lit from above. **Grain:** 5 % fractal noise, to stop banding.
- **Easing:** `cubic-bezier(0.2, 0.8, 0.2, 1)`. Colour fades take 400 ms, ribbon segments 500 ms, button presses 120 ms.
- **Reduced motion:** `prefers-reduced-motion` turns off every animation and transition.

---

## 6. Guardrails

1. **Never take focus.** Use `focus=False`, `WS_EX_NOACTIVATE`, the step-7 focus hand-back, and `SWP_NOACTIVATE` on every resize.
2. **Never appear in captures:** `WDA_EXCLUDEFROMCAPTURE`.
3. **Never block the agent.** Pipe writes are fire-and-forget, and the HUD process is disposable.
4. **Never confuse grounding.** The HUD is a real UIA window with a "Stop" button, and P3's
   `resolve_element` skips the top-level window named **"Clavix HUD"** (P3 spec §7.2). Don't rename it.
5. **Accessibility:**
   - the Stop button has an accessible name and a visible focus ring;
   - status text never relies on colour alone;
   - every text colour is at least 4.5:1 on the lightest glass measured (*verified* from screenshots over a white window);
   - `aria-live` stays off, because TTS already speaks every message.

---

## 7. Definition of done (replaces the HUD items in the P4 spec)

- [ ] `clavix/modules/hud_web/index.html` matches the reference: same tokens, states, protocol and `?demo`.
- [ ] Unit tests (unmarked, no GUI): `SafetyHUD` writes valid JSON lines, never blocks or raises
      when the HUD process is gone, and forwards `{"type": "abort"}` to `on_abort`.
- [ ] `desktop` smoke test: start the HUD, send each state and a full event sequence, assert it
      `ready`s, and close it within 2 s.
- [ ] Manual check, with `show_hud: true`, on `uv run clavix --config configs/local.yaml` and one spoken command:
  - [ ] the HUD looks like the reference's six states (Ready, Listening, Thinking, Acting, Done, Failed);
  - [ ] it never takes focus;
  - [ ] it is absent from `take_screenshot` output;
  - [ ] Stop and double-Esc both abort.
- [ ] Measured and noted in the PR: cold start ≤ 2 s, idle CPU ≤ 0.5 %, private memory ≤ 200 MB.
