"""Gateway metadata: metadata-rich embedding results + capability discovery.

``ModelRouter.embed`` returns bare ``list[list[float]]`` — enough for the legacy
implicit semantic cache, but it discards everything native embeddings need to
build a strict space: the *actual* model id (not the requested alias), provider
id/kind, dimension, dtype, metric, normalization, and usage. ``build_batch_result``
is the provider-neutral shape that preserves those facts end-to-end.

``embedding_capabilities`` is the picker contract. It represents every modality
from the start (not text-only); engines that are not installed, not keyed, or
not yet built surface as ``available=False`` options with an ``error`` reason
rather than being hidden — the same truthful-but-nonfatal discipline the model
capability contract requires (MODEL_CAPABILITY_CONTRACT Invariant 4).
"""

from __future__ import annotations

from typing import Any, TypedDict

from .spaces import ALL_MODALITIES, make_space_descriptor


class EmbeddingCapability(TypedDict):
    provider_id: str
    provider_kind: (
        str  # local_process|local_http|platform_api|platform_http|platform_function
    )
    model_id: str
    label: str
    modalities: list[str]
    dimensions: list[int] | None
    distance_metrics: list[str]
    local: bool
    available: bool
    error: str | None
    pricing: dict[str, Any] | None
    privacy: dict[str, Any]
    # factual picker metadata (NO use-case editorial): on-disk weight size, the
    # model's max input length (texts beyond it get truncated/rejected — a future
    # guard), and whether this is the one sensible default.
    size_gb: float | None
    max_input_tokens: int | None
    recommended: bool
    # True when this engine's dimension is NOT fixed in the registry and must be
    # discovered at create with one probe embed (custom remote / OpenRouter ids).
    # For these, ``dimensions`` is honestly None — never a fabricated width.
    dimension_discovery_required: bool


class EmbeddingBatchResult(TypedDict):
    vectors: list[list[float]]
    provider_id: str
    provider_kind: str
    requested_model: str | None
    actual_model_id: str
    model_revision: str | None
    dimension: int
    dtype: str
    distance_metric: str
    normalization: str
    modality: str
    usage: dict[str, Any]
    provider_request_id: str | None
    credential_source: str
    provider_reported_cost_usd: float | None
    provider_cost_usd: float | None
    cost_source: str
    fact_version: str


def build_batch_result(
    vectors: list[list[float]],
    *,
    provider_id: str,
    provider_kind: str,
    actual_model_id: str,
    requested_model: str | None = None,
    modality: str = "text",
    model_revision: str | None = "unknown",
    dtype: str = "float32",
    distance_metric: str = "cosine",
    normalization: str = "none",
    usage: dict[str, Any] | None = None,
    provider_request_id: str | None = None,
    dimension: int | None = None,
    credential_source: str = "local",
    provider_reported_cost_usd: float | None = None,
    provider_cost_usd: float | None = None,
    cost_source: str = "unknown",
) -> EmbeddingBatchResult:
    """Assemble a metadata-rich embedding result. ``dimension`` is inferred from
    the first vector unless given (so empty batches can still declare one)."""
    if dimension is None:
        dimension = len(vectors[0]) if vectors else 0
    return {
        "vectors": vectors,
        "provider_id": provider_id,
        "provider_kind": provider_kind,
        "requested_model": requested_model,
        "actual_model_id": actual_model_id,
        "model_revision": model_revision,
        "dimension": dimension,
        "dtype": dtype,
        "distance_metric": distance_metric,
        "normalization": normalization,
        "modality": modality,
        "usage": usage or {},
        "provider_request_id": provider_request_id,
        "credential_source": credential_source,
        "provider_reported_cost_usd": provider_reported_cost_usd,
        "provider_cost_usd": provider_cost_usd,
        "cost_source": cost_source,
        "fact_version": "frisket.model-call-fact.v1",
    }


