from __future__ import annotations

import pytest

from frisket.ai.embeddings.analytics import (
    encode_legacy_texts,
    encode_legacy_texts_async,
    encode_project_legacy_texts,
    encode_project_legacy_texts_async,
    ensure_legacy_analytical_space,
    legacy_analytical_descriptor,
    legacy_analytical_space,
)
from frisket.ai.embeddings.backends import LOCAL_MODEL, REMOTE_EMBED_MODEL
from frisket.engine.store import Project
from frisket.retrieval.store import CachedRepresentation, RetrievalStore


class MemoryCache:
    def __init__(self):
        self.rows: dict[tuple[str, str], CachedRepresentation] = {}

    def get_representations(self, space_id, input_hashes):
        return {
            input_hash: self.rows[(space_id, input_hash)]
            for input_hash in input_hashes
            if (space_id, input_hash) in self.rows
        }

    def put_representations(self, representations):
        for representation in representations:
            self.rows[(representation.space_id, representation.input_hash)] = (
                representation
            )


def test_legacy_space_uses_capability_dimensions_without_claiming_old_normalization():
    local = legacy_analytical_descriptor(f"fastembed/{LOCAL_MODEL}")
    remote = legacy_analytical_descriptor(REMOTE_EMBED_MODEL)

    assert (local["dimension"], local["normalization"]) == (384, "none")
    assert (remote["dimension"], remote["normalization"]) == (1536, "none")
    assert local["actual_model_id"] == f"fastembed/{LOCAL_MODEL}"
    assert remote["actual_model_id"] == REMOTE_EMBED_MODEL
    assert local["model_revision"] == remote["model_revision"] == "unknown"


def test_custom_legacy_model_requires_factual_dimension_and_retains_override():
    with pytest.raises(ValueError, match="dimension is unknown"):
        legacy_analytical_space("custom/model")

    space = legacy_analytical_space("custom/model", dimension=7)
    assert space.dimensions == 7
    assert space.normalized is False


def test_sync_encoder_uses_injected_cache_without_project_writes():
    cache = MemoryCache()
    space = legacy_analytical_space("custom/model", dimension=2)
    calls = []

    def embed(texts):
        calls.append(list(texts))
        return [[2.0, float(index)] for index, _text in enumerate(texts)]

    first = encode_legacy_texts(
        cache, ["alpha", "beta", "alpha"], embed=embed, space=space
    )
    second = encode_legacy_texts(cache, ["beta", "alpha"], embed=embed, space=space)

    assert first == [[2.0, 0.0], [2.0, 1.0], [2.0, 0.0]]
    assert second == [[2.0, 1.0], [2.0, 0.0]]
    assert calls == [["alpha", "beta"]]


@pytest.mark.asyncio
async def test_async_paid_order_is_admission_provider_receipt_then_cache_hit():
    cache = MemoryCache()
    space = legacy_analytical_space(REMOTE_EMBED_MODEL)
    events = []

    async def embed(texts):
        events.append(("provider", list(texts)))
        return {
            "vectors": [[2.0, *([0.0] * 1535)] for _text in texts],
            "provider_request_id": "paid-1",
        }

    callbacks = {
        "before_fresh_batch": lambda texts: events.append(("admission", texts)),
        "on_fresh_batch": lambda result, texts: events.append(
            ("receipt", result["provider_request_id"], texts)
        ),
    }
    first = await encode_legacy_texts_async(
        cache, ["paid"], embed=embed, space=space, **callbacks
    )
    second = await encode_legacy_texts_async(
        cache, ["paid"], embed=embed, space=space, **callbacks
    )

    assert first == second
    assert [event[0] for event in events] == ["admission", "provider", "receipt"]


@pytest.mark.asyncio
async def test_project_encoder_persists_same_space_used_by_shared_cache(tmp_path):
    project = Project.create(tmp_path / "analytics.frisket", name="analytics")
    model = "custom/model"
    try:
        vectors = await encode_project_legacy_texts_async(
            project,
            ["one"],
            embed=lambda _texts: [[0.25, 0.5]],
            model_id=model,
            dimension=2,
        )
        space = ensure_legacy_analytical_space(project, model, dimension=2)
        row = project.db.execute(
            "SELECT actual_model_id,dimension,normalization FROM embedding_spaces "
            "WHERE id=?",
            (space.identity,),
        ).fetchone()
        with RetrievalStore(project) as cache:
            count = cache.db.execute(
                "SELECT count(*) FROM representations WHERE space_id=?",
                (space.identity,),
            ).fetchone()[0]

        assert vectors == [[0.25, 0.5]]
        assert tuple(row) == (model, 2, "none")
        assert count == 1
    finally:
        project.close()


def test_custom_project_model_discovers_dimension_from_first_admitted_batch(tmp_path):
    project = Project.create(tmp_path / "custom-analytics.frisket")
    events = []

    def embed(texts):
        events.append(("provider", list(texts)))
        return [[2.0, float(index), 0.5] for index, _text in enumerate(texts)]

    callbacks = {
        "before_fresh_batch": lambda texts: events.append(("admission", texts)),
        "on_fresh_batch": lambda _result, texts: events.append(("receipt", texts)),
    }
    try:
        first = encode_project_legacy_texts(
            project,
            ["alpha", "beta", "alpha"],
            embed=embed,
            model_id="custom/unregistered",
            **callbacks,
        )
        second = encode_project_legacy_texts(
            project,
            ["beta", "alpha"],
            embed=embed,
            model_id="custom/unregistered",
            **callbacks,
        )
        row = project.db.execute(
            "SELECT dimension FROM embedding_spaces "
            "WHERE actual_model_id='custom/unregistered'"
        ).fetchone()

        assert first == [[2.0, 0.0, 0.5], [2.0, 1.0, 0.5], [2.0, 0.0, 0.5]]
        assert second == [[2.0, 1.0, 0.5], [2.0, 0.0, 0.5]]
        assert row["dimension"] == 3
        assert [event[0] for event in events] == [
            "admission",
            "provider",
            "receipt",
        ]
    finally:
        project.close()
