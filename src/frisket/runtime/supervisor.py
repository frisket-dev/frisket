"""Transparent child lifetime supervision; tools retain their native protocols."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile

from frisket.runtime.launch import PythonRuntime, optional_runtime_root
from frisket.runtime._environment_use import LOCK_NAME


def guarded_argv(
    argv: list[str],
    *,
    grace_seconds: float = 0.25,
    runtime_root: Path | None = None,
    runtime_ready: bool = True,
    runtime_exclusive: bool = False,
) -> list[str]:
    """Keep a POSIX worker's cleanup alive when its application parent dies.

    The caller must start the guard in a new session. The target has another
    session, so recursively launched guards survive the outer target's death
    long enough to clean their own children. Windows sandbox callers already
    own kill-on-close Jobs and keep their existing launch path.
    """
    runtime_root = runtime_root or optional_runtime_root(argv)
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
        *(
            [
                "--runtime-root",
                str(runtime_root),
                "exclusive" if runtime_exclusive else "shared",
                "ready" if runtime_ready else "verify",
            ]
            if runtime_root is not None
            else []
        ),
        *argv,
    ]


def spawn_service(
    argv: list[str],
    *,
    stdin: int | None = None,
    stdout: int | None = None,
    stderr: int | None = None,
    env: dict[str, str] | None = None,
    runtime_root: Path | None = None,
    runtime_ready: bool = True,
    runtime_exclusive: bool = False,
) -> subprocess.Popen:
    """Spawn a guarded service with optional explicit standard streams."""
    options: dict[str, object] = {
        "stdin": stdin,
        "stdout": stdout,
        "stderr": stderr,
        "env": env,
    }
    if os.name == "posix":
        options["start_new_session"] = True
        argv = guarded_argv(
            argv,
            grace_seconds=8,
            runtime_root=runtime_root,
            runtime_ready=runtime_ready,
            runtime_exclusive=runtime_exclusive,
        )
    elif os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
        # Launch the existing kill-on-close Job guardian directly. Keeping its
        # stop/proof directory lets stop_service wait for tree cleanup rather
        # than merely observing an intermediate Python wrapper exit.
        argv, control = windows_guard_command(
            argv,
            env=env,
            runtime_root=runtime_root,
            runtime_ready=runtime_ready,
            runtime_exclusive=runtime_exclusive,
        )
        try:
            process = subprocess.Popen(argv, **options)
        except BaseException:
            shutil.rmtree(control[0], ignore_errors=True)
            raise
        process._frisket_guard_control = control  # type: ignore[attr-defined]
        return process
    return subprocess.Popen(argv, **options)


def guard_exit_proves_cleanup(returncode: int | None) -> bool:
    """A normal guard exit acknowledges cleanup; unknown/dead guards cannot.

    Default SIGTERM death is safe during startup: the guard installs its TERM
    handler before it can spawn a target. Later target signal exits are encoded
    as positive 128+signal values, never negative guardian signal statuses.
    """
    return returncode is not None and (returncode >= 0 or returncode == -signal.SIGTERM)


def windows_cleanup_proved(proof: Path) -> bool:
    """Accept only the complete guardian acknowledgement, with a bounded read."""
    try:
        with proof.open("rb") as stream:
            return stream.read(6) == b"clean"
    except OSError:
        return False


def stop_guard(process: subprocess.Popen, *, timeout: float = 1.0) -> None:
    """Let the guardian finish tree cleanup before considering a forced exit."""

    def verify_exit() -> None:
        if os.name == "posix" and not guard_exit_proves_cleanup(process.returncode):
            raise RuntimeError("worker guardian was killed before proving shutdown")

    control = getattr(process, "_frisket_guard_control", None)
    if os.name == "nt" and control is not None:
        directory, stop, proof = control
        try:
            if process.poll() is None:
                stop.touch()
            process.wait(timeout=timeout)
            if not windows_cleanup_proved(proof):
                raise RuntimeError(
                    "Windows service guardian exited without cleanup proof"
                )
            process._frisket_guard_control = None  # type: ignore[attr-defined]
            shutil.rmtree(directory, ignore_errors=True)
            return
        except subprocess.TimeoutExpired:
            process.terminate()
            process.wait(timeout=1)
            raise RuntimeError("Windows service guardian did not complete shutdown")

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


def windows_guard_command(
    argv: list[str],
    *,
    env: dict[str, str] | None = None,
    runtime_root: Path | None = None,
    runtime_ready: bool = True,
    runtime_exclusive: bool = False,
    memory_limit_bytes: int | None = None,
    cpu_limit_seconds: int | None = None,
) -> tuple[list[str], tuple[Path, Path, Path]]:
    """Prepare the existing Job guardian for synchronous or async owners."""
    if memory_limit_bytes is not None and memory_limit_bytes <= 0:
        raise ValueError("Windows memory limit must be positive")
    if cpu_limit_seconds is not None and cpu_limit_seconds <= 0:
        raise ValueError("Windows CPU limit must be positive")
    control = Path(tempfile.mkdtemp(prefix="frisket-owned-"))
    config = control / "launch.json"
    stop = control / "stop"
    proof = control / "clean"
    config.write_text(
        json.dumps(
            {
                "command": argv[0],
                "args": argv[1:],
                "parentPid": os.getpid(),
                "ownerPid": os.getpid(),
                "stop": str(stop),
                "proof": str(proof),
                "memoryLimitBytes": memory_limit_bytes,
                "cpuLimitSeconds": cpu_limit_seconds,
                **(
                    {
                        "runtimeLock": str(root / LOCK_NAME),
                        "runtimeReady": str(root / ".ready") if runtime_ready else "",
                        "runtimeExclusive": runtime_exclusive,
                    }
                    if (root := runtime_root or optional_runtime_root(argv)) is not None
                    else {}
                ),
            }
        ),
        encoding="utf-8",
    )
    selected_env = env if env is not None else os.environ
    system_root = selected_env.get("SYSTEMROOT") or selected_env.get("SystemRoot")
    if not system_root:
        shutil.rmtree(control, ignore_errors=True)
        raise RuntimeError("Windows service guardian requires SYSTEMROOT")
    powershell = Path(system_root) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    argv = [
        str(powershell),
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(Path(__file__).with_name("_guard_windows.ps1").resolve()),
        "-Config",
        str(config),
    ]
    return argv, (control, stop, proof)
