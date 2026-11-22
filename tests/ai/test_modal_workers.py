"""Acceptance contracts for isolated Modal workers and GPU preferences."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
from frisket_models import server_startup

from frisket.ai.models import (
    modal_gpu,
    modal_moss,
    modal_paddle,
    modal_parakeet,
    modal_sidecar,
)

ROOT = Path(__file__).resolve().parents[2]


class _FakeWorker:
    def __init__(self, url: str) -> None:
        self._url = url

    def get_web_url(self) -> str:
        return self._url


def _original(value: Any) -> Any:
    return next(
        (
            original
            for name, original in vars(value).items()
            if name.startswith("_sync_original_")
        ),
        value,
    )


def _function_spec(module: Any, name: str) -> tuple[Any, Any]:
    function = _original(module.app.registered_functions[name])
    return function._spec, function._webhook_config


def _named_secrets(spec: Any) -> set[str]:
    return {
        secret._name
        for secret in (_original(item) for item in spec.secrets)
        if secret._name is not None
    }


def _image_lineage(image: Any) -> list[str]:
    lineage: list[str] = []
    current = _original(image)
    while True:
        lineage.append(current._rep)
        dependencies = current._deps() if current._deps is not None else ()
        image_dependencies = [item for item in dependencies if hasattr(item, "_rep")]
        if not image_dependencies:
            return lineage
        assert len(image_dependencies) == 1
        current = _original(image_dependencies[0])


def _export_sidecar_requirements(tmp_path: Path, *extras: str) -> str:
    requirements = tmp_path / "requirements.txt"
    extra_args = [arg for extra in extras for arg in ("--extra", extra)]
    result = subprocess.run(
        [
            "uv",
            "export",
            "--locked",
            "--no-dev",
            "--no-emit-project",
            *extra_args,
            "--output-file",
            str(requirements),
        ],
        cwd=ROOT / "sidecar",
        # uv reads bare exclude-newer-package dates in the local time zone;
        # the checked-in lock records them as UTC midnight.
        env={**os.environ, "TZ": "UTC"},
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return requirements.read_text(encoding="utf-8")


# --- GPU preference ordering ------------------------------------------------


def test_gpu_preferences_uses_default_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FRISKET_MODAL_DOTS_GPU", raising=False)

    assert modal_gpu.gpu_preferences("FRISKET_MODAL_DOTS_GPU", ("L4", "A10")) == [
        "L4",
        "A10",
    ]


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        ("A10", ["A10"]),
        (" A100-80GB , L40S , L4 ", ["A100-80GB", "L40S", "L4"]),
    ],
)
def test_gpu_preferences_honors_operator_order(
    monkeypatch: pytest.MonkeyPatch, configured: str, expected: list[str]
) -> None:
    monkeypatch.setenv("FRISKET_MODAL_DOTS_GPU", configured)

    assert modal_gpu.gpu_preferences("FRISKET_MODAL_DOTS_GPU", ("L4", "A10")) == (
        expected
    )


@pytest.mark.parametrize("configured", ["", " ", "L4,", ",A10", "L4, ,A10"])
def test_gpu_preferences_rejects_empty_choices(
    monkeypatch: pytest.MonkeyPatch, configured: str
) -> None:
    monkeypatch.setenv("FRISKET_MODAL_DOTS_GPU", configured)

    with pytest.raises(ValueError, match="FRISKET_MODAL_DOTS_GPU"):
        modal_gpu.gpu_preferences("FRISKET_MODAL_DOTS_GPU", ("L4", "A10"))


# --- Worker Dockerfiles -----------------------------------------------------


def test_locked_modal_workers_ignore_ambient_package_mirrors() -> None:
    sync = "uv sync --locked --no-dev --default-index https://pypi.org/simple"
    for worker in ("whisper_turbo", "parakeet_tdt"):
        dockerfile = ROOT / "sidecar" / "workers" / worker / "Dockerfile"
        # rule19: two-sources — both independently maintained worker images must pin the same install command
        assert sync in dockerfile.read_text(encoding="utf-8")


def test_dots_image_exposes_vllms_existing_python_to_modal() -> None:
    dockerfile_path = ROOT / "sidecar/workers/dots_mocr/Dockerfile"
    # rule19: container build contract pinned in the worker Dockerfile for Modal's python lookup
    dockerfile = dockerfile_path.read_text(encoding="utf-8")

    assert "RUN ln -s /usr/bin/python3.12 /usr/local/bin/python" in dockerfile


# --- Shared Modal app & worker registration ---------------------------------


def test_models_sidecar_and_moss_share_one_modal_app() -> None:
    pytest.importorskip("modal")

    assert set(modal_sidecar.app.registered_functions) == {
        "dots_mocr_worker",
        "glm_ocr_worker",
        "hosted_models",
        "moss_worker",
        "parakeet_worker",
        "paddleocr_vl_worker",
        "pp_ocrv6_worker",
    }


def test_models_sidecar_separates_edge_config_and_moss_link_secrets() -> None:
    pytest.importorskip("modal")

    spec, _webhook = _function_spec(modal_sidecar, "hosted_models")
    assert _original(spec.image) is _original(modal_sidecar.image)
    assert _named_secrets(spec) == {
        "frisket-dots-link",
        "frisket-models-edge",
        "frisket-moss-link",
    }
    assert spec.gpus == ["L4", "A10", "L40S", "A100-40GB", "A100-80GB"]
    assert (spec.cpu, spec.memory) == (4.0, 32768)
    assert set(spec.volumes) == {"/frisket-model-cache"}


def test_modal_registers_dots_mocr_as_an_isolated_worker() -> None:
    pytest.importorskip("modal")

    worker, _ = _function_spec(modal_sidecar, "dots_mocr_worker")
    gateway, _ = _function_spec(modal_sidecar, "hosted_models")

    assert _original(worker.image) is not _original(gateway.image)
    assert worker.gpus == ["L4", "A10", "L40S", "A100-40GB", "A100-80GB"]
    assert "frisket-dots-link" in _named_secrets(worker)
    assert "frisket-dots-link" in _named_secrets(gateway)


def test_modal_registers_glm_ocr_as_an_isolated_worker() -> None:
    pytest.importorskip("modal")

    worker, _ = _function_spec(modal_sidecar, "glm_ocr_worker")
    gateway, _ = _function_spec(modal_sidecar, "hosted_models")

    assert _original(worker.image) is not _original(gateway.image)
    assert "frisket-models-edge" in _named_secrets(worker)
    assert worker.gpus == ["L4", "A10", "L40S", "A100-40GB", "A100-80GB"]
    assert worker.memory == 16384


def test_moss_modal_graph_uses_native_image_and_isolated_link_secret() -> None:
    pytest.importorskip("modal")

    spec, webhook = _function_spec(modal_sidecar, "moss_worker")

    assert _original(spec.image) is _original(modal_moss.image)
    lineage = _image_lineage(spec.image)
    assert any(
        "from_dockerfile.<locals>.build_dockerfile_base" in item for item in lineage
    )
    assert all("from_registry" not in item for item in lineage)
    assert _named_secrets(spec) == {"frisket-moss-link"}
    assert spec.gpus == ["A10", "L4", "L40S", "A100-40GB", "A100-80GB"]
    assert (spec.cpu, spec.memory) == (2.0, 16384)
    assert webhook.web_server_port == 9000
    assert webhook.web_server_startup_timeout == 5


def test_modal_registers_paddle_engines_as_separate_gpu_workers() -> None:
    pytest.importorskip("modal")

    worker, _ = _function_spec(modal_sidecar, "paddleocr_vl_worker")
    pp_worker, _ = _function_spec(modal_sidecar, "pp_ocrv6_worker")
    gateway, _ = _function_spec(modal_sidecar, "hosted_models")

    assert _original(worker.image) is _original(modal_paddle.paddle_vllm_image)
    assert _original(pp_worker.image) is _original(modal_paddle.pp_ocrv6_image)
    assert _original(worker.image) is not _original(pp_worker.image)
    assert _original(worker.image) is not _original(gateway.image)
    assert worker.gpus == ["L4", "A10", "L40S", "A100-40GB", "A100-80GB"]
    assert pp_worker.gpus == ["L4", "A10", "L40S", "A100-40GB", "A100-80GB"]
    assert (worker.cpu, worker.memory) == (4.0, 32768)
    assert (pp_worker.cpu, pp_worker.memory) == (4.0, 32768)
    assert _named_secrets(worker) == {"frisket-models-edge"}
    assert _named_secrets(pp_worker) == {"frisket-models-edge"}
    assert set(worker.volumes) == {"/frisket-model-cache"}
    assert set(pp_worker.volumes) == {"/frisket-model-cache"}


def test_modal_registers_parakeet_as_an_isolated_worker() -> None:
    pytest.importorskip("modal")

    worker, webhook = _function_spec(modal_sidecar, "parakeet_worker")
    gateway, _ = _function_spec(modal_sidecar, "hosted_models")

    assert _original(worker.image) is _original(modal_parakeet.image)
    assert _original(worker.image) is not _original(gateway.image)
    assert _named_secrets(worker) == {"frisket-moss-link"}
    assert worker.gpus == ["T4", "L4", "A10", "L40S", "A100-40GB", "A100-80GB"]
    assert (worker.cpu, worker.memory) == (2.0, 16384)
    assert webhook.web_server_port == 9000
    assert webhook.web_server_startup_timeout == 5


# --- Gateway environment & runtime wiring -----------------------------------


def test_models_sidecar_sets_cache_paths_only_at_runtime() -> None:
    assert modal_sidecar.model_cache_environment() == {
        "HF_HOME": "/frisket-model-cache/hf",
        "TORCH_HOME": "/frisket-model-cache/torch",
        "XDG_CACHE_HOME": "/frisket-model-cache/cache",
        "PADDLE_PDX_CACHE_HOME": "/frisket-model-cache/paddlex",
    }


def test_modal_provides_the_dots_worker_url_and_token_to_the_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FRISKET_OCR_DOTS_WORKER_TOKEN", "worker-token")

    assert modal_sidecar.dots_worker_environment(
        _FakeWorker("https://dots-worker.example.test")
    ) == {
        "FRISKET_OCR_DOTS_WORKER_URL": "https://dots-worker.example.test",
        "FRISKET_OCR_DOTS_WORKER_TOKEN": "worker-token",
    }


def test_modal_provides_glm_worker_url_and_edge_token_to_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FRISKET_MODELS_TOKEN", "worker-token")

    assert modal_sidecar.glm_worker_environment(
        _FakeWorker("https://glm-worker.example.test")
    ) == {
        "FRISKET_OCR_GLM_WORKER_URL": "https://glm-worker.example.test",
        "FRISKET_OCR_GLM_WORKER_TOKEN": "worker-token",
    }


def test_models_sidecar_gets_moss_url_from_registered_function() -> None:
    env = modal_sidecar.moss_worker_environment(
        _FakeWorker("https://moss-worker.example.test")
    )

    assert env == {
        "FRISKET_TRANSCRIPTION_MOSS_WORKER_URL": "https://moss-worker.example.test"
    }


def test_modal_provides_paddle_worker_url_and_existing_edge_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FRISKET_MODELS_TOKEN", "edge-token")
    worker = _FakeWorker("https://paddle-worker.example.test")

    assert modal_sidecar.paddle_worker_environment(worker) == {
        "FRISKET_OCR_PADDLE_WORKER_URL": "https://paddle-worker.example.test",
        "FRISKET_OCR_PADDLE_WORKER_TOKEN": "edge-token",
    }
    assert modal_sidecar.pp_ocrv6_worker_environment(worker) == {
        "FRISKET_OCR_PP_OCRV6_WORKER_URL": "https://paddle-worker.example.test",
        "FRISKET_OCR_PP_OCRV6_WORKER_TOKEN": "edge-token",
    }


def test_modal_provides_parakeet_worker_url_and_private_token_to_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FRISKET_MOSS_WORKER_TOKEN", "private-worker-token")
    monkeypatch.setenv("FRISKET_MODELS_TOKEN", "public-edge-token")

    assert modal_sidecar.parakeet_worker_environment(
        _FakeWorker("https://parakeet-worker.example.test")
    ) == {
        "FRISKET_TRANSCRIPTION_PARAKEET_TDT_WORKER_URL": (
            "https://parakeet-worker.example.test"
        ),
        "FRISKET_TRANSCRIPTION_PARAKEET_TDT_WORKER_TOKEN": "private-worker-token",
    }


def test_modal_refuses_missing_parakeet_private_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FRISKET_MOSS_WORKER_TOKEN", raising=False)

    with pytest.raises(RuntimeError, match="FRISKET_MOSS_WORKER_TOKEN"):
        modal_sidecar.parakeet_worker_environment(
            _FakeWorker("https://parakeet-worker.example.test")
        )


# --- Worker-specific runtime & dependency contracts -------------------------


def test_moss_modal_runtime_image_id_is_namespaced() -> None:
    assert modal_moss.modal_runtime_image_id("im-Abc123") == "modal:im-Abc123"


@pytest.mark.parametrize(
    "image_id",
    [
        "",
        "im-",
        "modal:im-Abc123",
        "sha256:" + "a" * 64,
        "im-has space",
    ],
)
def test_moss_modal_rejects_missing_or_malformed_runtime_image_id(
    image_id: str,
) -> None:
    with pytest.raises(RuntimeError, match="MODAL_IMAGE_ID"):
        modal_moss.modal_runtime_image_id(image_id)


def test_models_sidecar_selected_extras_match_the_checked_in_lock(
    tmp_path: Path,
) -> None:
    exported = _export_sidecar_requirements(tmp_path, "convert", "ner", "transcribe")
    assert "\npaddleocr==" not in exported
    assert "\npaddlepaddle==" not in exported


def test_paddle_worker_keeps_pipeline_out_of_official_vllm_environment() -> None:
    assert modal_paddle.PADDLE_VLLM_IMAGE.endswith(
        "@sha256:5713fd30ab76094b7b6a20d95fd8e26fa9dc452bcc90ccb16f1fb056bd2a0f4d"
    )


def test_pp_ocrv6_worker_retains_cuda_paddle_install() -> None:
    assert modal_paddle.paddle_gpu_install_commands() == (
        "uv pip uninstall --system paddlepaddle",
        "uv pip install --system paddlepaddle-gpu==3.3.1 "
        "--index-url https://www.paddlepaddle.org.cn/packages/stable/cu126/ "
        "--exclude-newer false",
    )
    assert modal_paddle.paddle_pipeline_install_commands() == (
        "uv venv --python 3.12 /opt/frisket-paddle-pipeline",
        "uv pip install --python /opt/frisket-paddle-pipeline "
        "--requirements /tmp/requirements.txt",
        "uv pip uninstall --python /opt/frisket-paddle-pipeline paddlepaddle",
        "uv pip install --python /opt/frisket-paddle-pipeline "
        "paddlepaddle-gpu==3.3.1 "
        "--index-url https://www.paddlepaddle.org.cn/packages/stable/cu126/ "
        "--exclude-newer false",
    )


def test_paddle_worker_starts_the_two_process_entrypoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    monkeypatch.setattr(server_startup, "container_address", lambda: "10.0.0.1")
    monkeypatch.setattr(
        server_startup,
        "start_server",
        lambda command, **kwargs: calls.append(command),
    )

    modal_paddle.paddleocr_vl_worker()

    assert calls == [
        ["/bin/bash", "/opt/frisket-models/workers/paddle_vllm/entrypoint.sh"]
    ]


def test_paddle_worker_selects_non_autogrowth_allocator_before_import() -> None:
    assert modal_paddle.model_cache_environment()["FLAGS_allocator_strategy"] == (
        "naive_best_fit"
    )


def test_paddle_worker_dependency_closure_excludes_torch(tmp_path: Path) -> None:
    exported = _export_sidecar_requirements(tmp_path, "ocr-paddle")
    assert "\ntorch==" not in exported


def test_parakeet_worker_maps_private_token_and_runtime_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def launch(command: list[str], *, env: dict[str, str], **kwargs) -> None:
        captured["command"] = command
        captured["env"] = env

    monkeypatch.setenv("FRISKET_MOSS_WORKER_TOKEN", "private-worker-token")
    monkeypatch.setenv("MODAL_IMAGE_ID", "im-Abc123")
    monkeypatch.setattr(server_startup, "start_server", launch)
    monkeypatch.setattr(server_startup, "container_address", lambda: "10.0.0.1")

    modal_parakeet.parakeet_worker()

    assert captured["command"] == [
        "uvicorn",
        "--factory",
        "frisket_parakeet_tdt.app:create_app",
        "--host",
        "0.0.0.0",
        "--port",
        "9000",
    ]
    worker_env = captured["env"]
    assert worker_env["FRISKET_TRANSCRIPTION_WORKER_TOKEN"] == "private-worker-token"
    assert worker_env["FRISKET_TRANSCRIPTION_WORKER_IMAGE_ID"] == "modal:im-Abc123"


def test_parakeet_worker_refuses_missing_private_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FRISKET_MOSS_WORKER_TOKEN", raising=False)

    with pytest.raises(RuntimeError, match="FRISKET_MOSS_WORKER_TOKEN"):
        modal_parakeet.parakeet_worker()
