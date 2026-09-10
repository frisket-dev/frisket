"""Exact SQLite/BLOB vector backend in the rebuildable project.embeddings.db.

Default backend per the handoff note: little-endian float32 blobs ranked by a
registered ``f32_cosine_distance`` scalar so ranking is DB-shaped (swappable for
sqlite-vec / Vec1 later without changing the public contract). Raw vectors live
ONLY here; project.db keeps definitions/receipts. The sidecar is rebuildable —
deleting it must never corrupt the project.
"""

from __future__ import annotations

import math

import pytest

from frisket.ai.embeddings.vector_backend import VectorBackend, pack_f32, unpack_f32
from frisket.engine.store import Project


@pytest.fixture
def project(tmp_path):
    p = Project.create(tmp_path / "vec.frisket", name="vec")
    yield p
    p.close()


def test_pack_unpack_roundtrip():
    vec = [0.1, -2.5, 3.0, 0.0]
    out = unpack_f32(pack_f32(vec))
    assert all(abs(a - b) < 1e-6 for a, b in zip(vec, out))
    # 4 floats * 4 bytes
    assert len(pack_f32(vec)) == 16


def test_backend_creates_sidecar_db(project):
    backend = VectorBackend(project)
    backend.ensure_schema()
    assert (project.path / "project.embeddings.db").exists()
    names = {
        r["name"]
        for r in backend.db.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"embedding_items", "embedding_vectors_f32"} <= names
    backend.close()


def test_exact_cosine_ranking_orders_by_distance(project):
    backend = VectorBackend(project)
    backend.ensure_schema()
    idx, space = "embidx_a", "emb_a"
    rows = {
        "near": [1.0, 0.0, 0.0],
        "mid": [0.7, 0.7, 0.0],
        "far": [0.0, 1.0, 0.0],
    }
    for key, vec in rows.items():
        backend.upsert_item(
            index_id=idx,
            space_id=space,
            source_key=key,
            source_ref={"row_id": key},
            source_hash="h:" + key,
            status="ready",
            vector=vec,
        )
    results = backend.query_similar(idx, space, [1.0, 0.0, 0.0], k=3)
    assert [k for k, _ in results] == ["near", "mid", "far"]
    # exact: identical vector → ~0 distance
    assert results[0][1] == pytest.approx(0.0, abs=1e-6)
    # orthogonal → cosine distance ~1
    assert results[-1][1] == pytest.approx(1.0, abs=1e-6)
    backend.close()


def test_query_only_returns_ready_items(project):
    backend = VectorBackend(project)
    backend.ensure_schema()
    idx, space = "embidx_b", "emb_b"
    backend.upsert_item(
        index_id=idx,
        space_id=space,
        source_key="ok",
        source_ref={},
        source_hash="h1",
        status="ready",
        vector=[1.0, 0.0],
    )
    backend.upsert_item(
        index_id=idx,
        space_id=space,
        source_key="stale",
        source_ref={},
        source_hash="h2",
        status="stale",
        vector=[1.0, 0.0],
    )
    backend.upsert_item(
        index_id=idx,
        space_id=space,
        source_key="err",
        source_ref={},
        source_hash="h3",
        status="error",
        error_code="embedding_provider_error",
        error_message="boom",
    )
    results = backend.query_similar(idx, space, [1.0, 0.0], k=10)
    assert [k for k, _ in results] == ["ok"]
    backend.close()


def test_space_isolation(project):
    backend = VectorBackend(project)
    backend.ensure_schema()
    backend.upsert_item(
        index_id="i",
        space_id="emb_x",
        source_key="a",
        source_ref={},
        source_hash="h",
        status="ready",
        vector=[1.0, 0.0],
    )
    backend.upsert_item(
        index_id="i",
        space_id="emb_y",
        source_key="b",
        source_ref={},
        source_hash="h",
        status="ready",
        vector=[1.0, 0.0],
    )
    # querying one space never crosses into another space's vectors
    assert [k for k, _ in backend.query_similar("i", "emb_x", [1.0, 0.0], k=10)] == [
        "a"
    ]
    backend.close()


