"""Gateway metadata: metadata-rich embedding results + capability discovery
(Lane 1).

Two deliverables under test:

1. ``EmbeddingBatchResult`` — embeddings stop being a bare ``list[list[float]]``.
   The actual model id, provider id/kind, dimension, dtype, metric,
   normalization and usage survive end-to-end for BOTH a local provider and a
   remote provider. ``ModelRouter.embed`` keeps its vector-only contract so
   existing semantic callers are untouched.
2. ``embedding_capabilities`` — the picker contract. ALL modalities are
   represented at the shape level (not text-only); unavailable engines surface
   as disabled options with reasons (missing key / not-yet-built), never hidden.
"""

from __future__ import annotations

import sys
from types import ModuleType

import httpx
import pytest

import frisket.ai.embeddings.capabilities as capabilities_module
import frisket.semantic as semantic_module

from frisket.ai.embeddings import (
    ALL_MODALITIES,
    EmbeddingBatchResult,
    EmbeddingGateway,
    build_batch_result,
    embedding_capabilities,
    resolve_embedding_capability,
    space_descriptor_from_result,
    space_id_for,
)
from frisket.ai.llm.adapters import OpenAICompatAdapter
from frisket.ai.llm.endpoint_config import LocalModelEndpointConfig
from frisket.ai.llm.router import ModelRouter
from frisket.semantic import local_embedder


# --------------------------------------------------------------------------
# EmbeddingBatchResult — local provider path
# --------------------------------------------------------------------------


def test_build_batch_result_preserves_local_metadata():
    vectors = [[0.1, 0.2, 0.3, 0.4], [0.5, 0.6, 0.7, 0.8]]
    result: EmbeddingBatchResult = build_batch_result(
        vectors,
        provider_id="fastembed",
        provider_kind="local_process",
        requested_model="local",
        actual_model_id="fastembed/bge-small-en-v1.5",
        modality="text",
        normalization="l2",
        usage={"input_count": 2},
    )
    assert result["vectors"] == vectors
    assert result["provider_id"] == "fastembed"
    assert result["provider_kind"] == "local_process"
    assert result["requested_model"] == "local"
    assert result["actual_model_id"] == "fastembed/bge-small-en-v1.5"
    assert result["dimension"] == 4
    assert result["dtype"] == "float32"
    assert result["distance_metric"] == "cosine"
    assert result["normalization"] == "l2"
    assert result["modality"] == "text"
    assert result["usage"] == {"input_count": 2}


def test_batch_result_mints_a_consistent_space_descriptor():
    result = build_batch_result(
        [[1.0, 0.0]],
        provider_id="fastembed",
        provider_kind="local_process",
        requested_model="local",
        actual_model_id="fastembed/bge-small-en-v1.5",
        modality="text",
    )
    desc = space_descriptor_from_result(result, vector_options={"truncate": 256})
    assert desc["actual_model_id"] == "fastembed/bge-small-en-v1.5"
    assert desc["dimension"] == 2
    # the descriptor is well-formed enough to mint a space id
    assert space_id_for(desc).startswith("emb_")


def test_fastembed_text_producer_mints_known_free_cost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = "BAAI/bge-small-en-v1.5"

    def local_embedder(requested_model=None, *, env=None):
        del env
        actual_model = requested_model or model
        return (
            lambda texts: [[1.0, 0.0] for _ in texts],
            actual_model,
        )

    monkeypatch.setattr("frisket.semantic.local_embedder", local_embedder)
    result = EmbeddingGateway().embed(
        ["one", "two"], provider="fastembed", model=model, modality="text"
    )

    assert result["provider_id"] == "fastembed"
    assert result["provider_kind"] == "local_process"
    assert result["actual_model_id"] == model
    assert result["credential_source"] == "local"
    assert result["provider_reported_cost_usd"] is None
    assert result["provider_cost_usd"] == 0.0
    assert result["cost_source"] == "free_local"


