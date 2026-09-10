from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from frisket.ai.models import modal_paddle


def _original(value: Any) -> Any:
    return next(
        (
            original
            for name, original in vars(value).items()
            if name.startswith("_sync_original_")
        ),
        value,
    )


def _function_spec(module: Any, name: str) -> Any:
    function = _original(module.app.registered_functions[name])
    return function._spec


def _named_secrets(spec: Any) -> set[str]:
    return {
        secret._name
        for secret in (_original(item) for item in spec.secrets)
        if secret._name is not None
    }


def test_modal_registers_paddle_engines_as_separate_l4_workers() -> None:
    pytest.importorskip("modal")
    from frisket.ai.models import modal_sidecar

    worker = _function_spec(modal_sidecar, "paddleocr_vl_worker")
    pp_worker = _function_spec(modal_sidecar, "pp_ocrv6_worker")
    gateway = _function_spec(modal_sidecar, "hosted_models")

    assert _original(worker.image) is _original(modal_paddle.paddle_vllm_image)
    assert _original(pp_worker.image) is _original(modal_paddle.pp_ocrv6_image)
    assert _original(worker.image) is not _original(pp_worker.image)
    assert _original(worker.image) is not _original(gateway.image)
    assert (worker.gpus, worker.cpu, worker.memory) == ("L4", 4.0, 32768)
    assert (pp_worker.gpus, pp_worker.cpu, pp_worker.memory) == (
        "L4",
        4.0,
        32768,
    )
    assert _named_secrets(worker) == {"frisket-models-edge"}
    assert _named_secrets(pp_worker) == {"frisket-models-edge"}
    assert set(worker.volumes) == {"/frisket-model-cache"}
    assert set(pp_worker.volumes) == {"/frisket-model-cache"}


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
    monkeypatch.setattr(
        modal_paddle.subprocess,
        "Popen",
        lambda command: calls.append(command),
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
    root = Path(__file__).resolve().parents[2]
    requirements = tmp_path / "requirements.txt"
    result = subprocess.run(
        [
            "uv",
            "export",
            "--locked",
            "--no-dev",
            "--no-emit-project",
            "--extra",
            "ocr-paddle",
            "--output-file",
            str(requirements),
        ],
        cwd=root / "sidecar",
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "\ntorch==" not in requirements.read_text()


def test_modal_provides_paddle_worker_url_and_existing_edge_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from frisket.ai.models import modal_sidecar

    class PaddleWorker:
        def get_web_url(self) -> str:
            return "https://paddle-worker.example.test"

    monkeypatch.setenv("FRISKET_MODELS_TOKEN", "edge-token")

    assert modal_sidecar.paddle_worker_environment(PaddleWorker()) == {
        "FRISKET_OCR_PADDLE_WORKER_URL": "https://paddle-worker.example.test",
        "FRISKET_OCR_PADDLE_WORKER_TOKEN": "edge-token",
    }
    assert modal_sidecar.pp_ocrv6_worker_environment(PaddleWorker()) == {
        "FRISKET_OCR_PP_OCRV6_WORKER_URL": "https://paddle-worker.example.test",
        "FRISKET_OCR_PP_OCRV6_WORKER_TOKEN": "edge-token",
    }
