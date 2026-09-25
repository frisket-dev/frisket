"""Native Windows desktop headless launch behavior, through the public guardian."""

import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.realtime


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows PowerShell")
def test_spawn_service_starts_with_filtered_environment():
    from frisket.runtime.supervisor import spawn_service, stop_service

    environment = {
        name: os.environ[name]
        for name in (
            "COMSPEC",
            "PATH",
            "PATHEXT",
            "SYSTEMROOT",
            "TEMP",
            "TMP",
            "WINDIR",
        )
        if name in os.environ
    }
    assert "PSMODULEPATH" not in environment
    process = spawn_service(
        [
            sys.executable,  # subprocess-boundary: native guardian with a minimal OS environment.
            "-I",
            "-c",
            "raise SystemExit(0)",
        ],  # subprocess-boundary: native guardian with a minimal OS environment.
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=environment,
    )
    assert process.wait(timeout=15) == 0
    stop_service(process)


def _windows_pid_alive(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, wintypes.LPDWORD]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and (
            code.value == STILL_ACTIVE
        )
    finally:
        kernel32.CloseHandle(handle)


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows console handles")
def test_guarded_target_and_queue_service_have_no_console(tmp_path):
    from frisket.runtime import supervisor

    source = Path(supervisor.__file__).resolve().parents[2]
    target = tmp_path / "headless target.py"
    target.write_text(
        "import ctypes,json,subprocess,sys\n"
        f"sys.path.insert(0,{str(source)!r})\n"
        "from frisket.runtime.supervisor import spawn_service\n"
        "kernel=ctypes.WinDLL('kernel32',use_last_error=True)\n"
        "kernel.GetConsoleWindow.restype=ctypes.c_void_p\n"
        "worker=spawn_service([sys.executable,'-I','-c',"  # subprocess-boundary: observe the queue child's native console handle.
        "\"import ctypes; k=ctypes.WinDLL('kernel32'); k.GetConsoleWindow.restype=ctypes.c_void_p; print(int(k.GetConsoleWindow() or 0))\"],"
        "stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE)\n"
        "output,error=worker.communicate(timeout=15)\n"
        "assert worker.returncode==0,error\n"
        "print(json.dumps({'target':int(kernel.GetConsoleWindow() or 0),'queue':int(output)}))\n"
    )
    result = subprocess.run(
        [
            sys.executable,  # subprocess-boundary: execute the actual Windows guardian.
            "-I",
            str(Path(supervisor.__file__).with_name("_guard.py")),
            str(os.getpid()),
            "2",
            sys.executable,  # subprocess-boundary: observe the guarded target's native console handle.
            "-I",
            str(target),
        ],
        capture_output=True,
        text=True,
        timeout=45,
        # Give the outer guardian a console: each target/service launch must
        # independently suppress its console rather than depend on this parent.
        creationflags=subprocess.CREATE_NEW_CONSOLE,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"target": 0, "queue": 0}


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows protocol handles")
def test_headless_guard_preserves_binary_protocol_and_target_exit(tmp_path):
    from frisket.runtime import supervisor

    target = tmp_path / "protocol target.py"
    target.write_text(
        "import sys\n"
        "data=sys.stdin.buffer.read()\n"
        "sys.stdout.buffer.write(data)\n"
        "sys.stderr.buffer.write(data[::-1])\n"
        "sys.exit(7)\n"
    )
    payload = bytes(range(256)) * 32
    result = subprocess.run(
        [
            sys.executable,  # subprocess-boundary: execute the actual Windows guardian.
            "-I",
            str(Path(supervisor.__file__).with_name("_guard.py")),
            str(os.getpid()),
            "2",
            sys.executable,  # subprocess-boundary: byte-transparent target streams and exit status.
            "-I",
            str(target),
        ],
        input=payload,
        capture_output=True,
        timeout=45,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    assert result.returncode == 7, result.stderr
    assert result.stdout == payload
    assert result.stderr == payload[::-1]


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows Job Objects")
def test_spawn_service_job_dies_with_application_parent(tmp_path):
    """The public service launcher must own descendants, not only its wrapper."""
    from frisket.runtime import supervisor

    source = Path(supervisor.__file__).resolve().parents[2]
    pids = tmp_path / "pids.json"
    target = tmp_path / "target.py"
    target.write_text(
        "import json,os,subprocess,sys,time\n"
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])\n"  # subprocess-boundary: descendant cleanup proof.
        "open(sys.argv[1],'w').write(json.dumps([os.getpid(),child.pid]))\n"
        "time.sleep(60)\n"
    )
    launcher = tmp_path / "launcher.py"
    launcher.write_text(
        "import os,subprocess,sys,time\nfrom pathlib import Path\n"
        f"sys.path.insert(0,{str(source)!r})\n"
        "from frisket.runtime.supervisor import spawn_service\n"
        "spawn_service([sys.executable,sys.argv[1],sys.argv[2]],"  # subprocess-boundary: actual service ownership.
        "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
        "while not Path(sys.argv[2]).exists(): time.sleep(.02)\n"
        "os._exit(0)\n"
    )
    parent = subprocess.Popen(
        [
            sys.executable,  # subprocess-boundary: native Windows Job proof.
            str(launcher),
            str(target),
            str(pids),
        ],  # subprocess-boundary: native Windows Job proof.
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    owned_pids: list[int] = []
    try:
        assert parent.wait(timeout=15) == 0
        owned_pids = json.loads(pids.read_text())
        deadline = time.monotonic() + 15
        while any(_windows_pid_alive(pid) for pid in owned_pids):
            if time.monotonic() >= deadline:
                pytest.fail("service Job survived its application parent")
            time.sleep(0.05)
    finally:
        # TerminateProcess is only emergency test cleanup after a failed Job proof.
        for pid in owned_pids:
            if _windows_pid_alive(pid):
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/F", "/T"],
                    capture_output=True,
                    check=False,
                )
        if parent.poll() is None:
            parent.kill()


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows Job Objects")
def test_stop_service_waits_for_job_tree_cleanup(tmp_path):
    from frisket.runtime.supervisor import spawn_service, stop_service

    pids = tmp_path / "stop-pids.json"
    target = tmp_path / "stop-target.py"
    target.write_text(
        "import json,os,subprocess,sys,time\n"
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])\n"  # subprocess-boundary: whole-tree teardown proof.
        "open(sys.argv[1],'w').write(json.dumps([os.getpid(),child.pid]))\n"
        "time.sleep(60)\n"
    )
    process = spawn_service(
        [
            sys.executable,  # subprocess-boundary: synchronous Windows Job teardown.
            str(target),
            str(pids),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    owned_pids: list[int] = []
    try:
        deadline = time.monotonic() + 10
        while not pids.exists():
            if time.monotonic() >= deadline:
                pytest.fail("guarded service did not start")
            time.sleep(0.02)
        owned_pids = json.loads(pids.read_text())
        stop_service(process)
        assert all(not _windows_pid_alive(pid) for pid in owned_pids)
        stop_service(process)  # Repeated shutdown is harmless.
    finally:
        for pid in owned_pids:
            if _windows_pid_alive(pid):
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/F", "/T"],
                    capture_output=True,
                    check=False,
                )
        if process.poll() is None:
            process.kill()