def test_providerless_classify_gateway_uses_the_typed_local_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str | None, str | None]] = []

    def local_embedder(requested_model=None, *, env=None, capability=None):
        del env
        calls.append((requested_model, capability))
        return (
            lambda texts: [[1.0, 0.0] for _ in texts],
            f"fastembed/{requested_model}",
        )

    monkeypatch.setattr("frisket.semantic.local_embedder", local_embedder)
    result = EmbeddingGateway().embed(
        ["one"],
        provider="fastembed",
        model=semantic_module.PROVIDERLESS_CLASSIFY_MODEL,
        modality="text",
        local_capability="providerless_classify",
    )

    assert calls == [
        (
            semantic_module.PROVIDERLESS_CLASSIFY_MODEL,
            semantic_module.PROVIDERLESS_CLASSIFY_CAPABILITY,
        )
    ]
    assert result["provider_cost_usd"] == 0.0
    assert result["cost_source"] == "free_local"


@pytest.mark.parametrize(
    ("provider", "modality"),
    [("openai", "text"), ("fastembed", "image")],
)
def test_providerless_classify_gateway_capability_rejects_other_consumers(
    provider: str, modality: str
) -> None:
    with pytest.raises(
        ValueError,
        match="providerless_classify is valid only for local text embeddings",
    ):
        EmbeddingGateway().embed(
            ["one"],
            provider=provider,
            model=semantic_module.PROVIDERLESS_CLASSIFY_MODEL,
            modality=modality,
            local_capability="providerless_classify",
        )


def test_providerless_classify_uses_only_the_pinned_offline_snapshot(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    constructed: list[tuple[str, dict[str, object]]] = []

    class _Vector(list[float]):
        def tolist(self) -> list[float]:
            return list(self)

    class _TextEmbedding:
        def __init__(self, model: str, **kwargs: object) -> None:
            constructed.append((model, kwargs))

        def embed(self, texts: list[str]) -> list[_Vector]:
            return [_Vector([1.0, 0.0]) for _ in texts]

    installed_fastembed = ModuleType("fastembed")
    installed_fastembed.TextEmbedding = _TextEmbedding  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "fastembed", installed_fastembed)
    monkeypatch.setattr(semantic_module, "_local_models", {})
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path))
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    cloud_env = {
        "FRISKET_DISABLE_LOCAL_EMBED": "1",
        semantic_module.PROVIDERLESS_CLASSIFY_ENABLE_ENV: "1",
        semantic_module.PROVIDERLESS_CLASSIFY_THREADS_ENV: "2",
    }

    assert (
        local_embedder(
            semantic_module.PROVIDERLESS_CLASSIFY_MODEL,
            env={
                semantic_module.PROVIDERLESS_CLASSIFY_ENABLE_ENV: "1",
                semantic_module.PROVIDERLESS_CLASSIFY_THREADS_ENV: "invalid-but-irrelevant",
            },
        )
        is not None
    ), "classify-only configuration must not change the generic enabled path"
    assert (
        local_embedder(semantic_module.PROVIDERLESS_CLASSIFY_MODEL, env=cloud_env)
        is None
    )
    resolved = local_embedder(
        semantic_module.PROVIDERLESS_CLASSIFY_MODEL,
        env=cloud_env,
        capability="providerless_classify",
    )

    assert resolved is not None
    assert constructed == []
    embed, model_id = resolved
    from frisket.ai.models import artifact_manifest

    entry = artifact_manifest.providerless_classify_artifact()
    assert entry is not None
    source = entry.hf_snapshot
    assert source is not None
    repo_cache = tmp_path / f"models--{source.repo_id.replace('/', '--')}" / "snapshots"
    wrong_snapshot = repo_cache / ("f" * 40)
    for name in source.files:
        path = wrong_snapshot / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("wrong revision", encoding="utf-8")
    with pytest.raises(RuntimeError, match=source.revision):
        embed(["one"])

    snapshot = repo_cache / source.revision
    for name in source.files:
        path = snapshot / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("pinned", encoding="utf-8")
    assert embed(["one"]) == [[1.0, 0.0]]
    assert model_id == f"fastembed/{semantic_module.PROVIDERLESS_CLASSIFY_MODEL}"
    assert constructed == [
        (
            semantic_module.PROVIDERLESS_CLASSIFY_MODEL,
            {
                "threads": 2,
                "specific_model_path": str(snapshot),
                "local_files_only": True,
            },
        )
    ]


