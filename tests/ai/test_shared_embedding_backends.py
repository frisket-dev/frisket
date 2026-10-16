from __future__ import annotations

import sys
from types import ModuleType

import pytest

from frisket.ai.embeddings import backends


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
            raise ValueError("model is not installed")

    fastembed = ModuleType("fastembed")
    fastembed.TextEmbedding = MissingModel  # type: ignore[attr-defined]
    fastembed.ImageEmbedding = MissingModel  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "fastembed", fastembed)
    monkeypatch.setattr(backends, "_local_models", {})
    monkeypatch.setattr(backends, "_local_image_models", {})

    resolved = resolve()

    assert resolved is not None
    encode, _model_id = resolved
    with pytest.raises(ValueError, match="not installed"):
        encode(["input"])
    assert constructed == [
        (
            backends.LOCAL_MODEL
            if factory_name == "TextEmbedding"
            else backends.LOCAL_IMAGE_MODEL,
            {"local_files_only": True},
        )
    ]
