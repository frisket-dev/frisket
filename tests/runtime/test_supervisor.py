"""Real POSIX process ownership, including nested workers after parent death."""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from frisket.runtime.supervisor import guarded_argv, stop_guard


# realtime: assert kernel-observed process exit; a virtual clock cannot reap OS children.
pytestmark = [
    pytest.mark.realtime,
    pytest.mark.skipif(os.name != "posix", reason="POSIX process guardian"),
]


def _wait(predicate, seconds=8):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.025)
    assert predicate()


def _live(pid):
    # Zombies cannot run or hold files; the OS's orphan reaper owns them.
    stat = Path(f"/proc/{pid}/stat")
    if stat.exists() and stat.read_text().split(") ", 1)[1].startswith("Z"):
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def test_normal_exit_cleans_descendant_without_changing_stdout(tmp_path):
    child = tmp_path / "child.py"
    child.write_text(
        "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(60)"
    )
    parent = tmp_path / "parent.py"
    parent.write_text(
        # subprocess-boundary: verifies interpreter identity or real child lifetime.
        "import subprocess,sys; p=subprocess.Popen([sys.executable,sys.argv[1]]); print(p.pid,flush=True)"
    )
    process = subprocess.Popen(
        # subprocess-boundary: verifies interpreter identity or real child lifetime.
        guarded_argv([sys.executable, str(parent), str(child)]),
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    descendant = int(process.stdout.readline())
    try:
        assert process.wait(timeout=8) == 0
        _wait(lambda: not _live(descendant))
    finally:
        if _live(descendant):
            os.kill(descendant, signal.SIGKILL)
        process.kill() if process.poll() is None else None


def test_parent_death_cleans_nested_separate_sessions(tmp_path):
    from frisket.runtime import supervisor

    # The controller exits abruptly after launching a worker, which launches
    # another managed worker. No application cleanup handler gets to run.
    info = tmp_path / "pids.json"
    leaf = tmp_path / "leaf.py"
    leaf.write_text(
        "import os,sys,time,signal; from pathlib import Path; signal.signal(signal.SIGTERM,signal.SIG_IGN); Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(60)"
    )
    leaf_pid = tmp_path / "leaf.pid"
    outer = tmp_path / "outer.py"
    root = Path(supervisor.__file__).resolve().parents[2]
    outer.write_text(
        "import os,sys,subprocess,time,json\nfrom pathlib import Path\n"
        f"sys.path.insert(0,{str(root)!r})\n"
        "from frisket.runtime.supervisor import guarded_argv\n"
        # subprocess-boundary: verifies interpreter identity or real child lifetime.
        "p=subprocess.Popen(guarded_argv([sys.executable,sys.argv[1],sys.argv[2]]),start_new_session=True)\n"
        "while not Path(sys.argv[2]).exists(): time.sleep(.02)\n"
        "Path(sys.argv[3]).write_text(json.dumps([os.getpid(),p.pid,int(Path(sys.argv[2]).read_text())]))\n"
        "time.sleep(60)\n"
    )
    controller = tmp_path / "controller.py"
    controller.write_text(
        "import os,sys,subprocess,time\nfrom pathlib import Path\n"
        f"sys.path.insert(0,{str(root)!r})\n"
        "from frisket.runtime.supervisor import guarded_argv\n"
        # subprocess-boundary: verifies interpreter identity or real child lifetime.
        "p=subprocess.Popen(guarded_argv([sys.executable,*sys.argv[1:]]),start_new_session=True)\n"
        "while not Path(sys.argv[-1]).exists(): time.sleep(.02)\n"
        "print(p.pid,flush=True)\nos._exit(0)\n"
    )
    process = subprocess.Popen(
        [
            # subprocess-boundary: verifies interpreter identity or real child lifetime.
            sys.executable,
            str(controller),
            str(outer),
            str(leaf),
            str(leaf_pid),
            str(info),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    pids = []
    try:
        guardian_pid = int(process.stdout.readline())
        assert process.wait(timeout=8) == 0
        pids = [guardian_pid, *json.loads(info.read_text())]
        _wait(lambda: all(not _live(pid) for pid in pids))
    finally:
        for pid in pids:
            if _live(pid):
                os.kill(pid, signal.SIGKILL)
        process.kill() if process.poll() is None else None


def test_stop_guard_waits_for_term_ignoring_target(tmp_path):
    target = tmp_path / "target.py"
    target.write_text(
        "import os,signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
        "print(os.getpid(),flush=True); time.sleep(60)"
    )
    process = subprocess.Popen(
        # subprocess-boundary: verifies interpreter identity or real child lifetime.
        guarded_argv([sys.executable, str(target)]),
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    pid = int(process.stdout.readline())
    try:
        stop_guard(process)
        assert not _live(pid)
        assert process.poll() is not None
    finally:
        if _live(pid):
            os.kill(pid, signal.SIGKILL)
        if process.poll() is None:
            process.kill()
