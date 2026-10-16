"""Shared content-addressed vectors for legacy analytical text consumers."""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, TypeAlias

from frisket.retrieval.encoding import (
    EmbeddingVector,
    EncodingPurpose,
    TextEncodingInput,
)
from frisket.retrieval.store import RetrievalStore

from .backends import LOCAL_MODEL, REMOTE_EMBED_MODEL, EmbeddingResult, Embedder
from .capabilities import resolve_embedding_capability
from .facade import (
    MAX_CACHED_BATCH,
    EmbeddingFacade,
    RepresentationCache,
)
from .spaces import DescriptorEmbeddingSpace, make_space_descriptor
from .store import EmbeddingStore


BeforeFreshTexts: TypeAlias = Callable[[list[str]], None]
OnFreshTexts: TypeAlias = Callable[[EmbeddingResult, list[str]], None]
AsyncTextEmbedder: TypeAlias = Callable[
    [list[str]], EmbeddingResult | Awaitable[EmbeddingResult]
]

_FALLBACK_DIMENSIONS = {
    f"fastembed/{LOCAL_MODEL}": 384,
    REMOTE_EMBED_MODEL: 1536,
}


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    """Cosine similarity over finite analytical vectors of the same width."""

    if len(left) != len(right):
        raise ValueError("embedding vectors have inconsistent dimensions")
    dot = sum(float(a) * float(b) for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(float(value) ** 2 for value in left))
    right_norm = math.sqrt(sum(float(value) ** 2 for value in right))
    if not left_norm or not right_norm:
        return 0.0
    return dot / (left_norm * right_norm)


def _model_identity(model_id: str) -> tuple[str, str]:
    if not isinstance(model_id, str) or not model_id.strip():
        raise ValueError("analytical embedding model id must be non-empty")
    if "/" not in model_id:
        return "legacy", model_id
    provider_id, requested_model = model_id.split("/", 1)
    return provider_id, requested_model


def legacy_analytical_descriptor(
    model_id: str,
    *,
    dimension: int | None = None,
    normalization: str | None = None,
) -> dict[str, Any]:
    """Describe the exact historical text-input cache for one analytical model.

    Legacy ``cell_vec`` recorded the model id and raw text, but no revision or
    normalization fact.  Preserve those unknowns instead of upgrading them by
    inference.  A custom model remains supported when its caller supplies the
    factual output dimension.
    """

    provider_id, requested_model = _model_identity(model_id)
    capability = resolve_embedding_capability(
        modality="text",
        provider=None if provider_id == "legacy" else provider_id,
        model=requested_model,
        local_available=False,
        env={"FRISKET_DISABLE_LOCAL_EMBED": "1"},
    )
    known_dimensions = (
        tuple(capability["dimensions"] or ()) if capability is not None else ()
    )
    if dimension is None:
        if len(known_dimensions) == 1:
            dimension = known_dimensions[0]
        else:
            dimension = _FALLBACK_DIMENSIONS.get(model_id)
    elif known_dimensions and dimension not in known_dimensions:
        raise ValueError(
            f"analytical embedding dimension {dimension} does not match "
            f"{model_id!r} metadata"
        )
    if isinstance(dimension, bool) or not isinstance(dimension, int) or dimension <= 0:
        raise ValueError(
            f"analytical embedding dimension is unknown for {model_id!r}; "
            "supply the provider-reported dimension"
        )

    provider_kind = (
        str(capability["provider_kind"])
        if capability is not None
        else "local_process"
        if provider_id == "fastembed"
        else "platform_api"
    )
    return make_space_descriptor(
        provider_id=provider_id,
        provider_kind=provider_kind,
        requested_model=requested_model,
        # Migration proves old cache rows against this exact historical key.
        actual_model_id=model_id,
        model_revision="unknown",
        modality="text",
        dimension=dimension,
        dtype="float32",
        distance_metric="cosine",
        normalization=normalization or "none",
        vector_options={"input_contract": "legacy-analytical-text-v1"},
    )


def legacy_analytical_space(
    model_id: str,
    *,
    dimension: int | None = None,
    normalization: str | None = None,
) -> DescriptorEmbeddingSpace:
    """Return the stable shared-cache identity without mutating a project."""

    return DescriptorEmbeddingSpace.from_descriptor(
        legacy_analytical_descriptor(
            model_id, dimension=dimension, normalization=normalization
        )
    )


def ensure_legacy_analytical_space(
    project: Any,
    model_id: str,
    *,
    dimension: int | None = None,
    normalization: str | None = None,
) -> DescriptorEmbeddingSpace:
    """Persist and return the canonical legacy analytical space definition."""

    descriptor = legacy_analytical_descriptor(
        model_id, dimension=dimension, normalization=normalization
    )
    space = DescriptorEmbeddingSpace.from_descriptor(descriptor)
    stored = EmbeddingStore(project).create_space(
        descriptor, commit=not project.db.in_transaction
    )
    if stored != space.identity:
        raise RuntimeError("analytical embedding space identity changed while storing")
    return space


def _reported_dimension(embed: Any, dimension: int | None) -> int | None:
    """Use explicit or backend-reported dimensions without probing a provider."""

    if dimension is not None:
        return dimension
    reported = getattr(embed, "embedding_dimension", None)
    if isinstance(reported, bool) or not isinstance(reported, int) or reported <= 0:
        return None
    return reported


