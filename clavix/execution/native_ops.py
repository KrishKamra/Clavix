"""Concrete deterministic OS handlers: URLs, shell commands and screenshots.

Every handler returns an :class:`ExecutionResult` instead of raising, so the
dispatcher and verifier see failures as data. Handlers are synchronous; the
dispatcher runs them in a worker thread to keep the agent loop responsive.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Optional

from clavix.schemas.actions import ActionType, ExecutionResult

_DETERMINISTIC = ActionType.DETERMINISTIC


def launch_url(url: str) -> ExecutionResult:
    """Open ``url`` with the OS default protocol handler (non-blocking).

    Uses ``os.startfile`` on Windows, ``open`` on macOS and ``xdg-open`` elsewhere.
    """
    try:
        if sys.platform == "win32":
            os.startfile(url)  # type: ignore[attr-defined]  # noqa: S606
            return ExecutionResult(success=True, route=_DETERMINISTIC, details={"url": url})
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        proc = subprocess.Popen(  # noqa: S603
            [opener, url],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return ExecutionResult(success=True, route=_DETERMINISTIC, pid=proc.pid, details={"url": url})
    except OSError as exc:
        return ExecutionResult(
            success=False, route=_DETERMINISTIC, error_message=f"failed to open {url}: {exc}"
        )


def run_cli(cmd: str, timeout_sec: int = 10, cwd: Optional[str] = None) -> ExecutionResult:
    """Run a shell command to completion and capture its output.

    The command runs through the platform shell (``cmd.exe`` on Windows,
    ``/bin/sh`` on POSIX). On timeout the whole process tree is killed.

    Args:
        cmd: Shell command line.
        timeout_sec: Maximum wall-clock seconds to wait.
        cwd: Working directory for the command.
    """
    try:
        proc = subprocess.Popen(  # noqa: S602
            cmd,
            shell=True,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            start_new_session=sys.platform != "win32",
        )
    except OSError as exc:
        return ExecutionResult(success=False, route=_DETERMINISTIC, error_message=f"failed to start: {exc}")
    try:
        stdout, stderr = proc.communicate(timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        stdout, stderr = proc.communicate()
        return ExecutionResult(
            success=False,
            route=_DETERMINISTIC,
            pid=proc.pid,
            return_code=proc.returncode,
            stdout=stdout,
            stderr=stderr,
            error_message=f"command timed out after {timeout_sec}s",
        )
    ok = proc.returncode == 0
    return ExecutionResult(
        success=ok,
        route=_DETERMINISTIC,
        pid=proc.pid,
        return_code=proc.returncode,
        stdout=stdout,
        stderr=stderr,
        error_message=None if ok else f"exit code {proc.returncode}: {stderr.strip()[:500]}",
    )


def spawn_process(cmd: str, cwd: Optional[str] = None) -> ExecutionResult:
    """Start a shell command detached from Clavix and return immediately.

    Intended for GUI applications whose lifetime outlives the step. Success means
    the process started, not that it finished.
    """
    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    try:
        proc = subprocess.Popen(  # noqa: S602
            cmd,
            shell=True,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
            start_new_session=sys.platform != "win32",
        )
    except OSError as exc:
        return ExecutionResult(success=False, route=_DETERMINISTIC, error_message=f"failed to spawn: {exc}")
    return ExecutionResult(success=True, route=_DETERMINISTIC, pid=proc.pid, details={"detached": True})


def take_screenshot(
    filepath: str, target_region: Optional[tuple[int, int, int, int]] = None
) -> ExecutionResult:
    """Capture the screen to ``filepath`` (PNG/JPEG inferred from the extension).

    Args:
        filepath: Destination path; parent directories are created.
        target_region: Optional ``(left, top, width, height)`` region.
    """
    try:
        from PIL import ImageGrab

        bbox = None
        if target_region is not None:
            left, top, width, height = target_region
            bbox = (left, top, left + width, top + height)
        image = ImageGrab.grab(bbox=bbox)
        path = Path(filepath)
        path.parent.mkdir(parents=True, exist_ok=True)
        image.save(path)
    except Exception as exc:  # PIL raises OSError, ValueError and platform-specific errors
        return ExecutionResult(success=False, route=_DETERMINISTIC, error_message=f"screenshot failed: {exc}")
    return ExecutionResult(
        success=True,
        route=_DETERMINISTIC,
        artifact_path=str(path),
        details={"width": image.width, "height": image.height},
    )


def _kill_tree(proc: subprocess.Popen[str]) -> None:
    """Kill ``proc`` and its children (the shell may have spawned the real work)."""
    try:
        if sys.platform == "win32":
            subprocess.run(  # noqa: S603
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        else:
            os.killpg(proc.pid, signal.SIGKILL)  # type: ignore[attr-defined]
    except OSError:
        pass
    if proc.poll() is None:
        proc.kill()


class NativeOps:
    """Object facade over the native handlers so the dispatcher can be given fakes."""

    def launch_url(self, url: str) -> ExecutionResult:
        """See :func:`launch_url`."""
        return launch_url(url)

    def run_cli(self, cmd: str, timeout_sec: int = 10) -> ExecutionResult:
        """See :func:`run_cli`."""
        return run_cli(cmd, timeout_sec)

    def spawn_process(self, cmd: str) -> ExecutionResult:
        """See :func:`spawn_process`."""
        return spawn_process(cmd)

    def take_screenshot(
        self, filepath: str, target_region: Optional[tuple[int, int, int, int]] = None
    ) -> ExecutionResult:
        """See :func:`take_screenshot`."""
        return take_screenshot(filepath, target_region)
