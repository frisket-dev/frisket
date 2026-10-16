from __future__ import annotations

import sys
from types import ModuleType

import pytest

from frisket.ai.embeddings import backends
from frisket.ai.embeddings.gateway import (
    EmbeddingBackendUnavailable,
    EmbeddingGateway,
    EmbeddingProviderError,
)


def test_backend_resolution_requires_explicit_remote_opt_in(monkeypatch):
    monkeypatch.setattr(backends, "local_embedder", lambda: None)

    class Router:
        def has_embedding_backend(self):
            return True

    assert backends.resolve_embedder(Router(), allow_remote=False) is None
    resolved = backends.resolve_embedder(Router(), allow_remote=True)
    assert resolved is not None
    assert resolved[1] == backends.REMOTE_EMBED_MODEL


def test_local_backends_honor_disable_switch_without_loading_models():
    disabled = {"FRISKET_DISABLE_LOCAL_EMBED": "1"}
    assert backends.local_embedder(env=disabled) is None
    assert backends.local_image_embedder(env=disabled) is None
    assert backends.local_reranker(env=disabled) is None


@pytest.mark.parametrize(
    ("factory_name", "resolve"),
    [
        ("TextEmbedding", lambda: backends.local_embedder(env={})),
        ("ImageEmbedding", lambda: backends.local_image_embedder(env={})),
    ],
)
def test_local_backends_refuse_an_absent_model_without_network(
    monkeypatch, factory_name, resolve
):
    constructed = []

    class MissingModel:
        def __init__(self, model, **kwargs):
            constructed.append((model, kwargs))
            raise ValueError(f"Could not load model {model} from any source.")

    fastembed = ModuleType("fastembed")
    fastembed.TextEmbedding = MissingModel  # type: ignore[attr-defined]
    fastembed.ImageEmbedding = MissingModel  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "fastembed", fastembed)
    monkeypatch.setattr(backends, "_local_models", {})
    monkeypatch.setattr(backends, "_local_image_models", {})

    resolved = resolve()

    assert resolved is not None
    encode, _model_id = resolved
    with pytest.raises(
        backends.LocalEmbeddingModelUnavailable,
        match="FASTEMBED_CACHE_PATH",
    ):
        encode(["input"])
    assert constructed == [
        (
            backends.LOCAL_MODEL
            if factory_name == "TextEmbedding"
            else backends.LOCAL_IMAGE_MODEL,
            {"local_files_only": True},
        )
    ]


def test_gateway_reports_an_uncached_local_model_as_unavailable(monkeypatch):
    class MissingModel:
        def __init__(self, model, **_kwargs):
            raise ValueError(f"Could not load model {model} from any source.")

    fastembed = ModuleType("fastembed")
    fastembed.TextEmbedding = MissingModel  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "fastembed", fastembed)
    monkeypatch.setattr(backends, "_local_models", {})

    with pytest.raises(
        EmbeddingBackendUnavailable,
        match="frisket doctor embeddings",
    ):
        EmbeddingGateway().embed(
            ["input"], provider="fastembed", model=None, modality="text"
        )


def test_gateway_preserves_other_local_model_failures_as_provider_errors(monkeypatch):
    class BrokenModel:
        def __init__(self, _model, **_kwargs):
            raise ValueError("unsupported model configuration")

    fastembed = ModuleType("fastembed")
    fastembed.TextEmbedding = BrokenModel  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "fastembed", fastembed)
    monkeypatch.setattr(backends, "_local_models", {})

    with pytest.raises(EmbeddingProviderError, match="unsupported model configuration"):
        EmbeddingGateway().embed(
            ["input"], provider="fastembed", model=None, modality="text"
        )