def _text_inputs(texts: Sequence[str]) -> tuple[list[str], list[TextEncodingInput]]:
    values = list(texts)
    if any(not isinstance(value, str) for value in values):
        raise TypeError("analytical embedding inputs must be strings")
    return values, [TextEncodingInput(value) for value in values]


def _fresh_adapter(embed: AsyncTextEmbedder):
    def encode(inputs, **_space_facts):
        texts = [value.text for value in inputs]
        async_embed = getattr(embed, "embed_batch_async", None)
        return async_embed(texts) if callable(async_embed) else embed(texts)

    return encode


def encode_legacy_texts(
    cache: RepresentationCache,
    texts: Sequence[str],
    *,
    embed: Embedder,
    space: DescriptorEmbeddingSpace,
    before_fresh_batch: BeforeFreshTexts | None = None,
    on_fresh_batch: OnFreshTexts | None = None,
) -> list[list[float]]:
    """Synchronously resolve raw-text vectors through an injected cache."""

    values, inputs = _text_inputs(texts)
    vectors: list[EmbeddingVector] = []
    facade = EmbeddingFacade(cache)
    for offset in range(0, len(values), MAX_CACHED_BATCH):
        batch_values = values[offset : offset + MAX_CACHED_BATCH]
        batch_inputs = inputs[offset : offset + MAX_CACHED_BATCH]
        vectors.extend(
            facade.encode(
                batch_inputs,
                encoder=_fresh_adapter(embed),
                purpose=EncodingPurpose.DOCUMENT,
                space=space,
                cache_payloads=batch_values,
                before_fresh_batch=(
                    None
                    if before_fresh_batch is None
                    else lambda fresh: before_fresh_batch(
                        [value.text for value in fresh]
                    )
                ),
                on_fresh_batch=(
                    None
                    if on_fresh_batch is None
                    else lambda result, fresh: on_fresh_batch(
                        result, [value.text for value in fresh]
                    )
                ),
            )
        )
    return [list(vector) for vector in vectors]


async def encode_legacy_texts_async(
    cache: RepresentationCache,
    texts: Sequence[str],
    *,
    embed: AsyncTextEmbedder,
    space: DescriptorEmbeddingSpace,
    before_fresh_batch: BeforeFreshTexts | None = None,
    on_fresh_batch: OnFreshTexts | None = None,
) -> list[list[float]]:
    """Asynchronously resolve raw-text vectors through an injected cache."""

    values, inputs = _text_inputs(texts)
    vectors: list[EmbeddingVector] = []
    facade = EmbeddingFacade(cache)
    for offset in range(0, len(values), MAX_CACHED_BATCH):
        batch_values = values[offset : offset + MAX_CACHED_BATCH]
        batch_inputs = inputs[offset : offset + MAX_CACHED_BATCH]
        vectors.extend(
            await facade.encode_async(
                batch_inputs,
                encoder=_fresh_adapter(embed),
                purpose=EncodingPurpose.DOCUMENT,
                space=space,
                cache_payloads=batch_values,
                before_fresh_batch=(
                    None
                    if before_fresh_batch is None
                    else lambda fresh: before_fresh_batch(
                        [value.text for value in fresh]
                    )
                ),
                on_fresh_batch=(
                    None
                    if on_fresh_batch is None
                    else lambda result, fresh: on_fresh_batch(
                        result, [value.text for value in fresh]
                    )
                ),
            )
        )
    return [list(vector) for vector in vectors]


def encode_project_legacy_texts(
    project: Any,
    texts: Sequence[str],
    *,
    embed: Embedder,
    model_id: str,
    dimension: int | None = None,
    normalization: str | None = None,
    before_fresh_batch: BeforeFreshTexts | None = None,
    on_fresh_batch: OnFreshTexts | None = None,
) -> list[list[float]]:
    """Persist the space and synchronously use one project's shared cache."""

    space = ensure_legacy_analytical_space(
        project,
        model_id,
        dimension=_reported_dimension(embed, dimension),
        normalization=normalization,
    )
    with RetrievalStore(project) as cache:
        return encode_legacy_texts(
            cache,
            texts,
            embed=embed,
            space=space,
            before_fresh_batch=before_fresh_batch,
            on_fresh_batch=on_fresh_batch,
        )


async def encode_project_legacy_texts_async(
    project: Any,
    texts: Sequence[str],
    *,
    embed: AsyncTextEmbedder,
    model_id: str,
    dimension: int | None = None,
    normalization: str | None = None,
    before_fresh_batch: BeforeFreshTexts | None = None,
    on_fresh_batch: OnFreshTexts | None = None,
) -> list[list[float]]:
    """Persist the space and asynchronously use one project's shared cache."""

    space = ensure_legacy_analytical_space(
        project,
        model_id,
        dimension=_reported_dimension(embed, dimension),
        normalization=normalization,
    )
    with RetrievalStore(project) as cache:
        return await encode_legacy_texts_async(
            cache,
            texts,
            embed=embed,
            space=space,
            before_fresh_batch=before_fresh_batch,
            on_fresh_batch=on_fresh_batch,
        )


__all__ = [
    "BeforeFreshTexts",
    "OnFreshTexts",
    "cosine_similarity",
    "encode_legacy_texts",
    "encode_legacy_texts_async",
    "encode_project_legacy_texts",
    "encode_project_legacy_texts_async",
    "ensure_legacy_analytical_space",
    "legacy_analytical_descriptor",
    "legacy_analytical_space",
]
