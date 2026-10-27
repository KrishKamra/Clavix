"""Post-execution state assertion engine.

Each :class:`ActionStep` may declare ``expected_postcondition`` as
``"<kind>:<argument>"`` (see :class:`PostconditionKind`). Live-state predicates
(window titles, processes, files) are polled until ``timeout_sec`` because GUI
effects lag the dispatch; result-only predicates (exit code, stdout) are
checked once.
"""

from __future__ import annotations

import asyncio
import csv
import io
import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

from clavix.schemas.actions import (
    ActionStep,
    ExecutionResult,
    PostconditionKind,
    parse_postcondition,
)
from clavix.schemas.telemetry import VerificationResult, VerificationStatus

TitlesProvider = Callable[[], list[str]]
ProcessesProvider = Callable[[], list[str]]

_LIVE_KINDS = frozenset(
    {
        PostconditionKind.WINDOW_TITLE_CONTAINS,
        PostconditionKind.PROCESS_RUNNING,
        PostconditionKind.FILE_EXISTS,
    }
)


def default_window_titles() -> list[str]:
    """List visible top-level window titles.

    Raises:
        NotImplementedError: Off Windows, until the AT-SPI grounding module provides this.
    """
    if sys.platform != "win32":
        raise NotImplementedError("window enumeration is only implemented on Windows")
    import pygetwindow  # type: ignore[import-untyped]  # installed with pyautogui on Windows

    return [t for t in pygetwindow.getAllTitles() if t]


def default_process_names() -> list[str]:
    """List running process image names (lower-cased)."""
    if sys.platform == "win32":
        out = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            errors="replace",
            check=True,
        ).stdout
        return [row[0].lower() for row in csv.reader(io.StringIO(out)) if row]
    out = subprocess.run(["ps", "-A", "-o", "comm="], capture_output=True, text=True, check=True).stdout
    return [Path(line.strip()).name.lower() for line in out.splitlines() if line.strip()]


class StateVerifier:
    """Asserts step postconditions against execution results and live OS state."""

    def __init__(
        self,
        timeout_sec: float = 5.0,
        poll_interval_sec: float = 0.25,
        window_titles: TitlesProvider = default_window_titles,
        process_names: ProcessesProvider = default_process_names,
    ) -> None:
        self._timeout_sec = timeout_sec
        self._poll_interval_sec = poll_interval_sec
        self._window_titles = window_titles
        self._process_names = process_names

    async def verify(self, step: ActionStep, result: ExecutionResult) -> VerificationResult:
        """Check ``step``'s postcondition given its execution ``result``.

        A failed execution always fails verification. A step without a
        postcondition is ``SKIPPED`` (accepted) when execution succeeded.
        """
        expr = step.expected_postcondition
        if not result.success:
            return VerificationResult(
                status=VerificationStatus.FAILED,
                postcondition=expr,
                detail=f"execution failed: {result.error_message or 'unknown error'}",
            )
        if expr is None:
            return VerificationResult(status=VerificationStatus.SKIPPED, detail="no postcondition")

        kind, arg = parse_postcondition(expr)
        deadline = time.monotonic() + (self._timeout_sec if kind in _LIVE_KINDS else 0.0)
        while True:
            try:
                ok, detail = await asyncio.to_thread(self._check, kind, arg, result)
            except NotImplementedError as exc:
                return VerificationResult(
                    status=VerificationStatus.UNSUPPORTED, postcondition=expr, detail=str(exc)
                )
            if ok:
                return VerificationResult(status=VerificationStatus.PASSED, postcondition=expr, detail=detail)
            if time.monotonic() >= deadline:
                return VerificationResult(status=VerificationStatus.FAILED, postcondition=expr, detail=detail)
            await asyncio.sleep(self._poll_interval_sec)

    def _check(self, kind: PostconditionKind, arg: str, result: ExecutionResult) -> tuple[bool, str]:
        match kind:
            case PostconditionKind.WINDOW_TITLE_CONTAINS:
                needle = arg.lower()
                titles = self._window_titles()
                hit = next((t for t in titles if needle in t.lower()), None)
                return (True, f"window {hit!r}") if hit else (False, f"no window title contains {arg!r}")
            case PostconditionKind.PROCESS_RUNNING:
                name = arg.lower()
                names = set(self._process_names())
                ok = name in names or f"{name}.exe" in names
                return ok, f"process {arg!r} {'running' if ok else 'not running'}"
            case PostconditionKind.FILE_EXISTS:
                ok = Path(os.path.expandvars(arg)).expanduser().exists()
                return ok, f"{arg} {'exists' if ok else 'does not exist'}"
            case PostconditionKind.EXIT_CODE:
                expected = int(arg)
                ok = result.return_code == expected
                return ok, f"exit code {result.return_code} (expected {expected})"
            case PostconditionKind.STDOUT_CONTAINS:
                ok = arg.lower() in result.stdout.lower()
                return ok, f"stdout {'contains' if ok else 'does not contain'} {arg!r}"
        raise AssertionError(f"unhandled postcondition kind {kind}")  # pragma: no cover
