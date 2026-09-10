"""Isolated Paddle OCR worker registered by the hosted-models Modal app."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

try:
    import modal
except ModuleNotFoundError:  # pragma: no cover - Modal is deployment-only
    modal = None  # type: ignore[assignment]

GPU = os.environ.get("FRISKET_MODAL_PADDLE_GPU", "L4")
MAX_CONTAINERS = int(os.environ.get("FRISKET_MODAL_PADDLE_MAX_CONTAINERS", "2"))
TIMEOUT_SECONDS = int(os.environ.get("FRISKET_MODAL_PADDLE_TIMEOUT", "3600"))
MODEL_CACHE = "/frisket-model-cache"
PADDLE_GPU_INDEX = "https://www.paddlepaddle.org.cn/packages/stable/cu126/"
PADDLE_VLLM_IMAGE = (
    "ccr-2vdh3abv-pub.cnc.bj.baidubce.com/paddlepaddle/"
    "paddleocr-genai-vllm-server@"
    "sha256:5713fd30ab76094b7b6a20d95fd8e26fa9dc452bcc90ccb16f1fb056bd2a0f4d"
)


def paddle_pipeline_install_commands() -> tuple[str, ...]:
    """Install the current pipeline apart from Paddle's vLLM environment."""

    return (
        "uv venv --python 3.12 /opt/frisket-paddle-pipeline",
        "uv pip install --python /opt/frisket-paddle-pipeline "
        "--requirements /tmp/requirements.txt",
        "uv pip uninstall --python /opt/frisket-paddle-pipeline paddlepaddle",
        "uv pip install --python /opt/frisket-paddle-pipeline "
        "paddlepaddle-gpu==3.3.1 "
        f"--index-url {PADDLE_GPU_INDEX} --exclude-newer false",
    )


def paddle_gpu_install_commands() -> tuple[str, str]:
    """Replace the portable CPU wheel with Paddle's CUDA 12.6 runtime."""

    return (
        "uv pip uninstall --system paddlepaddle",
        "uv pip install --system paddlepaddle-gpu==3.3.1 "
        f"--index-url {PADDLE_GPU_INDEX} --exclude-newer false",
    )


def model_cache_environment() -> dict[str, str]:
    return {
        "FLAGS_allocator_strategy": "naive_best_fit",
        "PADDLE_PDX_CACHE_HOME": f"{MODEL_CACHE}/paddlex",
        "PADDLE_PDX_MODEL_SOURCE": "modelscope",
        "FRISKET_PADDLE_DEVICE": "gpu:0",
    }


if modal is not None:
    sidecar_dir = Path(__file__).resolve().parents[4] / "sidecar"
    worker_dir = sidecar_dir / "workers" / "paddle_vllm"
    paddle_vllm_image = (
        modal.Image.from_registry(
            PADDLE_VLLM_IMAGE,
            setup_dockerfile_commands=["USER root"],
        )
        .entrypoint([])
        .apt_install("libgl1", "libglib2.0-0", "libgomp1")
        .pip_install("uv==0.11.29")
        .add_local_dir(
            sidecar_dir,
            remote_path="/opt/frisket-models",
            copy=True,
            ignore=[
                ".venv",
                ".pytest_cache",
                "**/__pycache__",
                "**/*.pyc",
                "fixtures",
                "tests",
                "workers",
            ],
        )
        .add_local_dir(
            worker_dir,
            remote_path="/opt/frisket-models/workers/paddle_vllm",
            copy=True,
        )
        .workdir("/opt/frisket-models")
        .run_commands(
            "uv export --frozen --no-dev --no-emit-project "
            "--extra ocr-paddle --output-file /tmp/requirements.txt",
            *paddle_pipeline_install_commands(),
        )
        .env(model_cache_environment())
    )
    pp_ocrv6_image = (
        modal.Image.from_registry(
            "nvidia/cuda:12.8.1-cudnn-runtime-ubuntu22.04",
            add_python="3.12",
        )
        .entrypoint([])
        .apt_install("libgl1", "libglib2.0-0", "libgomp1")
        .pip_install("uv==0.11.29")
        .add_local_dir(
            sidecar_dir,
            remote_path="/opt/frisket-models",
            copy=True,
            ignore=[
                ".venv",
                ".pytest_cache",
                "**/__pycache__",
                "**/*.pyc",
                "fixtures",
                "tests",
                "workers",
            ],
        )
        .workdir("/opt/frisket-models")
        .run_commands(
            "uv export --frozen --no-dev --no-emit-project "
            "--extra ocr-paddle --output-file /tmp/requirements.txt",
            "uv pip install --system --requirements /tmp/requirements.txt",
            *paddle_gpu_install_commands(),
            "uv pip install --system --no-deps /opt/frisket-models",
        )
        .env(model_cache_environment())
    )


def paddleocr_vl_worker():
    """Start Paddle's vLLM server and Frisket's authenticated OCR contract."""

    os.environ.update(model_cache_environment())
    subprocess.Popen(
        ["/bin/bash", "/opt/frisket-models/workers/paddle_vllm/entrypoint.sh"]
    )


def pp_ocrv6_worker():
    """Serve PP-OCRv6 without sharing PaddleOCR-VL's persistent vLLM process."""

    os.environ.update(model_cache_environment())
    from frisket_models.app import create_app
    from frisket_models.engines import Registry, default_registry

    registry = default_registry()
    return create_app(
        registry=Registry([registry.get("pp-ocrv6")]),
        concurrency=1,
    )


def register_paddleocr_vl_worker(app: Any, *, edge_secret_name: str, model_cache: Any):
    """Register PaddleOCR-VL's accelerated scale-to-zero L4 worker."""

    if modal is None:  # pragma: no cover - deployment-only dependency
        raise RuntimeError("Modal is required to register the PaddleOCR-VL worker")

    return app.function(
        image=paddle_vllm_image,
        gpu=GPU,
        cpu=4.0,
        memory=32768,
        min_containers=0,
        max_containers=MAX_CONTAINERS,
        scaledown_window=120,
        timeout=TIMEOUT_SECONDS,
        volumes={MODEL_CACHE: model_cache},
        secrets=[modal.Secret.from_name(edge_secret_name)],
    )(
        modal.concurrent(max_inputs=1)(
            modal.web_server(9000, startup_timeout=1800)(paddleocr_vl_worker)
        )
    )


def register_pp_ocrv6_worker(app: Any, *, edge_secret_name: str, model_cache: Any):
    """Register PP-OCRv6 in its own scale-to-zero L4 worker."""

    if modal is None:  # pragma: no cover - deployment-only dependency
        raise RuntimeError("Modal is required to register the PP-OCRv6 worker")

    return app.function(
        image=pp_ocrv6_image,
        gpu=GPU,
        cpu=4.0,
        memory=32768,
        min_containers=0,
        max_containers=MAX_CONTAINERS,
        scaledown_window=120,
        timeout=TIMEOUT_SECONDS,
        volumes={MODEL_CACHE: model_cache},
        secrets=[modal.Secret.from_name(edge_secret_name)],
    )(modal.concurrent(max_inputs=1)(modal.asgi_app()(pp_ocrv6_worker)))


__all__ = [
    "PADDLE_VLLM_IMAGE",
    "paddle_gpu_install_commands",
    "paddle_pipeline_install_commands",
    "paddleocr_vl_worker",
    "pp_ocrv6_worker",
    "register_paddleocr_vl_worker",
    "register_pp_ocrv6_worker",
]
