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
        "worker=spawn_service([sys.executable,'-I','-c',"
        "\"import ctypes; k=ctypes.WinDLL('kernel32'); k.GetConsoleWindow.restype=ctypes.c_void_p; print(int(k.GetConsoleWindow() or 0))\"],"
        "stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE)\n"
        "output,error=worker.communicate(timeout=15)\n"
        "assert worker.returncode==0,error\n"
        "print(json.dumps({'target':int(kernel.GetConsoleWindow() or 0),'queue':int(output)}))\n"
    )
    # subprocess-boundary: kernel-observed console handles of installed-runtime children.
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            str(Path(supervisor.__file__).with_name("_guard.py")),
            str(os.getpid()),
            "2",
            sys.executable,
            "-I",
            str(target),
        ],
        capture_output=True,
        text=True,
        timeout=45,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"target": 0, "queue": 0}
