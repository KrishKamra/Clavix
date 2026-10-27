# Instructions for AI coding assistants

These rules apply to every AI assistant working in this repository (Claude Code, Copilot,
Cursor, Gemini and others) and to the person directing it.

## Read before writing code

1. `docs/ROADMAP.md`: find your member's row and the files you own.
2. Your spec, `docs/specs/P<N>_*.md`. It is the source of truth for your task.
3. The contract file your spec names (`clavix/modules/<module>.py`).
4. `clavix/testing/contracts.py` (the tests your code must pass) and
   `clavix/testing/fakes.py` (minimal working examples of each interface).

`docs/MASTER_SPEC.md` is background only. Where it differs from the code or the member
specs, the code and the member specs win.

## Hard rules

1. **Edit only the files your spec lists under "Ownership".** Never modify contracts, schemas,
   core modules, configs or other members' files. If you think one needs a change, stop and
   tell the human. Changing a contract follows the process in CONTRIBUTING.md.
2. **Don't invent APIs.** Before using any third-party function, confirm it exists in the
   installed version:
   `uv run python -c "import pkg, inspect; print(inspect.signature(pkg.Thing.method))"`.
   The specs list the facts that have already been verified.
3. **Don't add dependencies.** Use only the packages in your module's extra in
   `pyproject.toml`, plus the standard library.
4. **Keep constructors cheap and imports lazy.** No device, thread, model or COM initialization
   at import time or in `__init__`. CI installs **no extras**, so import your extra's packages
   (`faster_whisper`, `sounddevice`, `uiautomation`, `pyttsx3`, `pynput`) inside functions,
   never at module top level. Use `if TYPE_CHECKING:` for type-only imports.
5. **Return failures, don't raise them.** Follow the contract docstrings. For example,
   `resolve_element` returns `None` on a miss.
6. **Tests that touch the real desktop, audio or models must be marked** `@pytest.mark.desktop`
   or `@pytest.mark.slow`. Unmarked tests must pass on headless Linux.
7. **All checks must be clean before you call a task done:**
   ```powershell
   uv run ruff format . ; uv run ruff check . ; uv run mypy ; uv run pytest
   ```
8. Report honestly. If a test fails or you skipped a requirement, say so; don't weaken a test
   to make it pass.

## Project facts

- Windows 11 is the primary platform. The shell for `run_command` is `cmd.exe` on Windows.
- Python 3.12 managed by uv (`uv sync`, `uv run ...`). Never use `pip install`.
- The async agent loop runs on asyncio. Move blocking work off the event loop (the specs say how).
- Component wiring happens only in `clavix/runtime.py`, driven by `configs/default_config.yaml`
  (`modules.<name>.class`).
