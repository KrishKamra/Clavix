## What and why

<!-- One or two sentences. Link the issue or spec section. -->

## How it was verified

- [ ] `uv run ruff format . ; uv run ruff check . ; uv run mypy ; uv run pytest` are clean
- [ ] Desktop or slow tests that touch this change were run on Windows (`uv run pytest -m desktop`)
- [ ] `clavix-bench --planner reference --cleanup` still passes (if execution, grounding or feedback changed)

## Checklist

- [ ] I only edited files my spec owns, or the contract change follows CONTRIBUTING.md
- [ ] New third-party calls were checked against the installed version
- [ ] Docs and the changelog are updated if behaviour changed
