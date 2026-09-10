"""Local embedding backend (base install → fastembed/ONNX).

The integration test runs the REAL bge-small model end-to-end through the HTTP
endpoint — no stubs anywhere — so it proves actual learned semantics: 'car
trouble' ranks the 'automobile' row with zero shared keywords. These real-model
proofs carry the network marker because a cold cache downloads weights; the
Docker image pre-downloads the model.

The cache tests pin the sidecar vector-cache contract with a counting stub:
corpus embedded once, queries embed only themselves, content-addressed keys.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from frisket.ai.llm import ModelRouter
from frisket.semantic import local_embedder, semantic_search
from frisket.server.app import create_app
from frisket.engine.store import Project

_needs_real_local = pytest.mark.skipif(
    local_embedder() is None,
    reason="bundled local embedder unavailable or FRISKET_DISABLE_LOCAL_EMBED=1",
)


@pytest.mark.network
@pytest.mark.real
@_needs_real_local
def test_real_model_ranks_by_meaning_end_to_end(tmp_path):
    # keyless router: no API embedding backend — the LOCAL model must carry it
    router = ModelRouter(cache=None, cache_mode="off")
    assert not router.has_embedding_backend()
    client = TestClient(create_app(tmp_path / "ws", router=router))
    pid = client.post("/api/projects", json={"name": "Sem"}).json()["id"]
    csv = (
        "note\n"
        "the automobile would not start this morning\n"
        "she planted tomatoes in the garden\n"
    )
    client.post(
        f"/api/projects/{pid}/import/csv", files={"file": ("n.csv", csv, "text/csv")}
    )
    r = client.get(
        f"/api/projects/{pid}/search", params={"q": "car trouble", "mode": "semantic"}
    )
    assert r.status_code == 200, r.text
    hits = r.json()
    assert hits and hits[0]["semantic"] is True
    assert "automobile" in hits[0]["snip"].lower(), (
        f"real model failed to rank the semantic match first: {hits}"
    )
    # out-of-fixture query — the class of input the old synonym-table fake
    # returned [] for. A learned model must still rank by meaning.
    r = client.get(
        f"/api/projects/{pid}/search",
        params={"q": "growing vegetables", "mode": "semantic"},
    )
    hits = r.json()
    assert hits and "tomatoes" in hits[0]["snip"].lower(), (
        f"out-of-lexicon query not meaning-ranked: {hits}"
    )
    # cross-lingual (the model is multilingual by design): a Spanish and a
    # Japanese query must rank the English 'automobile' row first.
    for q in ("el coche está averiado", "問題のある車"):
        r = client.get(
            f"/api/projects/{pid}/search", params={"q": q, "mode": "semantic"}
        )
        hits = r.json()
        assert hits and "automobile" in hits[0]["snip"].lower(), (
            f"cross-lingual query {q!r} not meaning-ranked: {hits}"
        )


@pytest.mark.network
@pytest.mark.real
@_needs_real_local
def test_local_embedder_resolves():
    backend = local_embedder()
    assert backend is not None
    embed, model_id = backend
    assert model_id.startswith("fastembed/")
    vecs = embed(["hello", "world"])
    assert len(vecs) == 2 and len(vecs[0]) > 100  # real dense vectors


class _CountingEmbedder:
    """Deterministic stub that counts every text it is asked to embed."""

    def __init__(self):
        self.embedded: list[str] = []

    def __call__(self, texts: list[str]) -> list[list[float]]:
        self.embedded.extend(texts)
        # arbitrary-but-deterministic vectors; never zero
        return [
            [1.0, float(len(t) % 7 + 1), float(sum(map(ord, t)) % 11 + 1)]
            for t in texts
        ]


def _seed_project(tmp_path) -> Project:
    p = Project.create(tmp_path / "t.frisket", name="t")
    sheet = p.add_sheet("data")
    cols = {"note": p.add_column(sheet, "note")}
    p.add_rows(sheet, [{"note": "alpha bravo"}, {"note": "charlie delta"}], cols)
    return p


def test_vector_cache_embeds_corpus_once(tmp_path):
    p = _seed_project(tmp_path)
    emb = _CountingEmbedder()
    semantic_search(p, "first query", embed=emb, embed_id="stub/v1")
    first_pass = len(emb.embedded)
    assert first_pass == 3  # query + 2 cells

    semantic_search(p, "second query", embed=emb, embed_id="stub/v1")
    # second query embeds ONLY itself; corpus vectors come from the sidecar
    assert len(emb.embedded) == first_pass + 1, (
        f"corpus was re-embedded: {emb.embedded}"
    )
    p.close()


def test_vector_cache_is_model_scoped(tmp_path):
    p = _seed_project(tmp_path)
    a, b = _CountingEmbedder(), _CountingEmbedder()
    semantic_search(p, "q", embed=a, embed_id="stub/v1")
    semantic_search(p, "q", embed=b, embed_id="stub/v2")
    # a different model id must NOT reuse v1's vectors (stale-space bug)
    assert len(b.embedded) == 3, "model switch served stale cached vectors"
    p.close()
