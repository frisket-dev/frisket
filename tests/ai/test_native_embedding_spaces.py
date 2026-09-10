"""Native embedding space identity + project.db metadata storage (Lane 2).

These are the strict-comparability foundation: a ``space_id`` is a generated
identity over every parameter that can change vector values or distance
semantics (handoff note "Space identity"), and ``embedding_spaces`` /
``embedding_indexes`` are the canonical project.db definitions. Raw vectors do
NOT live here (that is the rebuildable sidecar — test_native_embedding_vector_backend).
"""

from __future__ import annotations

import sqlite3

import pytest

from frisket.ai.embeddings import (
    EmbeddingStore,
    make_space_descriptor,
    space_id_for,
    descriptor_hash,
    vector_options_hash,
)
from frisket.engine.store import Project


@pytest.fixture
def project(tmp_path):
    p = Project.create(tmp_path / "emb.frisket", name="emb")
    yield p
    p.close()


def _descriptor(**over):
    base = dict(
        provider_id="frisket-models",
        provider_kind="local_http",
        requested_model="default-image",
        actual_model_id="open_clip/ViT-B-32/laion2b_s34b_b79k",
        modality="image",
        dimension=512,
        dtype="float32",
        distance_metric="cosine",
        normalization="l2",
        vector_options={"resize": 224},
    )
    base.update(over)
    return make_space_descriptor(**base)


class TestSpaceIdentity:
    def test_space_id_is_deterministic_and_prefixed(self):
        d = _descriptor()
        a = space_id_for(d)
        b = space_id_for(_descriptor())
        assert a == b
        assert a.startswith("emb_")
        # base32 body, generated identity, not user typed
        assert len(a) == len("emb_") + 24

    @pytest.mark.parametrize(
        "field,value",
        [
            ("dimension", 256),
            ("actual_model_id", "open_clip/ViT-L-14/laion2b"),
            ("distance_metric", "dot"),
            ("normalization", "none"),
            ("modality", "text"),
            ("dtype", "int8"),
            ("provider_id", "openai"),
        ],
    )
    def test_value_affecting_params_change_space_id(self, field, value):
        assert space_id_for(_descriptor()) != space_id_for(
            _descriptor(**{field: value})
        )

    def test_vector_options_change_space_id(self):
        a = space_id_for(_descriptor(vector_options={"resize": 224}))
        b = space_id_for(_descriptor(vector_options={"resize": 336}))
        assert a != b

    def test_vector_options_hash_is_stable_and_order_independent(self):
        h1 = vector_options_hash({"a": 1, "b": 2})
        h2 = vector_options_hash({"b": 2, "a": 1})
        assert h1 == h2 and h1.startswith("sha256:")

    def test_presentation_fields_do_not_affect_space_id(self):
        # labels are UI sugar and stored separately; a stray label/name in the
        # descriptor dict must not change identity.
        d = _descriptor()
        baseline = space_id_for(d)
        polluted = dict(d, label="My image space", name="whatever", schedule="daily")
        assert space_id_for(polluted) == baseline

    def test_descriptor_hash_is_full_sha256(self):
        h = descriptor_hash(_descriptor())
        assert h.startswith("sha256:")
        assert len(h) == len("sha256:") + 64

    def test_invalid_descriptor_rejected(self):
        with pytest.raises(ValueError):
            make_space_descriptor(
                provider_id="x",
                provider_kind="local_http",
                actual_model_id="m",
                modality="text",
                dimension=0,  # must be > 0
            )
        with pytest.raises(ValueError):
            make_space_descriptor(
                provider_id="x",
                provider_kind="local_http",
                actual_model_id="m",
                modality="not-a-modality",
                dimension=8,
            )


class TestSpaceStorage:
    def test_tables_exist_in_fresh_project(self, project):
        names = {
            r["name"]
            for r in project.db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {"embedding_spaces", "embedding_indexes"} <= names

    def test_create_space_is_idempotent_by_descriptor(self, project):
        store = EmbeddingStore(project)
        d = _descriptor()
        sid1 = store.create_space(d)
        sid2 = store.create_space(d)
        assert sid1 == sid2 == space_id_for(d)
        rows = project.db.execute(
            "SELECT COUNT(*) AS n FROM embedding_spaces"
        ).fetchone()
        assert rows["n"] == 1

    def test_create_space_persists_all_descriptor_facts(self, project):
        store = EmbeddingStore(project)
        sid = store.create_space(_descriptor())
        row = store.get_space(sid)
        assert row["actual_model_id"] == "open_clip/ViT-B-32/laion2b_s34b_b79k"
        assert row["modality"] == "image"
        assert row["dimension"] == 512
        assert row["distance_metric"] == "cosine"
        assert row["normalization"] == "l2"
        assert row["provider_kind"] == "local_http"
        assert row["descriptor_hash"] == descriptor_hash(_descriptor())

    def test_distinct_descriptors_make_distinct_spaces(self, project):
        store = EmbeddingStore(project)
        store.create_space(_descriptor())
        store.create_space(_descriptor(dimension=256))
        n = project.db.execute("SELECT COUNT(*) AS n FROM embedding_spaces").fetchone()[
            "n"
        ]
        assert n == 2

    def test_create_and_get_index_roundtrips_policy(self, project):
        store = EmbeddingStore(project)
        sid = store.create_space(_descriptor(modality="text", provider_id="fastembed"))
        sheet_id = project.add_sheet("data")
        idx_id = store.create_index(
            name="headlines",
            space_id=sid,
            sheet_id=sheet_id,
            source_query={
                "version": "frisket.query.v1",
                "scope": {"sheet_id": sheet_id},
            },
            source_columns=["title"],
            source_policy={"kind": "text_cell", "normalization": "nfc"},
            maintenance_policy={"mode": "manual", "on_source_append": False},
            provider_policy={"allow_remote": False},
        )
        assert idx_id.startswith("embidx_")
        idx = store.get_index(idx_id)
        assert idx["name"] == "headlines"
        assert idx["space_id"] == sid
        assert idx["sheet_id"] == sheet_id
        assert idx["status"] == "idle"
        assert idx["ready_items"] == 0
        # policy hash is derived from the canonical source policy
        assert idx["source_policy_hash"].startswith("sha256:")
        assert store.get_source_policy(idx_id)["kind"] == "text_cell"

    def test_index_requires_existing_space(self, project):
        store = EmbeddingStore(project)
        sheet_id = project.add_sheet("data")
        with pytest.raises((ValueError, sqlite3.IntegrityError)):
            store.create_index(
                name="bad",
                space_id="emb_does_not_exist",
                sheet_id=sheet_id,
                source_query={},
                source_columns=["title"],
                source_policy={"kind": "text_cell"},
            )

    def test_update_index_counts(self, project):
        store = EmbeddingStore(project)
        sid = store.create_space(_descriptor())
        idx_id = store.create_index(
            name="x",
            space_id=sid,
            sheet_id=None,
            source_query={},
            source_columns=["c"],
            source_policy={"kind": "image_blob"},
        )
        store.update_index_counts(
            idx_id, total=10, ready=7, stale=2, error=1, status="ready"
        )
        idx = store.get_index(idx_id)
        assert (idx["total_items"], idx["ready_items"]) == (10, 7)
        assert (idx["stale_items"], idx["error_items"]) == (2, 1)
        assert idx["status"] == "ready"