@pytest.mark.parametrize("value", ["0", "9", "many"])
def test_providerless_classify_thread_bound_refuses_invalid_values(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setattr(semantic_module, "_local_models", {})
    with pytest.raises(
        ValueError,
        match=semantic_module.PROVIDERLESS_CLASSIFY_THREADS_ENV,
    ):
        local_embedder(
            semantic_module.PROVIDERLESS_CLASSIFY_MODEL,
            env={
                "FRISKET_DISABLE_LOCAL_EMBED": "1",
                semantic_module.PROVIDERLESS_CLASSIFY_ENABLE_ENV: "1",
                semantic_module.PROVIDERLESS_CLASSIFY_THREADS_ENV: value,
            },
            capability="providerless_classify",
        )
    assert semantic_module._local_models == {}


def test_providerless_classify_capability_refuses_any_other_model() -> None:
    with pytest.raises(
        ValueError,
        match=(
            "providerless_classify requires "
            + semantic_module.PROVIDERLESS_CLASSIFY_MODEL
        ),
    ):
        local_embedder(
            "BAAI/bge-base-en-v1.5",
            env={
                "FRISKET_DISABLE_LOCAL_EMBED": "1",
                semantic_module.PROVIDERLESS_CLASSIFY_ENABLE_ENV: "1",
            },
            capability="providerless_classify",
        )


def test_fastembed_image_producer_mints_known_free_cost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = "Qdrant/clip-ViT-B-32-vision"

    def local_image_embedder(requested_model=None, *, env=None):
        del env
        actual_model = requested_model or model
        return (
            lambda images: [[1.0, 0.0, 0.0] for _ in images],
            actual_model,
        )

    monkeypatch.setattr("frisket.semantic.local_image_embedder", local_image_embedder)
    result = EmbeddingGateway().embed(
        ["/tmp/image.png"],
        provider="fastembed",
        model=model,
        modality="image",
    )

    assert result["provider_id"] == "fastembed"
    assert result["provider_kind"] == "local_process"
    assert result["actual_model_id"] == model
    assert result["credential_source"] == "local"
    assert result["provider_reported_cost_usd"] is None
    assert result["provider_cost_usd"] == 0.0
    assert result["cost_source"] == "free_local"


# --------------------------------------------------------------------------
# EmbeddingBatchResult — remote provider path (no real network)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remote_adapter_embed_with_meta_preserves_actual_model():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "object": "list",
                "model": "text-embedding-3-small-actual",
                "data": [
                    {"object": "embedding", "index": 0, "embedding": [0.1, 0.2, 0.3]},
                    {"object": "embedding", "index": 1, "embedding": [0.4, 0.5, 0.6]},
                ],
                "usage": {"prompt_tokens": 7, "total_tokens": 7},
            },
            headers={"x-request-id": "req-123"},
        )

    adapter = OpenAICompatAdapter("sk-test", "https://api.openai.com/v1")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        vectors, meta = await adapter.embed_with_meta(
            ["a", "b"], "text-embedding-3-small", client
        )
    assert vectors == [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]
    # the provider-reported (actual) model id is preserved, not just the request
    assert meta["actual_model_id"] == "text-embedding-3-small-actual"
    assert meta["dimension"] == 3
    assert meta["usage"]["prompt_tokens"] == 7
    assert meta.get("provider_request_id") == "req-123"


