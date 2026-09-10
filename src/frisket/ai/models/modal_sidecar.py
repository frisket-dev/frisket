"""Scale-to-zero Modal entrypoint for the hosted ``frisket-models`` sidecar."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from frisket.ai.models.modal_dots import (
    DOTS_LINK_SECRET_NAME,
    register_dots_mocr_worker,
)
from frisket.ai.models.modal_glm_ocr import register_glm_ocr_worker
from frisket.ai.models.modal_moss import register_moss_worker
from frisket.ai.models.modal_parakeet import register_parakeet_worker
from frisket.ai.models.modal_paddle import (
    register_paddleocr_vl_worker,
    register_pp_ocrv6_worker,
)

try:
    import modal
except ModuleNotFoundError:  # pragma: no cover - Modal is deployment-only
    modal = None  # type: ignore[assignment]

APP_NAME = os.environ.get("FRISKET_MODAL_SIDECAR_APP", "frisket-models-sidecar")
# Expected payloads: edge=FRISKET_MODELS_TOKEN and
# link=FRISKET_MOSS_WORKER_TOKEN shared with isolated transcription workers.
EDGE_SECRET_NAME = os.environ.get(
    "FRISKET_MODAL_SIDECAR_EDGE_SECRET", "frisket-models-edge"
)
MOSS_LINK_SECRET_NAME = os.environ.get(
    "FRISKET_MODAL_MOSS_LINK_SECRET", "frisket-moss-link"
)
GPU = os.environ.get("FRISKET_MODAL_SIDECAR_GPU", "T4")
MAX_CONTAINERS = int(os.environ.get("FRISKET_MODAL_SIDECAR_MAX_CONTAINERS", "2"))
TIMEOUT_SECONDS = int(os.environ.get("FRISKET_MODAL_SIDECAR_TIMEOUT", "3600"))
MODEL_CACHE = "/frisket-model-cache"


def model_runtime_environment() -> dict[str, str]:
    """Keep resident gateway admission bounded to one request."""

    return {"FRISKET_MODELS_CONCURRENCY": "1"}


def model_cache_environment() -> dict[str, str]:
    """Point model libraries at the Volume after Modal mounts it."""

    return {
        "HF_HOME": f"{MODEL_CACHE}/hf",
        "TORCH_HOME": f"{MODEL_CACHE}/torch",
        "XDG_CACHE_HOME": f"{MODEL_CACHE}/cache",
        "PADDLE_PDX_CACHE_HOME": f"{MODEL_CACHE}/paddlex",
    }


def moss_worker_environment(worker: Any) -> dict[str, str]:
    """Build gateway configuration from its co-deployed MOSS function."""

    url = worker.get_web_url()
    if not url:
        raise RuntimeError("the registered MOSS worker has no web URL")
    return {"FRISKET_TRANSCRIPTION_MOSS_WORKER_URL": url}


def parakeet_worker_environment(worker: Any) -> dict[str, str]:
    """Build gateway configuration from its co-deployed Parakeet function."""

    url = worker.get_web_url()
    if not url:
        raise RuntimeError("the registered Parakeet worker has no web URL")
    token = os.environ.get("FRISKET_MOSS_WORKER_TOKEN")
    if not token:
        raise RuntimeError("FRISKET_MOSS_WORKER_TOKEN is not set")
    return {
        "FRISKET_TRANSCRIPTION_PARAKEET_TDT_WORKER_URL": url,
        "FRISKET_TRANSCRIPTION_PARAKEET_TDT_WORKER_TOKEN": token,
    }


def dots_worker_environment(worker: Any) -> dict[str, str]:
    """Build gateway configuration from its co-deployed dots.mocr worker."""

    url = worker.get_web_url()
    if not url:
        raise RuntimeError("the registered dots.mocr worker has no web URL")
    token = os.environ.get("FRISKET_OCR_DOTS_WORKER_TOKEN")
    if not token:
        raise RuntimeError("FRISKET_OCR_DOTS_WORKER_TOKEN is not set")
    return {
        "FRISKET_OCR_DOTS_WORKER_URL": url,
        "FRISKET_OCR_DOTS_WORKER_TOKEN": token,
    }


def paddle_worker_environment(worker: Any) -> dict[str, str]:
    """Build gateway configuration from its co-deployed Paddle worker."""

    url = worker.get_web_url()
    if not url:
        raise RuntimeError("the registered PaddleOCR-VL worker has no web URL")
    token = os.environ.get("FRISKET_MODELS_TOKEN")
    if not token:
        raise RuntimeError("FRISKET_MODELS_TOKEN is not set")
    return {
        "FRISKET_OCR_PADDLE_WORKER_URL": url,
        "FRISKET_OCR_PADDLE_WORKER_TOKEN": token,
    }


def glm_worker_environment(worker: Any) -> dict[str, str]:
    """Build gateway configuration from its co-deployed GLM-OCR worker."""

    url = worker.get_web_url()
    if not url:
        raise RuntimeError("the registered GLM-OCR worker has no web URL")
    token = os.environ.get("FRISKET_MODELS_TOKEN")
    if not token:
        raise RuntimeError("FRISKET_MODELS_TOKEN is not set")
    return {
        "FRISKET_OCR_GLM_WORKER_URL": url,
        "FRISKET_OCR_GLM_WORKER_TOKEN": token,
    }


def pp_ocrv6_worker_environment(worker: Any) -> dict[str, str]:
    """Build gateway configuration from its co-deployed PP-OCRv6 function."""

    url = worker.get_web_url()
    if not url:
        raise RuntimeError("the registered PP-OCRv6 worker has no web URL")
    token = os.environ.get("FRISKET_MODELS_TOKEN")
    if not token:
        raise RuntimeError("FRISKET_MODELS_TOKEN is not set")
    return {
        "FRISKET_OCR_PP_OCRV6_WORKER_URL": url,
        "FRISKET_OCR_PP_OCRV6_WORKER_TOKEN": token,
    }


if modal is not None:
    app = modal.App(APP_NAME)
    moss_worker = register_moss_worker(app)
    parakeet_worker = register_parakeet_worker(app)
    dots_mocr_worker = register_dots_mocr_worker(app)
    glm_ocr_worker = register_glm_ocr_worker(
        app,
        edge_secret_name=EDGE_SECRET_NAME,
    )
    model_cache = modal.Volume.from_name("frisket-model-cache", create_if_missing=True)
    paddleocr_vl_worker = register_paddleocr_vl_worker(
        app,
        edge_secret_name=EDGE_SECRET_NAME,
        model_cache=model_cache,
    )
    pp_ocrv6_worker = register_pp_ocrv6_worker(
        app,
        edge_secret_name=EDGE_SECRET_NAME,
        model_cache=model_cache,
    )
    sidecar_dir = Path(__file__).resolve().parents[4] / "sidecar"
    image = (
        modal.Image.from_registry(
            "nvidia/cuda:12.8.1-cudnn-runtime-ubuntu22.04",
            add_python="3.12",
        )
        .entrypoint([])
        .apt_install("ffmpeg", "libgl1", "libglib2.0-0", "libgomp1")
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
            "--extra convert --extra ner "
            "--extra transcribe --output-file /tmp/requirements.txt",
            "uv pip install --system --requirements /tmp/requirements.txt",
            "uv pip install --system --no-deps /opt/frisket-models",
        )
        .env(model_runtime_environment())
    )

    @app.function(
        image=image,
        gpu=GPU,
        cpu=4.0,
        memory=32768,
        min_containers=0,
        max_containers=MAX_CONTAINERS,
        scaledown_window=120,
        timeout=TIMEOUT_SECONDS,
        volumes={MODEL_CACHE: model_cache},
        secrets=[
            modal.Secret.from_name(EDGE_SECRET_NAME),
            modal.Secret.from_name(MOSS_LINK_SECRET_NAME),
            modal.Secret.from_name(DOTS_LINK_SECRET_NAME),
        ],
    )
    @modal.concurrent(max_inputs=1)
    @modal.asgi_app()
    def hosted_models():
        os.environ.update(model_cache_environment())
        os.environ.update(moss_worker_environment(moss_worker))
        os.environ.update(parakeet_worker_environment(parakeet_worker))
        os.environ.update(dots_worker_environment(dots_mocr_worker))
        os.environ.update(glm_worker_environment(glm_ocr_worker))
        os.environ.update(paddle_worker_environment(paddleocr_vl_worker))
        os.environ.update(pp_ocrv6_worker_environment(pp_ocrv6_worker))
        if moss_token := os.environ.get("FRISKET_MOSS_WORKER_TOKEN"):
            os.environ["FRISKET_TRANSCRIPTION_MOSS_WORKER_TOKEN"] = moss_token
        from frisket_models.modal_service import create_hosted_app

        return create_hosted_app()