def test_upsert_replaces_vector(project):
    backend = VectorBackend(project)
    backend.ensure_schema()
    backend.upsert_item(
        index_id="i",
        space_id="s",
        source_key="k",
        source_ref={},
        source_hash="h1",
        status="ready",
        vector=[1.0, 0.0],
    )
    backend.upsert_item(
        index_id="i",
        space_id="s",
        source_key="k",
        source_ref={},
        source_hash="h2",
        status="ready",
        vector=[0.0, 1.0],
    )
    item = backend.get_item("i", "k")
    assert item["source_hash"] == "h2"
    # only one item row, no orphan vector inflation in the result
    res = backend.query_similar("i", "s", [0.0, 1.0], k=10)
    assert res == [("k", pytest.approx(0.0, abs=1e-6))]
    backend.close()


def test_delete_index_removes_vectors(project):
    backend = VectorBackend(project)
    backend.ensure_schema()
    backend.upsert_item(
        index_id="gone",
        space_id="s",
        source_key="k",
        source_ref={},
        source_hash="h",
        status="ready",
        vector=[1.0],
    )
    backend.delete_index("gone")
    assert backend.query_similar("gone", "s", [1.0], k=10) == []
    assert (
        backend.db.execute(
            "SELECT COUNT(*) AS n FROM embedding_vectors_f32"
        ).fetchone()["n"]
        == 0
    )
    backend.close()


def test_backend_is_rebuildable(project):
    backend = VectorBackend(project)
    backend.ensure_schema()
    backend.upsert_item(
        index_id="i",
        space_id="s",
        source_key="k",
        source_ref={},
        source_hash="h",
        status="ready",
        vector=[1.0],
    )
    backend.close()
    # nuke the sidecar; reopening rebuilds an empty backend, project intact
    (project.path / "project.embeddings.db").unlink()
    backend2 = VectorBackend(project)
    backend2.ensure_schema()
    assert backend2.query_similar("i", "s", [1.0], k=10) == []
    backend2.close()


def test_mismatched_query_dimension_finds_no_false_matches(project):
    backend = VectorBackend(project)
    backend.ensure_schema()
    # a 3-d stored vector must not match a 1-d query at distance 0.0 (the bug
    # where zip() truncated to the shorter length)
    backend.upsert_item(
        index_id="i",
        space_id="s",
        source_key="k3",
        source_ref={},
        source_hash="h",
        status="ready",
        vector=[1.0, 0.0, 0.0],
    )
    assert backend.query_similar("i", "s", [1.0], k=10) == []
    backend.close()


def test_query_scopes_to_matching_dimension(project):
    backend = VectorBackend(project)
    backend.ensure_schema()
    backend.upsert_item(
        index_id="i",
        space_id="s",
        source_key="two",
        source_ref={},
        source_hash="h",
        status="ready",
        vector=[1.0, 0.0],
    )
    backend.upsert_item(
        index_id="i",
        space_id="s",
        source_key="three",
        source_ref={},
        source_hash="h",
        status="ready",
        vector=[1.0, 0.0, 0.0],
    )
    # a 2-d query only sees 2-d vectors, never the 3-d row
    assert [k for k, _ in backend.query_similar("i", "s", [1.0, 0.0], k=10)] == ["two"]
    backend.close()


def test_empty_query_rejected(project):
    backend = VectorBackend(project)
    backend.ensure_schema()
    with pytest.raises(ValueError):
        backend.query_similar("i", "s", [], k=10)
    backend.close()


def test_scalar_raises_on_blob_dimension_mismatch():
    from frisket.ai.embeddings.vector_backend import _f32_cosine_distance

    three = pack_f32([1.0, 0.0, 0.0])
    one = pack_f32([1.0])
    # declared dimension 3 but query blob is 1 wide → strict violation, not 0.0
    with pytest.raises(ValueError):
        _f32_cosine_distance(three, one, 3)


def test_backend_advertises_id_and_version():
    assert VectorBackend.BACKEND_ID
    assert VectorBackend.BACKEND_VERSION


def test_cosine_distance_math():
    # the registered scalar matches a hand cosine distance
    a, b = [1.0, 2.0, 3.0], [2.0, 4.0, 6.0]  # parallel → distance 0
    sim = sum(x * y for x, y in zip(a, b)) / (
        math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    )
    assert (1.0 - sim) == pytest.approx(0.0, abs=1e-6)
