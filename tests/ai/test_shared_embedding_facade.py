from __future__ import annotations

import asyncio

import pytest

from frisket.ai.embeddings.facade import EmbeddingFacade, embedding_input_hash
from frisket.ai.embeddings.spaces import (
    DescriptorEmbeddingSpace,
    make_space_descriptor,
    space_id_for,
)
from frisket.retrieval.encoding import (
    AudioEncodingInput,
    EmbeddingGemma2Space,
    EncodingError,
    EncodingPurpose,
    ImageEncodingInput,
    TextEncodingInput,
    VideoClipEncodingInput,
)
from frisket.retrieval.native import pack_vector
from frisket.retrieval.sources import input_hash, representation_id
from frisket.retrieval.store import CachedRepresentation


def _unit(index: int = 0) -> tuple[float, ...]:
    return tuple(1.0 if offset == index else 0.0 for offset in range(128))


class Cache:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], CachedRepresentation] = {}
        self.gets: list[tuple[str, tuple[str, ...]]] = []
        self.puts: list[tuple[CachedRepresentation, ...]] = []

    def get_representations(self, space_id, input_hashes):
        self.gets.append((space_id, tuple(input_hashes)))
        return {
            key: self.rows[(space_id, key)]
            for key in input_hashes
            if (space_id, key) in self.rows
        }

    def put_representations(self, representations):
        batch = tuple(representations)
        self.puts.append(batch)
        for row in batch:
            self.rows[(row.space_id, row.input_hash)] = row

    def seed(self, space_id: str, key: str, vector: tuple[float, ...]) -> None:
        self.rows[(space_id, key)] = CachedRepresentation(
            representation_id=representation_id(space=space_id, input_hash=key),
            space_id=space_id,
            input_hash=key,
            vector=pack_vector(vector, dimensions=len(vector)),
            dimension=len(vector),
        )


def test_input_hash_preserves_legacy_payload_and_domains_embeddinggemma_inputs():
    payload = "first\nsecond"
    assert embedding_input_hash(payload) == input_hash(payload)

    text = TextEncodingInput(payload)
    query = embedding_input_hash(text, purpose=EncodingPurpose.QUERY)
    document = embedding_input_hash(text, purpose=EncodingPurpose.DOCUMENT)
    assert query != document

    image = ImageEncodingInput(b"same bytes")
    audio = AudioEncodingInput(b"same bytes")
    video = VideoClipEncodingInput((image,))
    assert (
        len(
            {
                embedding_input_hash(image),
                embedding_input_hash(audio),
                embedding_input_hash(video),
            }
        )
        == 3
    )


@pytest.mark.asyncio
async def test_facade_embeds_unique_misses_after_admission_and_caches_after_receipt():
    cache = Cache()
    facade = EmbeddingFacade(cache)
    space = EmbeddingGemma2Space(128)
    space_id = space.identity
    first = TextEncodingInput("already cached")
    second = TextEncodingInput("fresh")
    first_key = embedding_input_hash(first, purpose=EncodingPurpose.DOCUMENT)
    cache.seed(space_id, first_key, _unit(0))
    events = []

    async def encode(inputs, *, purpose, space):
        events.append(("provider", tuple(inputs), purpose, space))
        return {"vectors": [list(_unit(1))], "provider_request_id": "request-1"}

    vectors = await facade.encode_async(
        [first, second, second],
        encoder=encode,
        purpose=EncodingPurpose.DOCUMENT,
        space=space,
        before_fresh_batch=lambda inputs: events.append(("admission", tuple(inputs))),
        on_fresh_batch=lambda result, inputs: events.append(
            ("receipt", result["provider_request_id"], tuple(inputs))
        ),
    )

    assert vectors == [_unit(0), _unit(1), _unit(1)]
    assert [event[0] for event in events] == ["admission", "provider", "receipt"]
    assert events[0][1] == (second,)
    assert len(cache.puts) == 1
    assert len(cache.puts[0]) == 1


