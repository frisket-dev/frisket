"""Isolated Parakeet worker registered by the hosted-models Modal app."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

from frisket.ai.models.modal_moss import MOSS_LINK_SECRET_NAME, modal_runtime_image_id

try:
    import modal
except ModuleNotFoundError:  # pragma: no cover - Modal is deployment-only
    modal = None  # type: ignore[assignment]

GPU = os.environ.get("FRISKET_MODAL_PARAKEET_GPU", "T4")
MAX_CONTAINERS = int(os.environ.get("FRISKET_MODAL_PARAKEET_MAX_CONTAINERS", "2"))
TIMEOUT_SECONDS = int(os.environ.get("FRISKET_MODAL_PARAKEET_TIMEOUT", "3600"))
WORKER_PORT = "9000"

if modal is not None:
    sidecar_dir = Path(__file__).resolve().parents[4] / "sidecar"
    image = modal.Image.from_dockerfile(
        sidecar_dir / "workers" / "parakeet_tdt" / "Dockerfile",
        context_dir=sidecar_dir,
    ).entrypoint([])


def parakeet_worker() -> None:
    """Start the authenticated fixed-model Parakeet worker server."""

    worker_env = os.environ.copy()
    link_token = worker_env.get("FRISKET_MOSS_WORKER_TOKEN")
    if not link_token:
        raise RuntimeError("FRISKET_MOSS_WORKER_TOKEN is not set")
    worker_env["FRISKET_TRANSCRIPTION_WORKER_TOKEN"] = link_token
    worker_env["FRISKET_TRANSCRIPTION_WORKER_IMAGE_ID"] = modal_runtime_image_id(
        worker_env.get("MODAL_IMAGE_ID")
    )
    subprocess.Popen(
        [
            "uvicorn",
            "--factory",
            "frisket_parakeet_tdt.app:create_app",
            "--host",
            "0.0.0.0",
            "--port",
            WORKER_PORT,
        ],
        env=worker_env,
    )


def register_parakeet_worker(app: Any):
    """Register Parakeet's isolated scale-to-zero T4 worker."""

    if modal is None:  # pragma: no cover - deployment-only dependency
        raise RuntimeError("Modal is required to register the Parakeet worker")

    return app.function(
        image=image,
        gpu=GPU,
        cpu=2.0,
        memory=16384,
        min_containers=0,
        max_containers=MAX_CONTAINERS,
        scaledown_window=120,
        timeout=TIMEOUT_SECONDS,
        env={"FRISKET_TRANSCRIPTION_WORKER_CONCURRENCY": "1"},
        secrets=[modal.Secret.from_name(MOSS_LINK_SECRET_NAME)],
    )(
        modal.concurrent(max_inputs=1)(
            modal.web_server(9000, startup_timeout=1800)(parakeet_worker)
        )
    )


__all__ = ["parakeet_worker", "register_parakeet_worker"]
