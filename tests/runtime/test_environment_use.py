"""Runtime repair fences real guarded users, including application crashes."""

import asyncio
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest
from filelock import FileLock, Timeout

from frisket.runtime import model_install, supervisor
from frisket.runtime._environment_use import LOCK_NAME

pytestmark = [
    pytest.mark.realtime,
    pytest.mark.skipif(os.name != "posix", reason="POSIX guardian usage lease"),
]


def _ready(root):
    python = model_install._runtime_python(root)
    python.parent.mkdir(parents=True)
    # subprocess-boundary: guarded children need an executable optional-runtime fixture.
    python.symlink_to(sys.executable)
    (root / ".ready").touch()


def _start(root):
    return subprocess.Popen(
        supervisor.guarded_argv(
            [
                sys.executable,  # subprocess-boundary: real children hold cross-process runtime leases.
                "-c",
                "import os,time; print(os.getpid(),flush=True); time.sleep(60)",
            ],
            runtime_root=root,
        ),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )


def test_repair_refuses_shared_running_users_then_succeeds(tmp_path, monkeypatch):
    _ready(tmp_path)
    monkeypatch.setenv("FRISKET_MODEL_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(model_install, "_probe_install", lambda: False)
    commands = []
    monkeypatch.setattr(model_install, "_uv_command", lambda: ["uv"])
    monkeypatch.setattr(
        model_install, "_run_uv", lambda argv, **_: commands.append(argv)
    )
    users = [_start(tmp_path), _start(tmp_path)]
    try:
        assert all(int(user.stdout.readline()) > 0 for user in users)
        for _ in range(2):
            with pytest.raises(RuntimeError, match="environment is in use"):
                model_install.install_docling(
                    should_cancel=lambda: False, progress=lambda _: None
                )
            assert (tmp_path / ".ready").is_file()
            assert model_install.runtime_python().exists()
            assert commands == []
            supervisor.stop_guard(users.pop())
        probes = iter([False, True])
        monkeypatch.setattr(model_install, "_probe_install", lambda: next(probes))
        model_install.install_docling(
            should_cancel=lambda: False, progress=lambda _: None
        )
        assert len(commands) == 2
    finally:
        for user in users:
            supervisor.stop_guard(user)


def test_waiting_guard_rechecks_readiness_after_failed_repair(tmp_path):
    _ready(tmp_path)
    writer = FileLock(tmp_path / LOCK_NAME)
    writer.acquire()
    user = _start(tmp_path)
    try:
        (tmp_path / ".ready").unlink()
        writer.release()
        output, error = user.communicate(timeout=8)
        assert user.returncode != 0
        assert output == ""
        assert "environment is not ready" in error
    finally:
        writer.release()
        supervisor.stop_guard(user)


def test_guard_keeps_usage_lease_after_application_parent_death(tmp_path):
    _ready(tmp_path)
    source = Path(supervisor.__file__).resolve().parents[2]
    controller = tmp_path / "controller.py"
    controller.write_text(
        "import os,subprocess,sys\nfrom pathlib import Path\n"
        f"sys.path.insert(0,{str(source)!r})\n"
        "from frisket.runtime.supervisor import guarded_argv\n"
        "target='import os,signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); print(os.getpid(),flush=True); time.sleep(60)'\n"
        # subprocess-boundary: the guardian must retain its lease after its parent dies.
        "child=subprocess.Popen(guarded_argv([sys.executable,'-c',target],runtime_root=Path(sys.argv[1]),grace_seconds=1),stdout=subprocess.PIPE,text=True,start_new_session=True)\n"
        "print(child.stdout.readline().strip(),flush=True)\nos._exit(0)\n"
    )
    parent = subprocess.Popen(
        [sys.executable, str(controller), str(tmp_path)],  # subprocess-boundary: kill the actual application parent.
        stdout=subprocess.PIPE,
        text=True,
    )
    pid = int(parent.stdout.readline())
    assert parent.wait(timeout=5) == 0
    try:
        with pytest.raises(Timeout):
            FileLock(tmp_path / LOCK_NAME, timeout=0).acquire()
        with FileLock(tmp_path / LOCK_NAME, timeout=8):
            # The guardian cannot release until its whole target group is gone.
            stat = Path(f"/proc/{pid}/stat")
            assert not stat.exists() or stat.read_text().split(") ", 1)[1].startswith(
                "Z"
            )
    finally:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_sandbox_resource_wrapper_preserves_optional_runtime_lease(
    tmp_path, monkeypatch
):
    from frisket.engine.sandbox import shim

    _ready(tmp_path)
    monkeypatch.setenv("FRISKET_MODEL_RUNTIME_DIR", str(tmp_path))

    async def run():
        proc, controller, cancelled = await shim._spawn_owned_process(
            [
                str(model_install.runtime_python()),
                "-c",
                "import os,time; print(os.getpid(),flush=True); time.sleep(60)",
            ],
            scratch=tmp_path,
            env={},
            stdin=subprocess.DEVNULL,
            policy=shim.SandboxPolicy(cpu_seconds=10, memory_mb=1024),
        )
        try:
            assert not cancelled
            assert int(await asyncio.wait_for(proc.stdout.readline(), timeout=8)) > 0
            with pytest.raises(Timeout):
                FileLock(tmp_path / LOCK_NAME, timeout=0).acquire()
        finally:
            await shim._teardown_cancelled_spawn(proc, controller)
            controller.close()
        with FileLock(tmp_path / LOCK_NAME, timeout=0):
            pass

    asyncio.run(run())


@pytest.mark.parametrize("operation", ["verify", "install"])
def test_setup_children_retain_lease_after_owner_crash(tmp_path, operation):
    _ready(tmp_path)
    source = Path(supervisor.__file__).resolve().parents[2]
    pid_file = tmp_path / "native.pid"
    controller = tmp_path / "setup.py"
    target = (
        "import os,signal,time; from pathlib import Path; "
        "signal.signal(signal.SIGTERM,lambda *_: (time.sleep(.75),exit(0))); "
        f"Path({str(pid_file)!r}).write_text(str(os.getpid())); time.sleep(60)"
    )
    controller.write_text(
        "import os,sys\n"
        f"sys.path.insert(0,{str(source)!r})\n"
        "from frisket.runtime import model_install\n"
        f"os.environ['FRISKET_MODEL_RUNTIME_DIR']={str(tmp_path)!r}\n"
        f"target={target!r}\n"
        + (
            "model_install._probe_python(model_install.runtime_python(),target)\n"
            if operation == "verify"
            # subprocess-boundary: a real setup child must outlive its owner while retaining the lease.
            else "model_install._run_uv([sys.executable,'-c',target],should_cancel=lambda:False,progress=lambda _:None,runtime_root=model_install.runtime_dir())\n"
        )
    )
    # subprocess-boundary: terminate the setup owner to verify cross-process lease retention.
    parent = subprocess.Popen([sys.executable, str(controller)])
    try:
        deadline = time.monotonic() + 8
        while not pid_file.exists() and time.monotonic() < deadline:
            assert parent.poll() is None
            time.sleep(0.02)
        assert pid_file.exists()
        parent.terminate()
        parent.wait(timeout=5)
        with pytest.raises(Timeout):
            FileLock(tmp_path / LOCK_NAME, timeout=0).acquire()
        with FileLock(tmp_path / LOCK_NAME, timeout=10):
            pass
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=5)
