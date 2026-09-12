"""Exercise the real interpreter/application boundary without model downloads."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import venv

import pytest

from frisket.runtime.launch import PythonRuntime, worker_argv


def test_private_interpreter_loads_separate_app_not_ambient_pythonpath(tmp_path):
    environment = tmp_path / "private Python"
    venv.EnvBuilder(with_pip=False).create(environment)
    executable = environment / (
        "Scripts/python.exe" if os.name == "nt" else "bin/python"
    )
    app = tmp_path / "Application Resources"
    package = app / "frisket"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    import frisket.runtime

    shutil.copytree(Path(frisket.runtime.__file__).parent, package / "runtime")
    hostile = tmp_path / "ambient"
    hostile.mkdir()
    (hostile / "frisket.py").write_text("raise RuntimeError('wrong app')")
    result = subprocess.run(
        worker_argv("runtime-info", runtime=PythonRuntime(executable, app)),
        env={**os.environ, "PYTHONPATH": str(hostile), "PATH": str(hostile)},
        cwd=hostile,
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    info = json.loads(result.stdout)
    assert Path(info["prefix"]) == environment
    assert Path(info["executable"]) == executable
    assert Path(info["app_code_root"]) == app


def test_worker_does_not_write_bytecode_into_its_app_tree(tmp_path):
    environment = tmp_path / "private Python"
    venv.EnvBuilder(with_pip=False).create(environment)
    executable = environment / (
        "Scripts/python.exe" if os.name == "nt" else "bin/python"
    )
    app = tmp_path / "Application Resources"
    package = app / "frisket"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    import frisket.runtime

    shutil.copytree(Path(frisket.runtime.__file__).parent, package / "runtime")
    plugin = package / "plugins"
    plugin.mkdir()
    (plugin / "subprocess_runner.py").write_text("def main():\n    return 0\n")
    cache = plugin / "__pycache__"
    cache.mkdir()
    stale_cache = cache / f"subprocess_runner.{sys.implementation.cache_tag}.pyc"
    stale_cache.write_bytes(b"signed-app-cache-must-not-change")
    before = {
        path.relative_to(app): path.read_bytes()
        for path in app.rglob("*")
        if path.is_file()
    }

    subprocess.run(
        worker_argv("plugin", runtime=PythonRuntime(executable, app)),
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )

    assert {
        path.relative_to(app): path.read_bytes()
        for path in app.rglob("*")
        if path.is_file()
    } == before


def test_missing_runtime_does_not_fall_back(tmp_path):
    with pytest.raises(ValueError, match="interpreter"):
        worker_argv(
            "runtime-info", runtime=PythonRuntime(tmp_path / "missing", tmp_path)
        )


def test_unknown_worker_is_rejected_in_parent_and_child():
    with pytest.raises(ValueError, match="worker"):
        worker_argv("os.system")
    command = worker_argv("runtime-info")
    command[3] = "os.system"
    result = subprocess.run(command, capture_output=True, timeout=10)
    assert result.returncode != 0
    assert b"unknown worker" in result.stderr


def test_desktop_bootstrap_refuses_invalid_stdin_without_writing_stdout():
    secret = "not-a-desktop-token"
    result = subprocess.run(
        worker_argv("desktop-server"),
        input=(f'{{"token":"{secret}"}}').encode(),
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 2
    assert result.stdout == b""
    assert secret.encode() not in result.stderr


def test_current_runtime_keeps_venv_executable():
    # subprocess-boundary: verifies interpreter identity or real child lifetime.
    assert worker_argv("runtime-info")[0] == os.path.abspath(sys.executable)
