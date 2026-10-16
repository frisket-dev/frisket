"""Cache-first embedding batches over the unified retrieval representation store."""

from __future__ import annotations

import asyncio
import inspect
import math
import struct
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, Protocol, TypeAlias

from frisket.retrieval.encoding import (
    EMBEDDINGGEMMA2_DOCUMENT_PREFIX,
    EMBEDDINGGEMMA2_QUERY_PREFIX,
    AudioEncodingInput,
    EmbeddingVector,
    EncodingError,
    EncodingInput,
    EncodingPurpose,
    ImageEncodingInput,
    TextEncodingInput,
    VideoClipEncodingInput,
)
from frisket.retrieval.native import pack_vector, validate_vector_blob
from frisket.retrieval.sources import input_hash, representation_id
from frisket.retrieval.store import CachedRepresentation


MAX_CACHED_BATCH = 1000
RawEmbeddingResult: TypeAlias = Sequence[Sequence[float]] | Mapping[str, Any]
FreshEncoder: TypeAlias = Callable[
    ...,
    RawEmbeddingResult | Awaitable[RawEmbeddingResult],
]
BeforeFreshBatch: TypeAlias = Callable[[list[EncodingInput]], None]
OnFreshBatch: TypeAlias = Callable[
    [RawEmbeddingResult, list[EncodingInput]], None
]


class RepresentationCache(Protocol):
    def get_representations(
        self, space_id: str, input_hashes: Sequence[str]
    ) -> dict[str, CachedRepresentation]: ...

    def put_representations(
        self, representations: Sequence[CachedRepresentation]
    ) -> None: ...


class EmbeddingSpace(Protocol):
    """The vector facts needed to validate and address cached representations."""

    dimensions: int
    identity: str
    normalized: bool


def embedding_input_hash(
    value: EncodingInput | bytes | str,
    *,
    purpose: EncodingPurpose | None = None,
) -> str:
    """Hash the exact logical input presented to an embedding processor.

    Plain ``str`` and ``bytes`` retain the historical contract: SHA-256 of
    those bytes with no domain prefix.  This is the key used by migrated
    FastEmbed row payloads.  Typed EmbeddingGemma inputs include only processor
    facts that change the encoded value: text's query/document prompt, media
    modality, PCM sample rate, and ordered video-frame boundaries.
    """

    if isinstance(value, str | bytes):
        return input_hash(value)
    if isinstance(value, TextEncodingInput):
        if not isinstance(purpose, EncodingPurpose):
            raise TypeError("text input hashing requires an EncodingPurpose")
        prefix = (
            EMBEDDINGGEMMA2_QUERY_PREFIX
            if purpose is EncodingPurpose.QUERY
            else EMBEDDINGGEMMA2_DOCUMENT_PREFIX
        )
        return input_hash(prefix + value.text)
    if isinstance(value, ImageEncodingInput):
        return input_hash(b"frisket-embedding-input-v1\0image\0" + value.image_bytes)
    if isinstance(value, AudioEncodingInput):
        header = (
            f"frisket-embedding-input-v1\0audio\0pcm16le-mono\0"
            f"{value.sample_rate}\0"
        ).encode("ascii")
        return input_hash(header + value.pcm16le_bytes)
    if isinstance(value, VideoClipEncodingInput):
        payload = bytearray(b"frisket-embedding-input-v1\0video\0")
        for frame in value.frames:
            payload.extend(len(frame.image_bytes).to_bytes(8, "little"))
            payload.extend(frame.image_bytes)
        return input_hash(bytes(payload))
    raise TypeError("unsupported embedding input")


def _vectors(result: RawEmbeddingResult) -> Sequence[Sequence[float]]:
    if isinstance(result, Mapping):
        vectors = result.get("vectors")
        if not isinstance(vectors, Sequence) or isinstance(vectors, str | bytes):
            raise EncodingError(
                "invalid_encoder_result",
                "The embedding provider returned no vector batch.",
            )
        return vectors
    if isinstance(result, str | bytes):
        raise EncodingError(
            "invalid_encoder_result", "The embedding provider returned no vector batch."
        )
    return result


