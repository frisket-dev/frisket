from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path

import pytest

from frisket.runtime import embeddinggemma_install, model_install


def test_retrieval_runtime_lock_owns_only_the_evaluated_dependencies() -> None:
    root = Path(__file__).resolve().parents[2]
    project = tomllib.loads(
        (root / "sidecar" / "retrieval-runtime" / "pyproject.toml").read_text()
    )

    assert project["project"]["dependencies"] == [
        "numpy==2.4.3",
        "pillow==12.1.1",
        "safetensors==0.8.0",
        "tokenizers==0.23.3",
        "torch==2.14.1",
        "torchvision==0.29.1",
        "transformers==5.19.0",
    ]
    assert project["tool"]["uv"]["sources"]["torch"] == [
        {"index": "pytorch-cpu", "marker": "sys_platform != 'darwin'"}
    ]
    requirements = (root / "sidecar" / "retrieval-constraints.txt").read_text()
    assert "torch==2.14.1+cpu ; sys_platform != 'darwin'" in requirements
    assert "torchvision==0.29.1+cpu ; sys_platform != 'darwin'" in requirements
    assert "transformers==5.19.0" in requirements
    assert "--hash=sha256:" in requirements
    assert "nvidia-" not in requirements
    assert "cuda-" not in requirements


def test_retrieval_requirements_are_fresh_from_the_frozen_lock(tmp_path) -> None:
    root = Path(__file__).resolve().parents[2]
    exported = tmp_path / "retrieval-constraints.txt"

    subprocess.run(  # noqa: S603
        [
            *model_install._uv_command(),
            "export",
            "--project",
            "sidecar/retrieval-runtime",
            "--frozen",
            "--offline",
            "--no-dev",
            "--no-emit-project",
            "--no-header",
            "--output-file",
            str(exported),
        ],
        cwd=root,
        check=True,
    )

    committed = (root / "sidecar" / "retrieval-constraints.txt").read_text()
    _, _, committed_body = committed.partition("\n")
    _, _, committed_body = committed_body.partition("\n")
    assert exported.read_text() == committed_body


@pytest.mark.parametrize("platform", ["linux", "win32", "darwin"])
def test_embeddinggemma_installs_the_frozen_requirements_directly(
    tmp_path, monkeypatch, platform
) -> None:
    monkeypatch.setenv("FRISKET_MODEL_RUNTIME_DIR", str(tmp_path / "runtimes"))
    monkeypatch.setattr(model_install.sys, "platform", platform)
    monkeypatch.setattr(model_install, "_uv_command", lambda: ["/bundled/uv"])
    source = tmp_path / "bundled source"
    source.mkdir()
    requirements = source / "retrieval-constraints.txt"
    requirements.touch()
    monkeypatch.setattr(model_install, "model_server_source", lambda: source)
    monkeypatch.setattr(embeddinggemma_install, "_probe_install", lambda: True)
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

    embeddinggemma_install.install_embeddinggemma(
        should_cancel=lambda: False, progress=lambda _message: None
    )

    profile = tmp_path / "runtimes" / "embeddinggemma-v1"
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
                model_install.sys.executable,  # subprocess-boundary: expected installer argv; process mocked
                str(profile / "venv"),
            ],
            [
                "/bundled/uv",
                "--no-config",
                "pip",
                "install",
                "--python",
                str(expected_python),
                *(["--torch-backend", "cpu"] if platform != "darwin" else []),
                "--requirement",
                str(requirements),
            ],
        ]
    )
    assert (profile / ".ready").is_file()


def test_embeddinggemma_refuses_missing_frozen_requirements(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("FRISKET_MODEL_RUNTIME_DIR", str(tmp_path / "runtimes"))
    source = tmp_path / "bundled source"
    source.mkdir()
    monkeypatch.setattr(model_install, "model_server_source", lambda: source)
    monkeypatch.setattr(
        model_install,
        "spawn_service",
        lambda *_args, **_kwargs: pytest.fail("must fail before creating the venv"),
    )

    with pytest.raises(RuntimeError, match="dependency requirements"):
        embeddinggemma_install.install_embeddinggemma(
            should_cancel=lambda: False, progress=lambda _message: None
        )
