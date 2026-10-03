from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path

import pytest

from frisket.runtime import classifier_install, model_install


def test_sidecar_classifier_extra_is_exact_and_cpu_only() -> None:
    sidecar = Path(__file__).resolve().parents[2] / "sidecar" / "pyproject.toml"
    project = tomllib.loads(sidecar.read_text())["project"]

    assert project["optional-dependencies"]["classify"] == [
        "gliclass==0.1.20",
        "torch==2.14.0",
        "torchvision==0.29.0",
        "transformers==5.17.0",
        "pillow==12.3.0",
        "safetensors==0.8.0",
        "numpy==2.5.3",
        "huggingface-hub==1.31.0",
    ]
    assert "classify" not in project["optional-dependencies"]["all"][0]


def test_classifier_runtime_is_a_versioned_docling_sibling(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("FRISKET_MODEL_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(model_install.sys, "platform", "linux")

    assert classifier_install.runtime_dir() == tmp_path / "classification-v1"
    assert classifier_install.runtime_python() == (
        tmp_path / "classification-v1" / "venv" / "bin" / "python"
    )
    assert model_install.runtime_python() == tmp_path / "venv" / "bin" / "python"


@pytest.mark.parametrize("platform", ["linux", "win32", "darwin"])
def test_classifier_install_uses_its_own_profile_and_cpu_dependencies(
    tmp_path, monkeypatch, platform
) -> None:
    monkeypatch.setenv("FRISKET_MODEL_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(model_install.sys, "platform", platform)
    monkeypatch.setattr(model_install, "_uv_command", lambda: ["/bundled/uv"])
    source = tmp_path / "bundled source"
    monkeypatch.setattr(model_install, "model_server_source", lambda: source)
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
    monkeypatch.setattr(
        model_install.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0),
    )
    progress: list[str] = []

    classifier_install.install_classifiers(
        should_cancel=lambda: False, progress=progress.append
    )

    profile = tmp_path / "classification-v1"
    expected_python = (
        profile
        / "venv"
        / ("Scripts/python.exe" if platform == "win32" else "bin/python")
    )
    assert (
        calls
        == [
            [
                "/bundled/uv",
                "venv",
                "--python",
                model_install.sys.executable,  # subprocess-boundary: verify the private venv uses the running app's interpreter
                str(profile / "venv"),
            ],
            [
                "/bundled/uv",
                "pip",
                "install",
                "--python",
                str(expected_python),
                *(["--torch-backend", "cpu"] if platform != "darwin" else []),
                f"{source}[classify]",
            ],
        ]
    )
    assert (profile / ".ready").is_file()
    assert not (tmp_path / ".ready").exists()
    assert progress[-1] == "Local classifiers installed"


def test_classifier_probe_uses_the_profile_interpreter_and_optional_imports(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("FRISKET_MODEL_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(model_install.sys, "platform", "linux")
    profile_python = classifier_install.runtime_python()
    profile_python.parent.mkdir(parents=True)
    profile_python.touch()
    seen: list[tuple[list[str], dict[str, object]]] = []

    def run(argv, **kwargs):
        seen.append((argv, kwargs))
        return subprocess.CompletedProcess([], 0)

    monkeypatch.setattr(model_install.subprocess, "run", run)

    assert classifier_install._probe_install() is True
    argv, kwargs = seen[0]
    assert argv[:2] == [str(profile_python), "-c"]
    assert "gliclass" in argv[2]
    assert "transformers" in argv[2]
    assert "frisket_models" in argv[2]
    assert kwargs["env"] == model_install.model_child_environment()


def test_classifier_readiness_is_passive_and_profile_local(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("FRISKET_MODEL_RUNTIME_DIR", str(tmp_path))
    python = classifier_install.runtime_python()
    python.parent.mkdir(parents=True)
    python.touch()
    (tmp_path / ".ready").touch()

    assert classifier_install.is_installed() is False
    (classifier_install.runtime_dir() / ".ready").touch()
    monkeypatch.setattr(
        model_install.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("passive readiness must not spawn"),
    )
    assert classifier_install.is_installed() is True


def test_classifier_install_reuses_completed_profile(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FRISKET_MODEL_RUNTIME_DIR", str(tmp_path))
    python = classifier_install.runtime_python()
    python.parent.mkdir(parents=True)
    python.touch()
    (classifier_install.runtime_dir() / ".ready").touch()
    monkeypatch.setattr(
        model_install,
        "spawn_service",
        lambda *_args, **_kwargs: pytest.fail("must not reinstall"),
    )
    progress: list[str] = []

    classifier_install.install_classifiers(
        should_cancel=lambda: False, progress=progress.append
    )

    assert progress == ["Local classifiers are already installed"]


def test_failed_classifier_probe_does_not_publish_readiness(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("FRISKET_MODEL_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(model_install, "_uv_command", lambda: ["/bundled/uv"])
    monkeypatch.setattr(
        model_install, "model_server_source", lambda: tmp_path / "source"
    )

    class CompletedProcess:
        returncode = 0

        def poll(self):
            return self.returncode

    monkeypatch.setattr(
        model_install, "spawn_service", lambda *_args, **_kwargs: CompletedProcess()
    )
    monkeypatch.setattr(classifier_install, "_probe_install", lambda: False)

    with pytest.raises(RuntimeError, match="classifier environment"):
        classifier_install.install_classifiers(
            should_cancel=lambda: False, progress=lambda _message: None
        )

    assert not (classifier_install.runtime_dir() / ".ready").exists()
