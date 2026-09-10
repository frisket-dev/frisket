"""ASGI entrypoint for the isolated MOSS transcription worker.

The reusable Boundary-B runtime (validation, no-queue admission, byte bound,
spooling, cleanup) lives in ``frisket_models.transcription.worker``; this module
only binds the MOSS ``REGISTRATION`` to it and reads deployment knobs from the
environment.  Run it with, e.g.::

    uvicorn frisket_worker_moss.app:app --host 0.0.0.0 --port 9000
"""

from __future__ import annotations

import os

from fastapi import FastAPI
from frisket_models.transcription.worker import (
    DEFAULT_MAX_UPLOAD_BYTES,
    create_worker_app,
)

from .adapter import REGISTRATION


def create_app() -> FastAPI:
    """Bind deployment settings to the shared Boundary-B worker."""

    # ``FRISKET_TRANSCRIPTION_WORKER_IMAGE_ID`` is read by the wrapper;
    # without it the worker truthfully stays unavailable.
    return create_worker_app(
        REGISTRATION,
        token=os.environ.get("FRISKET_MOSS_WORKER_TOKEN"),
        concurrency=int(os.environ.get("FRISKET_MOSS_WORKER_CONCURRENCY", "1")),
        max_upload_bytes=int(
            os.environ.get(
                "FRISKET_TRANSCRIPTION_WORKER_MAX_UPLOAD_BYTES",
                str(DEFAULT_MAX_UPLOAD_BYTES),
            )
        ),
    )


app = create_app()

__all__ = ["app", "create_app"]
