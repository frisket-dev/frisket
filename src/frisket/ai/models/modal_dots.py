"""Isolated dots.mocr worker registered by the hosted-models Modal app."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

try:
    import modal
except ModuleNotFoundError:  # pragma: no cover - Modal is deployment-only
    modal = None  # type: ignore[assignment]

DOTS_LINK_SECRET_NAME = os.environ.get(
    "FRISKET_MODAL_DOTS_LINK_SECRET", "frisket-dots-link"
)
GPU = os.environ.get("FRISKET_MODAL_DOTS_GPU", "L4")
MAX_CONTAINERS = int(os.environ.get("FRISKET_MODAL_DOTS_MAX_CONTAINERS", "2"))
TIMEOUT_SECONDS = int(os.environ.get("FRISKET_MODAL_DOTS_TIMEOUT", "3600"))
if modal is not None:
    sidecar_dir = Path(__file__).resolve().parents[4] / "sidecar"
    image = modal.Image.from_dockerfile(
        sidecar_dir / "workers" / "dots_mocr" / "Dockerfile",
        context_dir=sidecar_dir,
    ).entrypoint([])


def dots_mocr_worker() -> None:
    """Start the authenticated dots.mocr proxy and loopback vLLM server."""

    subprocess.Popen(["/app/sidecar/workers/dots_mocr/entrypoint.sh"])


def register_dots_mocr_worker(app):
    """Register dots.mocr's isolated image and scale-to-zero L4 pool."""

    if modal is None:  # pragma: no cover - deployment-only dependency
        raise RuntimeError("Modal is required to register the dots.mocr worker")

    return app.function(
        image=image,
        gpu=GPU,
        cpu=4.0,
        memory=32768,
        min_containers=0,
        max_containers=MAX_CONTAINERS,
        scaledown_window=120,
        timeout=TIMEOUT_SECONDS,
        secrets=[modal.Secret.from_name(DOTS_LINK_SECRET_NAME)],
    )(
        modal.concurrent(max_inputs=1)(
            modal.web_server(9000, startup_timeout=1800)(dots_mocr_worker)
        )
    )


__all__ = [
    "DOTS_LINK_SECRET_NAME",
    "dots_mocr_worker",
    "register_dots_mocr_worker",
]