@pytest.mark.asyncio
async def test_paid_receipt_precedes_malformed_result_refusal_and_no_cache_write():
    cache = Cache()
    facade = EmbeddingFacade(cache)
    events = []

    def encode(inputs, *, purpose, space):
        events.append("provider")
        return {"vectors": [[0.5] + [0.0] * 127], "provider_request_id": "paid"}

    with pytest.raises(EncodingError, match="invalid vector"):
        await facade.encode_async(
            [TextEncodingInput("billable")],
            encoder=encode,
            purpose=EncodingPurpose.DOCUMENT,
            space=EmbeddingGemma2Space(128),
            space_id="space:paid",
            before_fresh_batch=lambda _inputs: events.append("admission"),
            on_fresh_batch=lambda _result, _inputs: events.append("receipt"),
        )

    assert events == ["admission", "provider", "receipt"]
    assert cache.puts == []


@pytest.mark.asyncio
async def test_legacy_cache_payload_uses_exact_migrated_hash_without_provider_call():
    cache = Cache()
    facade = EmbeddingFacade(cache)
    payload = "alpha\nbeta"
    key = embedding_input_hash(payload)
    cache.seed("space:legacy", key, _unit())

    def unreachable(*_args, **_kwargs):
        raise AssertionError("cache hit reached provider")

    vectors = await facade.encode_async(
        [TextEncodingInput(payload)],
        encoder=unreachable,
        purpose=EncodingPurpose.DOCUMENT,
        space=EmbeddingGemma2Space(128),
        space_id="space:legacy",
        cache_payloads=[payload],
        before_fresh_batch=lambda _inputs: pytest.fail("cache hit requested admission"),
        on_fresh_batch=lambda _result, _inputs: pytest.fail(
            "cache hit recorded a receipt"
        ),
    )

    assert vectors == [_unit()]
    assert cache.puts == []


@pytest.mark.asyncio
async def test_descriptor_space_caches_finite_non_normalized_legacy_vectors():
    cache = Cache()
    facade = EmbeddingFacade(cache)
    descriptor = make_space_descriptor(
        provider_id="legacy-provider",
        provider_kind="local_process",
        requested_model="legacy-model",
        actual_model_id="legacy-model",
        modality="text",
        dimension=384,
        normalization="none",
    )
    space = DescriptorEmbeddingSpace.from_descriptor(descriptor)
    vector = [2.0, *([0.0] * 383)]

    fresh = await facade.encode_async(
        [TextEncodingInput("legacy text")],
        encoder=lambda _inputs, **_kwargs: [vector],
        purpose=EncodingPurpose.DOCUMENT,
        space=space,
        cache_payloads=["legacy text"],
    )
    cached = await facade.encode_async(
        [TextEncodingInput("legacy text")],
        encoder=lambda *_args, **_kwargs: pytest.fail("cache miss"),
        purpose=EncodingPurpose.DOCUMENT,
        space=space,
        cache_payloads=["legacy text"],
    )

    assert space.identity == space_id_for(descriptor)
    assert space.dimensions == 384
    assert space.normalized is False
    assert fresh == cached == [tuple(vector)]


def test_descriptor_space_reports_l2_normalization():
    descriptor = make_space_descriptor(
        provider_id="fastembed",
        provider_kind="local_process",
        actual_model_id="model",
        modality="text",
        dimension=384,
        normalization="l2",
    )

    assert DescriptorEmbeddingSpace.from_descriptor(descriptor).normalized is True


def test_sync_facade_rejects_use_from_an_event_loop():
    cache = Cache()
    facade = EmbeddingFacade(cache)

    async def call() -> None:
        with pytest.raises(RuntimeError, match="await encode_async"):
            facade.encode(
                [],
                encoder=lambda *_args, **_kwargs: [],
                purpose=EncodingPurpose.QUERY,
                space=EmbeddingGemma2Space(128),
                space_id="space:test",
            )

    asyncio.run(call())
    assert cache.gets == []
