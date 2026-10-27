# Contributing to Clavix

## Setup

```powershell
uv sync                         # Python 3.12 (.python-version), runtime + dev tools
uv sync --extra acoustic        # Person 2 also installs their module's extra
uv sync --extra grounding       # Person 3
uv sync --extra feedback        # Person 4
```

`uv sync` without `--extra` removes extras installed earlier. Pass your extra every time,
or use `uv sync --all-extras`.

## Before every push

```powershell
uv run ruff format .
uv run ruff check .
uv run mypy
uv run pytest -m "not slow and not desktop"   # what CI runs
uv run pytest                                  # everything, on your Windows machine
```

## Test markers

| Marker    | Use for                                                       | Runs in CI |
|-----------|---------------------------------------------------------------|------------|
| (none)    | Pure logic, fakes, temp files, harmless shell commands such as `echo` | yes |
| `slow`    | Tests that need downloaded models or audio hardware           | no         |
| `desktop` | Tests that open windows, move the mouse or play audio         | no         |

Unmarked tests must not touch the real desktop, the network or the microphone.

## Ownership

See [docs/ROADMAP.md](docs/ROADMAP.md). Edit only the files you own. If you need a change
elsewhere, open an issue for the owner, or a PR that the owner reviews.

## Branches, commits and PRs

- Branch names: `p<N>/<short-topic>`, for example `p3/uia-resolve`.
- Keep commits small, with an imperative subject line ("Add UIA tie-break ordering").
- PR description: what changed, how you tested it, and which spec checklist items it closes.
- PR checklist:
  - [ ] All five commands in "Before every push" pass.
  - [ ] Your contract suite (`clavix.testing.contracts`) passes.
  - [ ] No files outside your ownership changed.
  - [ ] Every new dependency is in your extra in `pyproject.toml` and approved by P1.

## Changing a contract

The contract files are `clavix/modules/acoustic.py`, `grounding.py` and `feedback.py`, the
schemas in `clavix/schemas/`, and `clavix/testing/contracts.py`. They are frozen during
Phase 2, because every member builds against them.

1. Open an issue titled `Contract: <module>: <change>`. Describe the problem, the proposed
   signature and who is affected.
2. P1 decides. Additive changes, such as a new optional method with a default, are preferred
   over breaking ones.
3. P1 makes the change, the contract test update and the fakes update in one PR, then
   notifies everyone affected.

## Code style

- Strict typing (`mypy --strict`). No `Any` unless it crosses an untyped library boundary.
- Google-style docstrings on every public class and function.
- Keep constructors cheap. Open devices, threads and models lazily, on first use.
- Use `logging.getLogger(__name__)`, not `print`. The only exception is `ConsoleFeedback`.
- Match the surrounding code: comment density, naming and error handling (return
  `ExecutionResult` objects; don't raise for expected failures).
