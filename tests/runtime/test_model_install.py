from __future__ import annotations

import sys
import subprocess
from types import SimpleNamespace

import pytest
from filelock import FileLock

from frisket.runtime import model_install


def test_runtime_paths_follow_xdg_cache(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("FRISKET_MODEL_RUNTIME_DIR", raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.setattr(model_install.sys, "platform", "linux")

    assert model_install.runtime_dir() == tmp_path / "frisket" / "model-server"
    assert model_install.runtime_python() == (
        tmp_path / "frisket" / "model-server" / "venv" / "bin" / "python"
    )


def test_uv_command_uses_packaged_binary_when_path_has_none(monkeypatch) -> None:
    monkeypatch.setattr(model_install.shutil, "which", lambda _name: None)
    monkeypatch.setitem(
        sys.modules, "uv", SimpleNamespace(find_uv_bin=lambda: "/package/bin/uv")
    )

    assert model_install._uv_command() == ["/package/bin/uv"]


def test_model_child_environment_is_allowlisted() -> None:
    child = model_install.model_child_environment(
        {
            "PATH": "/tools",
            "UV_CACHE_DIR": "/cache",
            "UV_OVERRIDE": "/override.txt",
            "UV_PUBLISH_TOKEN": "publish-secret",
            "HTTPS_PROXY": "http://proxy.test",
            "SSL_CERT_FILE": "/ca.pem",
            "HF_HUB_OFFLINE": "1",
            "PSMODULEPATH": "C:/Windows/System32/WindowsPowerShell/v1.0/Modules",
            "PSModulePath": "/mixed-case/module/path",
            "PYTHONPATH": "/untrusted",
            "OPENAI_API_KEY": "provider-secret",
            "FRISKET_LOCAL_MODELS_TOKEN": "managed-secret",
            "DATABASE_URL": "app-secret",
        },
        installer=True,
    )

    assert child == {
        "PATH": "/tools",
        "UV_CACHE_DIR": "/cache",
        "UV_OVERRIDE": "/override.txt",
        "HTTPS_PROXY": "http://proxy.test",
        "SSL_CERT_FILE": "/ca.pem",
        "HF_HUB_OFFLINE": "1",
        "PSMODULEPATH": "C:/Windows/System32/WindowsPowerShell/v1.0/Modules",
        "PSModulePath": "/mixed-case/module/path",
    }


def test_uv_and_probe_receive_only_allowlisted_environment(monkeypatch) -> None:
    monkeypatch.setenv("UV_OVERRIDE", "/override.txt")
    monkeypatch.setenv("OPENAI_API_KEY", "provider-secret")
    monkeypatch.setenv("PYTHONPATH", "/untrusted")
    seen: list[dict[str, str]] = []

    class CompletedProcess:
        returncode = 0

        def poll(self):
            return 0

    def spawn(*_args, **kwargs):
        seen.append(kwargs["env"])
        return CompletedProcess()

    def run(*_args, **kwargs):
        seen.append(kwargs["env"])
        return subprocess.CompletedProcess([], 0)

    monkeypatch.setattr(model_install, "spawn_service", spawn)
    monkeypatch.setattr(model_install.subprocess, "run", run)
    model_install._run_uv(
        ["uv", "--version"],
        should_cancel=lambda: False,
        progress=lambda _message: None,
    )
    assert model_install._probe_install() is True

    assert len(seen) == 2
    assert seen[0]["UV_OVERRIDE"] == "/override.txt"
    assert "UV_OVERRIDE" not in seen[1]
    assert all("OPENAI_API_KEY" not in env for env in seen)
    assert all("PYTHONPATH" not in env for env in seen)


def test_install_uses_isolated_runtime_and_current_models_extra(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("FRISKET_MODEL_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(model_install, "is_installed", lambda: False)
    monkeypatch.setattr(model_install, "_probe_install", lambda: True)
    monkeypatch.setattr(model_install, "_uv_command", lambda: ["/bundled/uv"])
    monkeypatch.setattr(
        model_install.importlib.metadata, "version", lambda _name: "1.2.3"
    )
    calls: list[list[str]] = []

    class CompletedProcess:
        returncode = 0

        def poll(self):
            return self.returncode

    monkeypatch.setattr(
        model_install,
        "spawn_service",
        lambda argv, **_kwargs: calls.append(argv) or CompletedProcess(),
    )
    progress: list[str] = []

    model_install.install_docling(should_cancel=lambda: False, progress=progress.append)

    assert (
        calls[0][:4]
        == [
            "/bundled/uv",
            "venv",
            "--python",
            model_install.sys.executable,  # subprocess-boundary: assert the venv's base interpreter.
        ]
    )
    assert calls[1] == [
        "/bundled/uv",
        "pip",
        "install",
        "--python",
        str(model_install.runtime_python()),
        "--torch-backend",
        "cpu",
        "frisket-data[models]==1.2.3",
    ]
    assert (tmp_path / ".ready").is_file()
    assert progress[-1] == "Docling model server installed"


def test_cancellation_terminates_active_uv_process(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FRISKET_MODEL_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(model_install, "is_installed", lambda: False)
    monkeypatch.setattr(model_install, "_uv_command", lambda: ["/bundled/uv"])
    state = {"cancel": False}

    class RunningProcess:
        returncode = None
        terminated = False

        def poll(self):
            state["cancel"] = True
            return self.returncode

    process = RunningProcess()
    monkeypatch.setattr(model_install, "spawn_service", lambda *_a, **_kw: process)

    def stop(active):
        assert active is process
        process.terminated = True
        process.returncode = 0

    monkeypatch.setattr(model_install, "stop_service", stop)

    with pytest.raises(model_install.ModelInstallCancelled):
        model_install.install_docling(
            should_cancel=lambda: state["cancel"], progress=lambda _message: None
        )

    assert process.terminated
    assert not (tmp_path / ".ready").exists()


def test_is_installed_is_passive_marker_and_python_check(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FRISKET_MODEL_RUNTIME_DIR", str(tmp_path))
    python = model_install.runtime_python()
    python.parent.mkdir(parents=True)
    python.touch()
    assert model_install.is_installed() is False

    (tmp_path / ".ready").touch()
    monkeypatch.setattr(
        model_install.subprocess,
        "run",
        lambda *_a, **_kw: pytest.fail("passive readiness must not spawn a process"),
    )
    assert model_install.is_installed() is True


def test_probe_happens_before_ready_marker_is_published(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FRISKET_MODEL_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(model_install, "is_installed", lambda: False)
    monkeypatch.setattr(model_install, "_uv_command", lambda: ["/bundled/uv"])
    monkeypatch.setattr(model_install.importlib.metadata, "version", lambda _name: "1")

    class CompletedProcess:
        returncode = 0

        def poll(self):
            return 0

    monkeypatch.setattr(
        model_install, "spawn_service", lambda *_a, **_kw: CompletedProcess()
    )

    def probe():
        assert not (tmp_path / ".ready").exists()
        return True

    monkeypatch.setattr(model_install, "_probe_install", probe)
    model_install.install_docling(should_cancel=lambda: False, progress=lambda _m: None)
    assert (tmp_path / ".ready").is_file()


def test_waiting_for_install_lock_can_be_cancelled(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FRISKET_MODEL_RUNTIME_DIR", str(tmp_path))
    tmp_path.mkdir(exist_ok=True)
    held = FileLock(tmp_path / ".install.lock")
    held.acquire()
    checks = iter([False, False, True])
    monkeypatch.setattr(model_install.time, "sleep", lambda _seconds: None)
    try:
        with pytest.raises(model_install.ModelInstallCancelled):
            model_install.install_docling(
                should_cancel=lambda: next(checks), progress=lambda _message: None
            )
    finally:
        held.release()


def test_mac_install_does_not_request_linux_torch_index(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FRISKET_MODEL_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(model_install.sys, "platform", "darwin")
    monkeypatch.setattr(model_install, "is_installed", lambda: False)
    monkeypatch.setattr(model_install, "_probe_install", lambda: True)
    monkeypatch.setattr(model_install, "_uv_command", lambda: ["/bundled/uv"])
    monkeypatch.setattr(model_install.importlib.metadata, "version", lambda _name: "1")
    calls = []

    class CompletedProcess:
        returncode = 0

        def poll(self):
            return 0

    monkeypatch.setattr(
        model_install,
        "spawn_service",
        lambda argv, **_kwargs: calls.append(argv) or CompletedProcess(),
    )
    model_install.install_docling(should_cancel=lambda: False, progress=lambda _m: None)
    assert "--torch-backend" not in calls[1]
