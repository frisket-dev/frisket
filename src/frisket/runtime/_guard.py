"""Stdlib process guardian. Never reads or writes the target's protocol streams.

This handles application crashes and ordinary process descendants. It is not
a containment wall against a hostile program that deliberately changes session
or kills its guardian; sandbox policy supplies the separate security boundary.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time


def main() -> int:
    if len(sys.argv) < 4:
        raise ValueError("invalid guardian configuration")
    parent_pid = int(sys.argv[1])
    grace = float(sys.argv[2])
    if parent_pid < 1 or not 0 < grace <= 10:
        raise ValueError("invalid guardian configuration")
    if sys.platform == "win32":
        from pathlib import Path
        import json
        import tempfile

        # Keep the existing argv interface for model setup/native checks. The
        # PS Job owner watches both this wrapper and its application owner, so
        # killing either cannot strand the native target or its descendants.
        with tempfile.TemporaryDirectory(prefix="frisket-owned-") as directory:
            root = Path(directory)
            config = root / "launch.json"
            config.write_text(
                json.dumps(
                    {
                        "command": sys.argv[3],
                        "args": sys.argv[4:],
                        "parentPid": os.getpid(),
                        "ownerPid": parent_pid,
                        "stop": str(root / "stop"),
                        "proof": str(root / "clean"),
                    }
                ),
                encoding="utf-8",
            )
            powershell = (
                Path(os.environ["SYSTEMROOT"])
                / "System32/WindowsPowerShell/v1.0/powershell.exe"
            )
            return subprocess.call(
                [
                    str(powershell),
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(Path(__file__).with_name("_guard_windows.ps1")),
                    "-Config",
                    str(config),
                ],
                # CREATE_NO_WINDOW cannot rely on console inheritance for the
                # protocol handles. Explicit redirection makes subprocess pass
                # inheritable duplicates of these streams to the Job guardian.
                stdin=sys.stdin if sys.stdin is not None else subprocess.DEVNULL,
                stdout=sys.stdout if sys.stdout is not None else subprocess.DEVNULL,
                stderr=sys.stderr if sys.stderr is not None else subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
    stopped = False

    def stop(_signum, _frame):
        nonlocal stopped
        stopped = True

    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signum, stop)
    # Parent may have exited between Popen and this interpreter's startup.
    # getppid observes actual parentage, unlike kill(pid,0), which can mistake
    # a reused PID for a still-running parent. This also works on macOS.
    if os.getppid() != parent_pid or stopped:
        return 0
    if sys.platform == "linux":
        # Reap orphaned grandchildren ourselves rather than depending on an
        # arbitrary container PID 1 to reap them before the group-exit proof.
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
            raise OSError(ctypes.get_errno(), "worker subreaper setup failed")
    child = subprocess.Popen(sys.argv[3:], start_new_session=True)

    def reap():
        child.poll()
        if sys.platform == "linux":
            while True:
                try:
                    pid, status = os.waitpid(-1, os.WNOHANG)
                except ChildProcessError:
                    return
                if pid == 0:
                    return
                if pid == child.pid:
                    child.returncode = os.waitstatus_to_exitcode(status)

    def send(signum):
        try:
            os.killpg(child.pid, signum)
        except ProcessLookupError:
            pass

    def group_exists():
        try:
            os.killpg(child.pid, 0)
            return True
        except ProcessLookupError:
            return False

    try:
        while child.poll() is None and not stopped and os.getppid() == parent_pid:
            time.sleep(0.025)
    finally:
        send(signal.SIGTERM)
        deadline = time.monotonic() + grace
        while group_exists() and time.monotonic() < deadline:
            reap()
            time.sleep(0.01)
        if group_exists():
            send(signal.SIGKILL)
        # Exit is the cleanup acknowledgement. If a kernel task cannot exit,
        # stay alive: the bounded parent supervisor will report unproved
        # teardown instead of mistaking a dead guardian for a dead target.
        while group_exists():
            reap()
            time.sleep(0.01)
        child.wait()
    return child.returncode if child.returncode >= 0 else 128 - child.returncode


if __name__ == "__main__":
    raise SystemExit(main())
