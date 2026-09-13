"""Native Windows desktop headless launch behavior, through the public guardian."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


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
