"""Isolated GLM-OCR worker registered by the hosted-models Modal app."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

try:
    import modal
except ModuleNotFoundError:  # pragma: no cover - Modal is deployment-only
    modal = None  # type: ignore[assignment]

GPU = os.environ.get("FRISKET_MODAL_GLM_OCR_GPU", "L4")
MAX_CONTAINERS = int(os.environ.get("FRISKET_MODAL_GLM_OCR_MAX_CONTAINERS", "2"))
TIMEOUT_SECONDS = int(os.environ.get("FRISKET_MODAL_GLM_OCR_TIMEOUT", "3600"))

if modal is not None:
    sidecar_dir = Path(__file__).resolve().parents[4] / "sidecar"
    image = modal.Image.from_dockerfile(
        sidecar_dir / "workers" / "glm_ocr" / "Dockerfile",
        context_dir=sidecar_dir,
    ).entrypoint([])


def glm_ocr_worker() -> None:
    """Start the authenticated GLM-OCR proxy and loopback vLLM server."""

    subprocess.Popen(["/app/sidecar/workers/glm_ocr/entrypoint.sh"])


def register_glm_ocr_worker(app: Any, *, edge_secret_name: str):
    """Register the GLM-OCR isolated scale-to-zero L4 worker."""

    if modal is None:  # pragma: no cover - deployment-only dependency
        raise RuntimeError("Modal is required to register the GLM-OCR worker")

    return app.function(
        image=image,
        gpu=GPU,
        cpu=4.0,
        memory=16384,
        min_containers=0,
        max_containers=MAX_CONTAINERS,
        scaledown_window=120,
        timeout=TIMEOUT_SECONDS,
        secrets=[modal.Secret.from_name(edge_secret_name)],
    )(
        modal.concurrent(max_inputs=1)(
            modal.web_server(9000, startup_timeout=1500)(glm_ocr_worker)
        )
    )


__all__ = ["glm_ocr_worker", "register_glm_ocr_worker"]