def _unpack_cached(
    representation: CachedRepresentation, *, space: EmbeddingSpace
) -> EmbeddingVector:
    if (
        representation.dimension != space.dimensions
        or representation.precision != "float32"
    ):
        raise EncodingError(
            "invalid_cached_representation",
            "The cached embedding does not match its declared space.",
        )
    try:
        blob = _validate_vector_blob(representation.vector, space=space)
    except (TypeError, ValueError) as exc:
        raise EncodingError(
            "invalid_cached_representation",
            "The cached embedding does not match its declared space.",
        ) from exc
    return tuple(value[0] for value in struct.iter_unpack("<f", blob))


def _pack_vector(values: Sequence[float], *, space: EmbeddingSpace) -> bytes:
    if space.normalized:
        return pack_vector(values, dimensions=space.dimensions)
    encoded = bytearray()
    for value in values:
        if isinstance(value, bool):
            raise ValueError("vector values must be numbers")
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("vector values must be finite")
        try:
            chunk = struct.pack("<f", number)
        except (OverflowError, struct.error) as exc:
            raise ValueError("vector values must fit finite float32") from exc
        if not math.isfinite(struct.unpack("<f", chunk)[0]):
            raise ValueError("vector values must fit finite float32")
        encoded.extend(chunk)
    if len(encoded) != space.dimensions * 4:
        raise ValueError(f"expected {space.dimensions} vector dimensions")
    return bytes(encoded)


def _validate_vector_blob(blob: bytes, *, space: EmbeddingSpace) -> bytes:
    if space.normalized:
        return validate_vector_blob(blob, dimensions=space.dimensions)
    if not isinstance(blob, bytes):
        raise TypeError("vector blob must be bytes")
    if len(blob) != space.dimensions * 4:
        raise ValueError(f"expected {space.dimensions * 4} vector bytes")
    return _pack_vector(
        [value[0] for value in struct.iter_unpack("<f", blob)], space=space
    )


def _validate_space(space: EmbeddingSpace) -> None:
    if (
        isinstance(space.dimensions, bool)
        or not isinstance(space.dimensions, int)
        or space.dimensions <= 0
    ):
        raise TypeError("space dimensions must be a positive integer")
    if not isinstance(space.identity, str) or not space.identity.strip():
        raise TypeError("space identity must be a non-empty string")
    if not isinstance(space.normalized, bool):
        raise TypeError("space normalized flag must be a boolean")


def _validated_fresh(
    result: RawEmbeddingResult,
    *,
    expected: int,
    space: EmbeddingSpace,
) -> list[EmbeddingVector]:
    raw = _vectors(result)
    if len(raw) != expected:
        raise EncodingError(
            "invalid_encoder_result",
            "The embedding provider returned the wrong number of vectors.",
        )
    vectors: list[EmbeddingVector] = []
    for value in raw:
        if any(isinstance(item, bool) for item in value):
            raise EncodingError(
                "invalid_encoder_result",
                "The embedding provider returned an invalid vector.",
            )
        try:
            blob = _pack_vector(value, space=space)
        except (TypeError, ValueError) as exc:
            raise EncodingError(
                "invalid_encoder_result",
                "The embedding provider returned an invalid vector.",
            ) from exc
        vectors.append(tuple(item[0] for item in struct.iter_unpack("<f", blob)))
    return vectors


