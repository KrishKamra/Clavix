# Security policy

Clavix turns speech into actions on a real desktop: it runs shell commands, opens URLs and clicks
interface elements. A flaw in planning, validation or dispatch can therefore do real damage, and we
take reports seriously.

## Supported versions

| Version | Supported |
|---------|:---------:|
| `main`  | ✅ |
| `0.1.x` | ✅ |
| older   | ❌ |

## Reporting a vulnerability

> [!IMPORTANT]
> Please **do not open a public issue** for a security problem.

Report it privately through **[GitHub private vulnerability reporting](https://github.com/KrishKamra/Clavix/security/advisories/new)**
(the repository's *Security* tab → *Report a vulnerability*). Include:

- what an attacker can make Clavix do, and from where (spoken command, crafted window title, config file…);
- the steps or the command that reproduces it, with the plan Clavix produced if you have it;
- your Clavix commit, Windows build and planner model.

We aim to acknowledge a report within **7 days** and to agree on a fix and disclosure date with you.
Clavix is an academic project maintained by students, so please allow for exam periods.

## What counts

| In scope | Out of scope |
|----------|--------------|
| A command that bypasses `execution.blocked_command_patterns` | Damage from commands a user deliberately spoke and confirmed |
| A plan that passes schema validation but runs something other than it states | Weaknesses in Ollama, Whisper or Windows themselves (report upstream) |
| The HUD taking focus, appearing in screenshots, or being matched by grounding | Running Clavix with `enable_visual_fallback` on an unattended machine |
| Text on screen (a window title, a web page) steering the planner or dispatcher | Social engineering of a person who is operating Clavix |

## Safety model

Clavix is designed to fail closed:

- **Local only.** Speech recognition and planning run on the device; with the default Ollama planner
  no audio, transcript or screen content leaves the machine.
- **Schema-validated plans.** The planner may only emit typed actions from a fixed vocabulary, and an
  invalid plan is rejected rather than repaired by guesswork.
- **Blocked commands.** `execution.blocked_command_patterns` refuses matching shell commands before
  they run.
- **Deterministic first.** Coordinate clicks are off by default and are the last resort when on;
  `pyautogui.FAILSAFE` is always enabled.
- **Abort at any time.** The HUD's Stop button or a double press of <kbd>Esc</kbd> aborts the running step.

> [!WARNING]
> `clavix-bench` and the desktop tests drive the real desktop: they open browser tabs and Notepad,
> write files and take screenshots. Run them on a machine you are actively using, never unattended.
