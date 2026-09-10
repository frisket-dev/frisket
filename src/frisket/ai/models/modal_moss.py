"""Isolated MOSS worker registered by the hosted-models Modal app."""

from __future__ import annotations

import os
import re
from pathlib import Path

try:
    import modal
except ModuleNotFoundError:  # pragma: no cover - Modal is deployment-only
    modal = None  # type: ignore[assignment]

MOSS_LINK_SECRET_NAME = os.environ.get(
    "FRISKET_MODAL_MOSS_LINK_SECRET", "frisket-moss-link"
)
GPU = os.environ.get("FRISKET_MODAL_MOSS_GPU", "A10")
MAX_CONTAINERS = int(os.environ.get("FRISKET_MODAL_MOSS_MAX_CONTAINERS", "2"))
TIMEOUT_SECONDS = int(os.environ.get("FRISKET_MODAL_MOSS_TIMEOUT", "3600"))
_MODAL_IMAGE_ID = re.compile(r"^im-[A-Za-z0-9]+$")


def modal_runtime_image_id(image_id: str | None) -> str:
    """Namespace Modal's injected immutable image identity for the wire."""

    if image_id is None or _MODAL_IMAGE_ID.fullmatch(image_id) is None:
        raise RuntimeError("MODAL_IMAGE_ID must be a valid Modal image id")
    return f"modal:{image_id}"


if modal is not None:
    sidecar_dir = Path(__file__).resolve().parents[4] / "sidecar"
    image = modal.Image.from_dockerfile(
        sidecar_dir / "workers" / "moss" / "Dockerfile",
        context_dir=sidecar_dir,
    ).entrypoint([])


def moss_worker():
    """Start the authenticated MOSS worker server."""

    import subprocess

    worker_env = os.environ.copy()
    worker_env["FRISKET_TRANSCRIPTION_WORKER_IMAGE_ID"] = modal_runtime_image_id(
        os.environ.get("MODAL_IMAGE_ID")
    )
    subprocess.Popen(
        ["/app/sidecar/workers/moss/entrypoint.sh"],
        env=worker_env,
    )


def register_moss_worker(app):
    """Register MOSS's isolated image and GPU pool in a shared Modal app."""

    if modal is None:  # pragma: no cover - deployment-only dependency
        raise RuntimeError("Modal is required to register the MOSS worker")

    registered = app.function(
        image=image,
        gpu=GPU,
        cpu=2.0,
        memory=16384,
        min_containers=0,
        max_containers=MAX_CONTAINERS,
        scaledown_window=120,
        timeout=TIMEOUT_SECONDS,
        env={
            "FRISKET_MOSS_WORKER_CONCURRENCY": "1",
            "FRISKET_MOSS_WORKER_PORT": "9000",
        },
        secrets=[modal.Secret.from_name(MOSS_LINK_SECRET_NAME)],
    )(
        modal.concurrent(max_inputs=1)(
            modal.web_server(9000, startup_timeout=1800)(moss_worker)
        )
    )

    return registered


__all__ = ["modal_runtime_image_id", "moss_worker", "register_moss_worker"]
