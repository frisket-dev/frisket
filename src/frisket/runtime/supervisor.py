"""Transparent child lifetime supervision; tools retain their native protocols."""

from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess

from frisket.runtime.launch import PythonRuntime


def guarded_argv(argv: list[str], *, grace_seconds: float = 0.25) -> list[str]:
    """Keep a POSIX worker's cleanup alive when its application parent dies.

    The caller must start the guard in a new session. The target has another
    session, so recursively launched guards survive the outer target's death
    long enough to clean their own children. Windows sandbox callers already
    own kill-on-close Jobs and keep their existing launch path.
    """
    if os.name != "posix":
        return argv
    if not argv or not 0 < grace_seconds <= 10:
        raise ValueError("guardian requires a command and bounded grace period")
    return [
        str(PythonRuntime.current().executable),
        "-I",
        str(Path(__file__).with_name("_guard.py").resolve()),
        str(os.getpid()),
        str(grace_seconds),
        *argv,
    ]


def spawn_service(argv: list[str]) -> subprocess.Popen:
    return subprocess.Popen(
        guarded_argv(argv, grace_seconds=8),
        **({"start_new_session": True} if os.name == "posix" else {}),
    )


def guard_exit_proves_cleanup(returncode: int | None) -> bool:
    """A normal guard exit acknowledges cleanup; unknown/dead guards cannot.

    Default SIGTERM death is safe during startup: the guard installs its TERM
    handler before it can spawn a target. Later target signal exits are encoded
    as positive 128+signal values, never negative guardian signal statuses.
    """
    return returncode is not None and (returncode >= 0 or returncode == -signal.SIGTERM)


def stop_guard(process: subprocess.Popen, *, timeout: float = 1.0) -> None:
    """Let the guardian finish tree cleanup before considering a forced exit."""

    def verify_exit() -> None:
        if os.name == "posix" and not guard_exit_proves_cleanup(process.returncode):
            raise RuntimeError("worker guardian was killed before proving shutdown")

    if process.poll() is not None:
        verify_exit()
        return
    process.terminate()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=1)
        raise RuntimeError("worker guardian did not complete shutdown")
    verify_exit()


def stop_service(process: subprocess.Popen | None) -> None:
    if process is None:
        return
    stop_guard(process, timeout=10)
