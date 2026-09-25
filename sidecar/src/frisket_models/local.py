"""CPU-only factory for Frisket's optional native model runtime."""

from __future__ import annotations

import os
from functools import partial

from frisket_models.app import create_app as create_sidecar_app
from frisket_models.engines import Engine, Registry, load_docling


def create_app():
    """Create the minimal local sidecar without enabling unrelated engines."""
    token = os.environ.get("FRISKET_LOCAL_MODELS_TOKEN")
    if not token:
        raise RuntimeError("FRISKET_LOCAL_MODELS_TOKEN is not set")
    registry = Registry(
        [
            Engine(
                "docling",
                "/to-markdown",
                ["docling"],
                partial(load_docling, device="cpu"),
            )
        ]
    )
    return create_sidecar_app(token=token, registry=registry, concurrency=1)


__all__ = ["create_app"]