@pytest.mark.asyncio
async def test_router_embed_batch_returns_metadata_rich_result():
    class FakeAdapter:
        base_url = "https://example/v1"

        async def embed_with_meta(self, texts, model, client):
            return (
                [[1.0, 0.0]] * len(texts),
                {
                    "actual_model_id": f"{model}-resolved",
                    "dimension": 2,
                    "usage": {"prompt_tokens": 3},
                    "provider_request_id": "rid",
                },
            )

    router = ModelRouter()
    router._adapters["openai"] = FakeAdapter()
    result = await router.embed_batch(["x", "y"], model="openai/text-embedding-3-small")
    assert isinstance(result, dict)
    assert result["provider_id"] == "openai"
    assert result["provider_kind"] == "platform_api"
    assert result["requested_model"] == "openai/text-embedding-3-small"
    assert result["actual_model_id"] == "text-embedding-3-small-resolved"
    assert result["dimension"] == 2
    assert result["vectors"] == [[1.0, 0.0], [1.0, 0.0]]
    assert result["provider_cost_usd"] is None
    assert result["cost_source"] == "unknown"
    await router.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("meta_source", "expected_source"),
    [("pricing_data", "pricing_data"), (None, "provider_reported")],
)
async def test_router_embed_batch_preserves_hosted_cost_source(
    meta_source: str | None, expected_source: str
):
    class FakeAdapter:
        base_url = "https://example/v1"

        async def embed_with_meta(self, texts, model, client):
            meta = {
                "actual_model_id": model,
                "dimension": 2,
                "provider_reported_cost_usd": 0.004,
                "provider_cost_usd": 0.004,
            }
            if meta_source is not None:
                meta["cost_source"] = meta_source
            return [[1.0, 0.0]] * len(texts), meta

    router = ModelRouter()
    router._adapters["openai"] = FakeAdapter()  # noqa: SLF001 - hermetic adapter
    result = await router.embed_batch(["x"], model="openai/text-embedding-3-small")

    assert result["provider_reported_cost_usd"] == 0.004
    assert result["provider_cost_usd"] == 0.004
    assert result["cost_source"] == expected_source
    await router.aclose()


@pytest.mark.asyncio
async def test_canonical_ollama_embedding_mints_known_free_cost():
    class FakeAdapter:
        async def embed_with_meta(self, texts, model, client):
            return (
                [[1.0, 0.0]] * len(texts),
                {"actual_model_id": model, "dimension": 2},
            )

    endpoint = LocalModelEndpointConfig(
        endpoint_id="desk",
        display_name="Desk",
        origin="http://127.0.0.1:11434",
        source="local_file",
    )
    router = ModelRouter(use_env_keys=False, local_endpoints=(endpoint,))
    local_adapter = router._adapters["ollama"]  # noqa: SLF001 - hermetic adapter
    local_adapter._adapters["desk"] = FakeAdapter()  # noqa: SLF001

    model = "ollama/@desk/nomic-embed-text"
    result = await router.embed_batch(["x"], model=model)

    assert result["provider_id"] == "ollama"
    assert result["provider_kind"] == "local_http"
    assert result["requested_model"] == model
    assert result["actual_model_id"] == model
    assert result["credential_source"] == "local"
    assert result["provider_reported_cost_usd"] is None
    assert result["provider_cost_usd"] == 0.0
    assert result["cost_source"] == "free_local"
    await router.aclose()


@pytest.mark.asyncio
async def test_router_embed_stays_vector_only_for_legacy_callers():
    class FakeAdapter:
        base_url = "https://example/v1"

        async def embed(self, texts, model, client):
            return [[0.0, 1.0]] * len(texts)

        async def embed_with_meta(self, texts, model, client):
            return (
                [[0.0, 1.0]] * len(texts),
                {"actual_model_id": model, "dimension": 2},
            )

    router = ModelRouter()
    router._adapters["openai"] = FakeAdapter()
    vecs = await router.embed(["x"], model="openai/text-embedding-3-small")
    assert vecs == [[0.0, 1.0]]  # bare vectors, semantic.py contract intact
    await router.aclose()


# --------------------------------------------------------------------------
# Capability discovery
# --------------------------------------------------------------------------


def test_capabilities_cover_all_modalities_at_shape_level():
    caps = embedding_capabilities(router=ModelRouter(), local_available=True)
    covered = set().union(*(c["modalities"] for c in caps))
    # not text-only: the contract represents every modality from the start
    assert {"text", "image", "audio", "video", "file", "row"} <= covered
    assert {"text", "image", "audio", "video", "file", "row"} <= set(ALL_MODALITIES)


