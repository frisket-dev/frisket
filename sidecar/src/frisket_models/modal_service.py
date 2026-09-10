"""The bounded engine bundle served by Frisket Cloud on Modal."""

from __future__ import annotations

from fastapi import FastAPI

from frisket_models.app import create_app
from frisket_models.engines import Registry, default_registry

HOSTED_ENGINE_NAMES = (
    "dots.mocr",
    "glm-ocr",
    "paddleocr-vl",
    "pp-ocrv6",
    "docling",
    "gliner",
    "whisper-turbo",
)


def create_hosted_app(
    token: str | None = None,
    concurrency: int | None = None,
) -> FastAPI:
    """Create the hosted sidecar without optional or unlicensed engines.

    MOSS remains an isolated worker registered by ``create_app`` from its
    existing deployment environment. Chandra, reranking, and embeddings are
    intentionally absent from this resident bundle.
    """

    available = default_registry()
    hosted = Registry([available.get(name) for name in HOSTED_ENGINE_NAMES])
    return create_app(token=token, registry=hosted, concurrency=concurrency)


__all__ = ["HOSTED_ENGINE_NAMES", "create_hosted_app"]
