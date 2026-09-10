"""Authenticated Frisket contract around the hosted PaddleOCR-VL pipeline."""

from frisket_models.app import create_app as create_sidecar_app
from frisket_models.engines import Registry, default_registry


def create_app():
    registry = default_registry()
    return create_sidecar_app(
        registry=Registry([registry.get("paddleocr-vl")]),
        concurrency=1,
    )