def space_descriptor_from_result(
    result: EmbeddingBatchResult,
    *,
    vector_options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Mint a strict space descriptor from a batch result, carrying the actual
    provider facts forward. ``vector_options`` are the source/extraction knobs
    that also belong in the space identity."""
    return make_space_descriptor(
        provider_id=result["provider_id"],
        provider_kind=result["provider_kind"],
        requested_model=result.get("requested_model"),
        actual_model_id=result["actual_model_id"],
        model_revision=result.get("model_revision") or "unknown",
        modality=result["modality"],
        dimension=result["dimension"],
        dtype=result["dtype"],
        distance_metric=result["distance_metric"],
        normalization=result["normalization"],
        vector_options=vector_options,
    )


# Known embedding engines across modalities. This is the contract-shape registry:
# unavailable real engines are NOT omitted — they are returned disabled with a
# reason so the picker can show "needs API key" / "not yet available" instead of
# guessing. Real availability is resolved per call against the router + env.
_KNOWN_ENGINES: tuple[dict[str, Any], ...] = (
    {
        "provider_id": "fastembed",
        "provider_kind": "local_process",
        "model_id": "paraphrase-multilingual-MiniLM-L12-v2",
        "label": "Local · multilingual (~50 languages)",
        "modalities": ["text", "row"],
        "dimensions": [384],
        "distance_metrics": ["cosine"],
        "pricing": {"policy": "not_billable"},
        "privacy": {"egress": "none", "local": True},
        "size_gb": 0.22,
        "max_input_tokens": 512,
        "recommended": True,
        "_resolve": "local_text",
    },
    {
        "provider_id": "fastembed",
        "provider_kind": "local_process",
        "model_id": "BAAI/bge-small-en-v1.5",
        "label": "Local · English, small & fast",
        "modalities": ["text", "row"],
        "dimensions": [384],
        "distance_metrics": ["cosine"],
        "pricing": {"policy": "not_billable"},
        "privacy": {"egress": "none", "local": True},
        "size_gb": 0.067,
        "max_input_tokens": 512,
        "recommended": False,
        "_resolve": "local_text",
    },
    {
        "provider_id": "fastembed",
        "provider_kind": "local_process",
        "model_id": "BAAI/bge-large-en-v1.5",
        "label": "Local · English, larger / more accurate",
        "modalities": ["text", "row"],
        "dimensions": [1024],
        "distance_metrics": ["cosine"],
        "pricing": {"policy": "not_billable"},
        "privacy": {"egress": "none", "local": True},
        "size_gb": 1.2,
        "max_input_tokens": 512,
        "recommended": False,
        "_resolve": "local_text",
    },
    {
        "provider_id": "openai",
        "provider_kind": "platform_api",
        "model_id": "text-embedding-3-small",
        "label": "OpenAI text-embedding-3-small",
        "modalities": ["text", "row"],
        "dimensions": [1536],
        "distance_metrics": ["cosine"],
        "pricing": {
            "policy": "known_unit_price",
            "input_usd_per_million_tokens": 0.02,
            "source_url": "https://developers.openai.com/api/docs/models/text-embedding-3-small",
            "updated": "2026-08-28",
        },
        "privacy": {"egress": "remote", "local": False},
        "_resolve": "remote_openai",
    },
    {
        "provider_id": "gemini",
        "provider_kind": "platform_api",
        # text-embedding-004 404s on Google's OpenAI-compat endpoint;
        # gemini-embedding-001 is the current model — 3072-d default.
        "model_id": "gemini-embedding-001",
        "label": "Gemini · multilingual (remote)",
        "modalities": ["text", "row"],
        "dimensions": [3072],
        "distance_metrics": ["cosine"],
        "pricing": {
            "policy": "known_unit_price",
            "input_usd_per_million_tokens": 0.15,
            "source_url": "https://ai.google.dev/gemini-api/docs/pricing",
            "updated": "2026-08-28",
        },
        "privacy": {"egress": "remote", "local": False},
        "max_input_tokens": 2048,
        "_resolve": "remote_gemini",
    },
    {
        # OpenRouter (and any OpenAI-compat remote) is a BRING-YOUR-OWN-MODEL-ID
        # path: there is no single curated model, so this catalog row is a
        # placeholder the picker shows so a user can type a remote model id. Its
        # dimension is discovered at create with one probe embed (the adapter
        # reports it), never fixed here — dimensions stays None.
        "provider_id": "openrouter",
        "provider_kind": "platform_api",
        "model_id": "",  # user supplies a custom id (e.g. openai/text-embedding-3-large)
        "label": "OpenRouter · custom remote model (dimension discovered at create)",
        "modalities": ["text", "row"],
        "dimensions": None,
        "distance_metrics": ["cosine"],
        "pricing": {"policy": "unknown_unit_price"},
        "privacy": {"egress": "remote", "local": False},
        "dimension_discovery_required": True,
        "_resolve": "remote_openrouter",
    },
    # REAL in-process image embedding: fastembed's ImageEmbedding is light ONNX
    # (onnxruntime, NO torch — same engine class as the local text model), so the
    # CLIP vision tower runs on the box. Availability is detected like local text
    # (fastembed importable, here also ImageEmbedding + PIL). 512-d, ~0.34GB.
    {
        "provider_id": "fastembed",
        "provider_kind": "local_process",
        "model_id": "Qdrant/clip-ViT-B-32-vision",
        "label": "Local · CLIP image (ViT-B/32 vision, in-process)",
        # image only: this is the CLIP VISION tower. Cross-modal text->image query
        # (the CLIP text tower) is a separate engine, not implemented here.
        "modalities": ["image"],
        "dimensions": [512],
        "distance_metrics": ["cosine"],
        "pricing": {"policy": "not_billable"},
        "privacy": {"egress": "none", "local": True},
        "size_gb": 0.34,
        "recommended": False,
        "_resolve": "local_image",
    },
    # Heavier media engines belong on the local_http sidecar / remote GPU. The
    # contract carries them now; the engines themselves are a later slice, so
    # they advertise disabled with a clear reason. The sidecar CLIP entry stays
    # disabled-with-reason — the in-process fastembed image engine above is the
    # real path; this row keeps the multimodal/sidecar seam honest.
    {
        "provider_id": "frisket-models",
        "provider_kind": "local_http",
        "model_id": "open_clip/ViT-B-32/laion2b_s34b_b79k",
        "label": "CLIP image/text (sidecar)",
        "modalities": ["image", "multimodal"],
        "dimensions": [512],
        "distance_metrics": ["cosine"],
        "pricing": {"policy": "not_billable"},
        "privacy": {"egress": "none", "local": True},
        "_resolve": "unbuilt",
    },
    {
        "provider_id": "frisket-models",
        "provider_kind": "local_http",
        "model_id": "audio-embedding",
        "label": "Acoustic audio embedding (sidecar)",
        "modalities": ["audio"],
        "dimensions": None,
        "distance_metrics": ["cosine"],
        "pricing": {"policy": "not_billable"},
        "privacy": {"egress": "none", "local": True},
        "_resolve": "unbuilt",
    },
    {
        "provider_id": "frisket-models",
        "provider_kind": "local_http",
        "model_id": "video-embedding",
        "label": "Video frame/segment embedding (sidecar)",
        "modalities": ["video"],
        "dimensions": None,
        "distance_metrics": ["cosine"],
        "pricing": {"policy": "not_billable"},
        "privacy": {"egress": "none", "local": True},
        "_resolve": "unbuilt",
    },
    {
        "provider_id": "frisket-models",
        "provider_kind": "local_http",
        "model_id": "file-page-embedding",
        "label": "Document page/chunk embedding (sidecar)",
        "modalities": ["file"],
        "dimensions": None,
        "distance_metrics": ["cosine"],
        "pricing": {"policy": "not_billable"},
        "privacy": {"egress": "none", "local": True},
        "_resolve": "unbuilt",
    },
)

_UNBUILT_REASON = "engine not yet available in this build (modality contract reserved)"


def _host_ram_gb() -> float | None:
    """Total host RAM in GB, best-effort (posix sysconf); None when undetectable."""
    try:
        import os

        return (os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")) / 1e9
    except (ValueError, OSError, AttributeError):
        return None


def embedding_model_size_budget_gb(env: dict[str, str] | None = None) -> float:
    """Max on-disk model size allowed BEFORE download: FRISKET_EMBEDDING_MAX_MODEL_GB
    (default 16), capped at ~60% of detected host RAM as a backstop. The size guard
    refuses a model larger than this so a refresh can't blow up the box."""
    import os

    env = env if env is not None else dict(os.environ)
    try:
        cap = float(env.get("FRISKET_EMBEDDING_MAX_MODEL_GB", "16"))
    except (TypeError, ValueError):
        cap = 16.0
    ram = _host_ram_gb()
    return min(cap, 0.6 * ram) if ram else cap


def _detect_local_text(env: dict[str, str]) -> bool:
    """The in-process text engine is usable under the runtime's availability rule.

    Delegating to ``semantic.local_embedder`` keeps package detection and the
    ``FRISKET_DISABLE_LOCAL_EMBED`` escape hatch single-sourced. The returned
    embedder is lazy, so capability discovery never loads or downloads a model.
    """
    from frisket.semantic import local_embedder

    return local_embedder(env=env) is not None


def _detect_local_image(env: dict[str, str]) -> bool:
    """The in-process CLIP image engine is usable: fastembed's ``ImageEmbedding``
    AND PIL importable, and FRISKET_DISABLE_LOCAL_EMBED not set. Delegates to
    ``semantic.local_image_embedder`` so the availability rule is single-sourced
    (no model load — the embedder is lazy)."""
    from frisket.semantic import local_image_embedder

    return local_image_embedder(env=env) is not None


def embedding_capabilities(
    *,
    router: Any = None,
    local_available: bool | None = None,
    env: dict[str, str] | None = None,
    include_unbuilt: bool = True,
) -> list[EmbeddingCapability]:
    """Resolve the known engine registry into concrete capability options.

    ``router`` provides the configured remote providers (an adapter exists only
    when a key was supplied). ``local_available`` overrides fastembed detection
    (tests pin it; production auto-detects). ``env`` is the complete environment
    view for both local-disable and remote-key decisions; an explicitly supplied
    mapping never falls back to process globals. ``include_unbuilt`` preserves
    reserved capability descriptors for internal validation while allowing a
    user-facing catalog to omit engines that cannot run yet.
    """
    import os

    env = env if env is not None else dict(os.environ)
    providers = set(router.providers()) if router is not None else set()
    if local_available is None:
        local_available = _detect_local_text(env)

    def resolve(kind: str) -> tuple[bool, str | None]:
        if kind == "local_text":
            return (
                (True, None)
                if local_available
                else (False, "local FastEmbed runtime unavailable; reinstall Frisket")
            )
        if kind == "local_image":
            if local_available is False:
                return (
                    False,
                    "local FastEmbed image runtime unavailable; reinstall Frisket",
                )
            return (
                (True, None)
                if _detect_local_image(env)
                else (
                    False,
                    "local FastEmbed image runtime unavailable; reinstall Frisket",
                )
            )
        if kind == "remote_openai":
            ok = "openai" in providers or bool(env.get("OPENAI_API_KEY"))
            return (True, None) if ok else (False, "missing OPENAI_API_KEY")
        if kind == "remote_gemini":
            ok = "gemini" in providers or bool(env.get("GEMINI_API_KEY"))
            return (True, None) if ok else (False, "missing GEMINI_API_KEY")
        if kind == "remote_openrouter":
            ok = "openrouter" in providers or bool(env.get("OPENROUTER_API_KEY"))
            return (True, None) if ok else (False, "missing OPENROUTER_API_KEY")
        return (False, _UNBUILT_REASON)

    out: list[EmbeddingCapability] = []
    for engine in _KNOWN_ENGINES:
        if not include_unbuilt and engine["_resolve"] == "unbuilt":
            continue
        available, error = resolve(engine["_resolve"])
        out.append(
            EmbeddingCapability(
                provider_id=engine["provider_id"],
                provider_kind=engine["provider_kind"],
                model_id=engine["model_id"],
                label=engine["label"],
                modalities=list(engine["modalities"]),
                dimensions=engine["dimensions"],
                distance_metrics=list(engine["distance_metrics"]),
                local=bool(engine["privacy"].get("local")),
                available=available,
                error=error,
                pricing=engine["pricing"],
                privacy=dict(engine["privacy"]),
                size_gb=engine.get("size_gb"),
                max_input_tokens=engine.get("max_input_tokens"),
                recommended=bool(engine.get("recommended")),
                dimension_discovery_required=bool(
                    engine.get("dimension_discovery_required")
                ),
            )
        )
    return out


# Providers that fall back to the fastembed registry for an uncurated model id.
_FASTEMBED_PROVIDERS = frozenset({"fastembed", "local"})


def _fastembed_registry() -> dict[str, dict[str, Any]] | None:
    """fastembed's supported-model registry keyed by HF model id, or None when the
    bundled runtime is unavailable (so the custom-id fallback simply yields no
    match rather than raising)."""
    try:
        from fastembed import TextEmbedding
    except ImportError:
        return None
    return {m["model"]: m for m in TextEmbedding.list_supported_models()}


def _synthesize_fastembed_capability(
    model: str,
    *,
    modality: str,
    local_available: bool | None,
    env: dict[str, str],
) -> EmbeddingCapability | None:
    """Resolve a fastembed-supported model id that is NOT in _KNOWN_ENGINES into a
    capability synthesized from the fastembed registry (dim + on-disk size are
    FACTUAL, read from fastembed itself — never fabricated). Returns None when
    fastembed is disabled/unavailable or the id is not in its registry. The
    disable decision happens before importing that registry. The registry has no
    context-length field, so max_input_tokens is honestly None."""
    available = _detect_local_text(env) if local_available is None else local_available
    if not available:
        return None
    registry = _fastembed_registry()
    if registry is None:
        return None
    ref = registry.get(model)
    if ref is None:
        return None
    dim = ref.get("dim")
    if not dim:
        return None
    try:
        size_gb: float | None = float(ref["size_in_GB"])
    except (KeyError, TypeError, ValueError):
        size_gb = None
    return EmbeddingCapability(
        provider_id="fastembed",
        provider_kind="local_process",
        model_id=model,
        label=f"Local · {model} (custom)",
        modalities=["text", "row"],
        dimensions=[int(dim)],
        distance_metrics=["cosine"],
        local=True,
        available=available,
        error=None
        if available
        else "local FastEmbed runtime unavailable; reinstall Frisket",
        pricing={"policy": "not_billable"},
        privacy={"egress": "none", "local": True},
        size_gb=size_gb,
        max_input_tokens=None,
        recommended=False,
        dimension_discovery_required=False,
    )


# Remote providers that accept an arbitrary OpenAI-compat model id (the router's
# adapter discovers the dimension at probe time). OpenRouter is the curated one;
# a user-supplied custom remote provider that the router has an adapter for also
# qualifies (resolved against router.providers() below).
_REMOTE_CUSTOM_PROVIDERS = frozenset({"openrouter"})


def _synthesize_remote_custom_capability(
    provider: str,
    model: str,
    *,
    router: Any | None,
    env: dict[str, str],
) -> EmbeddingCapability:
    """Synthesize a discovery-required capability for a REMOTE provider + an
    uncurated model id. The dimension is unknown here (None) and is discovered at
    create with one probe embed — NEVER fabricated. ``available`` reflects whether
    the provider is actually configured (key present); an unconfigured provider is
    honestly disabled-with-reason so an un-refreshable index isn't minted."""
    providers = set(router.providers()) if router is not None else set()
    env_key = f"{provider.upper()}_API_KEY"
    configured = provider in providers or bool(env.get(env_key))
    return EmbeddingCapability(
        provider_id=provider,
        provider_kind="platform_api",
        model_id=model,
        label=f"Remote · {provider}/{model} (dimension discovered at create)",
        modalities=["text", "row"],
        dimensions=None,
        distance_metrics=["cosine"],
        local=False,
        available=configured,
        error=None if configured else f"missing {env_key}",
        # Embedding unit pricing is not modeled for arbitrary remote ids.
        pricing={"policy": "unknown_unit_price"},
        privacy={"egress": "remote", "local": False},
        size_gb=None,
        max_input_tokens=None,
        recommended=False,
        dimension_discovery_required=True,
    )


def resolve_embedding_capability(
    *,
    modality: str,
    provider: str | None = None,
    model: str | None = None,
    router: Any = None,
    local_available: bool | None = None,
    env: dict[str, str] | None = None,
) -> EmbeddingCapability | None:
    """Pick the registry capability for a requested (provider, model, modality).

    Availability does NOT filter the result — a remote engine with no key still
    resolves so an index can be *created* (its declared model id / dimension are
    the space facts); egress is gated later, at refresh. Among matches, prefer an
    available engine, then a local one (the cheap/private default for text).

    A fastembed-supported model id that is NOT curated in _KNOWN_ENGINES falls
    back to the fastembed registry (custom local ids), so any in-process model can
    be picked. A non-fastembed, non-curated id resolves to None (the create
    executor surfaces embedding_model_unsupported)."""
    import os

    env = env if env is not None else dict(os.environ)
    caps = embedding_capabilities(
        router=router, local_available=local_available, env=env
    )
    candidates = [c for c in caps if modality in c["modalities"]]
    if provider:
        candidates = [c for c in candidates if c["provider_id"] == provider]
    if model:
        candidates = [c for c in candidates if c["model_id"] == model]
    if not candidates:
        # Custom fastembed model id (not curated): resolve dim + size from the
        # fastembed registry for in-process providers. Text/row only.
        if (
            model
            and modality in ("text", "row")
            and (provider is None or provider in _FASTEMBED_PROVIDERS)
        ):
            return _synthesize_fastembed_capability(
                model,
                modality=modality,
                local_available=local_available,
                env=env,
            )
        # Custom REMOTE model id (OpenRouter / any configured remote provider):
        # synthesize a discovery-required capability. The dimension is unknown
        # here and is discovered at create with one probe embed — never faked.
        # A non-remote, non-fastembed id still resolves to None (the executor
        # surfaces embedding_model_unsupported).
        if (
            model
            and provider in _REMOTE_CUSTOM_PROVIDERS
            and modality in ("text", "row")
        ):
            return _synthesize_remote_custom_capability(
                provider, model, router=router, env=env
            )
        return None
    candidates.sort(key=lambda c: (not c["available"], not c["local"]))
    return candidates[0]


__all__ = [
    "ALL_MODALITIES",
    "EmbeddingBatchResult",
    "EmbeddingCapability",
    "build_batch_result",
    "embedding_capabilities",
    "resolve_embedding_capability",
    "space_descriptor_from_result",
]