class EmbeddingFacade:
    """Resolve aligned vectors through one content-addressed project cache."""

    def __init__(self, cache: RepresentationCache) -> None:
        self._cache = cache

    async def encode_async(
        self,
        inputs: Sequence[EncodingInput],
        *,
        encoder: FreshEncoder,
        purpose: EncodingPurpose,
        space: EmbeddingSpace,
        space_id: str | None = None,
        cache_payloads: Sequence[bytes | str] | None = None,
        before_fresh_batch: BeforeFreshBatch | None = None,
        on_fresh_batch: OnFreshBatch | None = None,
    ) -> list[EmbeddingVector]:
        """Return cached/fresh vectors in input order, embedding unique misses once.

        Paid callback order is strict: cache lookup, admission callback, provider,
        receipt callback, result validation, then cache publication.
        """

        values = list(inputs)
        if not values:
            return []
        if len(values) > MAX_CACHED_BATCH:
            raise ValueError(f"embedding batches are bounded to {MAX_CACHED_BATCH} inputs")
        if not isinstance(purpose, EncodingPurpose):
            raise TypeError("purpose must be EncodingPurpose.QUERY or DOCUMENT")
        _validate_space(space)
        resolved_space_id = space.identity if space_id is None else space_id
        if not isinstance(resolved_space_id, str) or not resolved_space_id.strip():
            raise ValueError("space_id must be non-empty")
        if cache_payloads is not None and len(cache_payloads) != len(values):
            raise ValueError("cache payloads must align with embedding inputs")
        if cache_payloads is not None:
            for value, payload in zip(values, cache_payloads, strict=True):
                exact = (
                    payload.encode("utf-8") if isinstance(payload, str) else payload
                )
                if not isinstance(value, TextEncodingInput) or exact != value.text.encode(
                    "utf-8"
                ):
                    raise ValueError(
                        "cache payloads are only for exact legacy text encoder inputs"
                    )

        hashes = [
            embedding_input_hash(payload)
            if cache_payloads is not None
            else embedding_input_hash(value, purpose=purpose)
            for value, payload in zip(
                values,
                cache_payloads if cache_payloads is not None else values,
                strict=True,
            )
        ]
        unique_hashes = list(dict.fromkeys(hashes))
        cached_rows = self._cache.get_representations(resolved_space_id, unique_hashes)
        cached: dict[str, EmbeddingVector] = {
            key: _unpack_cached(row, space=space)
            for key, row in cached_rows.items()
        }
        missing_hashes = [key for key in unique_hashes if key not in cached]
        if missing_hashes:
            first_index = {key: hashes.index(key) for key in missing_hashes}
            fresh_inputs = [values[first_index[key]] for key in missing_hashes]
            if before_fresh_batch is not None:
                before_fresh_batch(fresh_inputs)
            result = encoder(fresh_inputs, purpose=purpose, space=space)
            if inspect.isawaitable(result):
                result = await result
            if on_fresh_batch is not None:
                on_fresh_batch(result, fresh_inputs)
            fresh = _validated_fresh(
                result,
                expected=len(missing_hashes),
                space=space,
            )
            publications: list[CachedRepresentation] = []
            for key, vector in zip(missing_hashes, fresh, strict=True):
                cached[key] = vector
                publications.append(
                    CachedRepresentation(
                        representation_id=representation_id(
                            space=resolved_space_id, input_hash=key
                        ),
                        space_id=resolved_space_id,
                        input_hash=key,
                        vector=_pack_vector(vector, space=space),
                        dimension=space.dimensions,
                    )
                )
            self._cache.put_representations(publications)
        return [cached[key] for key in hashes]

    def encode(
        self,
        inputs: Sequence[EncodingInput],
        *,
        encoder: FreshEncoder,
        purpose: EncodingPurpose,
        space: EmbeddingSpace,
        space_id: str | None = None,
        cache_payloads: Sequence[bytes | str] | None = None,
        before_fresh_batch: BeforeFreshBatch | None = None,
        on_fresh_batch: OnFreshBatch | None = None,
    ) -> list[EmbeddingVector]:
        """Synchronous facade for worker-thread and CLI callers."""

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(
                self.encode_async(
                    inputs,
                    encoder=encoder,
                    purpose=purpose,
                    space=space,
                    space_id=space_id,
                    cache_payloads=cache_payloads,
                    before_fresh_batch=before_fresh_batch,
                    on_fresh_batch=on_fresh_batch,
                )
            )
        raise RuntimeError("EmbeddingFacade.encode cannot run on an event loop; await encode_async")


__all__ = [
    "BeforeFreshBatch",
    "EmbeddingFacade",
    "EmbeddingSpace",
    "FreshEncoder",
    "MAX_CACHED_BATCH",
    "OnFreshBatch",
    "RawEmbeddingResult",
    "RepresentationCache",
    "embedding_input_hash",
]
