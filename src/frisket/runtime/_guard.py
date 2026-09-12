"""Stdlib POSIX guardian. Never reads or writes the target's protocol streams.

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
    if not 0 < grace <= 10 or len(sys.argv) < 4:
        raise ValueError("invalid guardian configuration")
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
