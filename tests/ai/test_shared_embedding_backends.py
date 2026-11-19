from __future__ import annotations

import sys
from importlib.machinery import ModuleSpec
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


def test_local_reranker_refuses_an_absent_model_without_network(monkeypatch):
    constructed = []

    class MissingModel:
        def __init__(self, model, **kwargs):
            constructed.append((model, kwargs))
            raise ValueError(f"Could not load model {model} from any source.")

    cross_encoder = ModuleType("fastembed.rerank.cross_encoder")
    monkeypatch.setattr(cross_encoder, "TextCrossEncoder", MissingModel, raising=False)
    monkeypatch.setitem(sys.modules, "fastembed.rerank.cross_encoder", cross_encoder)
    monkeypatch.setattr(backends, "_local_rerank_model", None)

    score = backends.local_reranker(env={})

    assert score is not None
    with pytest.raises(
        backends.LocalEmbeddingModelUnavailable,
        match=r"reranking model .*FASTEMBED_CACHE_PATH",
    ):
        score("query", ["document"])
    assert constructed == [(backends.RERANK_MODEL, {"local_files_only": True})]


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


@pytest.mark.parametrize("error_type", [TypeError, RuntimeError])
def test_remote_gateway_does_not_disguise_dispatch_wiring_errors(error_type):
    error = error_type("remote failure")

    class Router:
        async def embed_batch(self, texts, *, model, modality):
            raise error

    expected = TypeError if error_type is TypeError else EmbeddingProviderError
    with pytest.raises(expected, match="remote failure") as caught:
        EmbeddingGateway(router=Router()).embed(
            ["input"],
            provider="openai",
            model="text-embedding-3-small",
            modality="text",
        )
    if error_type is TypeError:
        assert caught.value is error


def test_remote_gateway_preserves_missing_execution_keyword_type_error():
    class Router:
        async def embed_batch(self, texts, *, model, modality, _execution):
            raise AssertionError("unowned dispatch must fail before transport")

    with pytest.raises(TypeError, match="_execution"):
        EmbeddingGateway(router=Router()).embed(
            ["input"],
            provider="openai",
            model="text-embedding-3-small",
            modality="text",
        )


def _fastembed_installed(monkeypatch) -> None:
    fastembed = ModuleType("fastembed")
    fastembed.__spec__ = ModuleSpec("fastembed", None)
    monkeypatch.setitem(sys.modules, "fastembed", fastembed)


def test_reranker_readiness_names_the_model_until_it_is_cached(monkeypatch, tmp_path):
    _fastembed_installed(monkeypatch)
    env = {"FASTEMBED_CACHE_PATH": str(tmp_path)}

    reason = backends.local_reranker_unavailable_reason(env=env)

    assert reason is not None
    assert backends.RERANK_MODEL in reason
    model = (
        tmp_path
        / f"models--{backends.RERANK_MODEL.replace('/', '--')}"
        / "snapshots"
        / "rev"
        / "onnx"
        / "model.onnx"
    )
    model.parent.mkdir(parents=True)
    model.write_bytes(b"onnx")
    assert backends.local_reranker_unavailable_reason(env=env) is None


def test_reranker_readiness_reports_a_disabled_reranker():
    assert (
        backends.local_reranker_unavailable_reason(env={"FRISKET_DISABLE_RERANK": "1"})
        == "Rerank is turned off on this server."
    )
