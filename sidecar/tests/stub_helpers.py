"""Stub engine registry and auth constants shared across sidecar tests.

Plain importable module (not conftest.py) so `from stub_helpers import
stub_registry` has a single, unambiguous target under pytest's default
prepend import mode — a same-named conftest.py living in another test
directory collected in the same session would otherwise race for the
one `sys.modules["conftest"]` slot.
"""

from __future__ import annotations

from pathlib import Path

from frisket_models.engines import Engine, Registry
from frisket_models.transcription.contract import (
    TranscribeOptions,
    TranscribeResult,
)

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def stub_registry() -> Registry:
    """Engines with the real names/routes but deterministic in-process
    adapters — the contract under test is the HTTP surface, not the models.
    ``modules=[]`` makes every stub 'installed'; the broken/missing pair
    exercises the 503 + capabilities-error paths."""

    def ocr_pages(images: list[bytes]) -> list[dict]:
        return [
            {
                "text": f"page {i + 1} text",
                "blocks": [
                    {
                        "text": f"page {i + 1} text",
                        "bbox": [[0, 0], [10, 0], [10, 10], [0, 10]],
                        "score": 0.99,
                    }
                ],
            }
            for i in range(len(images))
        ]

    def to_markdown(name: str, data: bytes) -> dict:
        return {"markdown": f"# {name}\n\n{len(data)} bytes", "ocr_used": [False, True]}

    def transcribe(path: Path, options: TranscribeOptions) -> TranscribeResult:
        assert path.read_bytes()
        return TranscribeResult(
            engine="whisper-turbo",
            text="hello world",
            segments=[{"start": 0.0, "end": 1.5, "text": "hello world"}],
            language=options.language or "en",
            duration=1.5,
            model_ids=["dropbox-dash/faster-whisper-large-v3-turbo"],
            revision="stub-revision",
            device="cpu",
            dtype="int8",
            timings={"adapter.inference_seconds": 0.01},
            warnings=[],
            accepted_options=options.supplied_options(),
        )

    def extract(texts: list[str], labels: list[str], threshold: float) -> list:
        return [
            [{"text": t[:4], "label": labels[0], "start": 0, "end": 4, "score": 0.9}]
            for t in texts
        ]

    def rerank(query: str, documents: list[str]) -> list[float]:
        # longer document = more relevant, deterministically
        return [float(len(d)) for d in documents]

    def embed(texts: list[str]) -> list[list[float]]:
        return [[0.1, 0.2, 0.3] for _ in texts]

    def broken_loader():
        raise ImportError("torch exploded (stub)")

    return Registry(
        [
            Engine(
                "dots.mocr",
                "/ocr",
                [],
                lambda: ocr_pages,
                models=["dots-studio/dots.mocr"],
            ),
            Engine(
                "glm-ocr",
                "/ocr",
                [],
                lambda: ocr_pages,
                models=["zai-org/GLM-OCR"],
            ),
            Engine(
                "docling", "/to-markdown", [], lambda: to_markdown, models=["docling"]
            ),
            Engine("gliner", "/ner", [], lambda: extract, models=["stub-gliner"]),
            Engine(
                "whisper-turbo",
                "/v1/transcribe",
                [],
                lambda: transcribe,
                models=["dropbox-dash/faster-whisper-large-v3-turbo"],
            ),
            Engine(
                "cross-encoder", "/rerank", [], lambda: rerank, models=["stub-rerank"]
            ),
            Engine(
                "fastembed", "/v1/embeddings", [], lambda: embed, models=["stub-embed"]
            ),
            # an engine whose loader blows up at first use (→ 503, error
            # surfaced in /capabilities)
            Engine("dots.mocr-broken", "/ocr", [], broken_loader),
            # an engine whose extra simply isn't installed
            Engine(
                "docling-missing",
                "/to-markdown",
                ["frisket_models_not_a_real_module"],
                lambda: to_markdown,
            ),
        ]
    )