def test_capability_entries_have_full_shape():
    caps = embedding_capabilities(router=ModelRouter(), local_available=True)
    required = {
        "provider_id",
        "provider_kind",
        "model_id",
        "label",
        "modalities",
        "dimensions",
        "distance_metrics",
        "local",
        "available",
        "error",
        "pricing",
        "privacy",
    }
    for c in caps:
        assert required <= set(c), f"missing keys in {c.get('model_id')}"


def test_local_text_available_with_base_runtime():
    avail = embedding_capabilities(router=ModelRouter(), local_available=True)
    unavail = embedding_capabilities(router=ModelRouter(), local_available=False)
    local_text = [c for c in avail if c["provider_kind"] == "local_process"]
    assert local_text and any(c["available"] for c in local_text)
    # honest degradation: damaged base runtime → disabled with a reason, never hidden
    local_text_off = [c for c in unavail if c["provider_kind"] == "local_process"]
    assert local_text_off and all(not c["available"] for c in local_text_off)
    assert all(c["error"] for c in local_text_off)


def test_disable_flag_hides_installed_text_and_image_but_not_hosted(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.delenv("FRISKET_DISABLE_LOCAL_EMBED", raising=False)
    caps = embedding_capabilities(
        router=ModelRouter(),
        env={
            "FRISKET_DISABLE_LOCAL_EMBED": "1",
            "FRISKET_ENABLE_PROVIDERLESS_CLASSIFY": "1",
            "FRISKET_PROVIDERLESS_CLASSIFY_THREADS": "2",
            "OPENAI_API_KEY": "injected-test-key",
        },
    )

    local = [c for c in caps if c["provider_id"] == "fastembed"]
    assert any("text" in c["modalities"] for c in local)
    assert any("image" in c["modalities"] for c in local)
    assert all(not c["available"] for c in local)

    hosted = [c for c in caps if c["provider_id"] == "openai"]
    assert hosted and all(c["available"] for c in hosted)


def test_explicit_env_mapping_does_not_fall_back_to_process_disable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FRISKET_DISABLE_LOCAL_EMBED", "1")
    installed_fastembed = ModuleType("fastembed")
    installed_fastembed.TextEmbedding = object  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "fastembed", installed_fastembed)
    assert local_embedder(env={}) is not None


def test_disabled_custom_fastembed_resolution_never_reads_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def registry_tripwire():
        raise AssertionError("disabled custom model imported the FastEmbed registry")

    monkeypatch.setattr(
        capabilities_module, "_fastembed_registry", registry_tripwire, raising=True
    )
    cap = resolve_embedding_capability(
        modality="text",
        provider="fastembed",
        model="BAAI/bge-base-en-v1.5",
        env={"FRISKET_DISABLE_LOCAL_EMBED": "1"},
    )
    assert cap is None


def test_remote_capability_disabled_without_key_enabled_with_key():
    no_key = embedding_capabilities(router=ModelRouter(keys={}), local_available=False)
    openai_off = [c for c in no_key if c["provider_id"] == "openai"]
    assert openai_off and all(not c["available"] for c in openai_off)
    assert all(c["error"] for c in openai_off)

    with_key = embedding_capabilities(
        router=ModelRouter(keys={"openai": "sk-test"}), local_available=False
    )
    openai_on = [c for c in with_key if c["provider_id"] == "openai"]
    assert openai_on and any(c["available"] for c in openai_on)
    # remote engines are flagged non-local with a remote-egress privacy note
    assert all(not c["local"] for c in openai_on)
    assert all(c["privacy"].get("egress") == "remote" for c in openai_on)


def test_unbuilt_sidecar_media_engines_are_disabled_with_reasons():
    caps = embedding_capabilities(router=ModelRouter(), local_available=True)
    media = [
        c
        for c in caps
        if c["provider_id"] == "frisket-models"
        and set(c["modalities"]) & {"image", "audio", "video", "file"}
    ]
    assert media, "unbuilt sidecar media modalities must be represented"
    for c in media:
        assert c["available"] is False
        assert c["error"]
