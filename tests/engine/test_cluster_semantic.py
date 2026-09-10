"""Semantic dedupe algorithm: `ops.cluster.compute_semantic_clusters`.

The stub-embedder tests pin the algorithm + the panel JSON contract: semantic
clusters land in the EXACT shape cluster_fingerprint.compute_clusters emits
({key, canonical, size, values:[{value,count}], row_ids}). The cache test pins
the no-re-embed rule (vectors come from the shared content-addressed sidecar);
the cap test pins the O(n²) bound; the network-marked FastEmbed test proves a
real model groups meaning-level duplicates fingerprinting can never collide.

The retired `run_cluster` wrapper had a silent fingerprint fallback. The
no-fallback contract is enforced by
`preview.cluster.compute_method_clusters` (semantic with no embedder ->
`embedding_backend_unavailable`), tested in tests/preview/test_cluster_preview.py.
"""

from __future__ import annotations

import json

import pytest

from cluster_fingerprint_known_pairs import JON_SMITH_COLLISION_PAIR
from frisket.ops.cluster import compute_semantic_clusters
from frisket.ops.cluster_fingerprint import compute_clusters
from frisket.engine.store import Project

# JON_SMITH_COLLISION_PAIR is the shared single source of truth for this
# exact collision claim. It is also used by
# web/tests/e2e/cluster-resolve-v1-execution.spec.ts and cross-checked against
# the real fingerprint() in
# tests/test_cluster_fingerprint_known_pairs.py.
_JON_SMITH, _SMITH_JON = JON_SMITH_COLLISION_PAIR

# Hand-built unit-ish vectors with known pairwise cosines:
#   ACME Corp        · Acme Corporation  = 0.98
#   ACME Corp        · ACME Inc.         = 0.95
#   Acme Corporation · ACME Inc.         ≈ 0.993
#   Banana Farms     · Banana Farms LLC  = 0.995
#   every ACME ·  every Banana           = 0.0
VECS: dict[str, list[float]] = {
    "ACME Corp": [1.0, 0.0, 0.0],
    "Acme Corporation": [0.98, 0.199, 0.0],
    "ACME Inc.": [0.95, 0.3122, 0.0],
    "Banana Farms": [0.0, 0.0, 1.0],
    "Banana Farms LLC": [0.0, 0.0995, 0.995],
    # near-orthogonal to both groups; near each other (cosine ≈ 0.995)
    _JON_SMITH: [0.0, 1.0, 0.0],
    _SMITH_JON: [0.0, 0.995, 0.0995],
}


class StubEmbedder:
    """Deterministic lookup embedder that counts every text it embeds."""

    def __init__(self, vecs: dict[str, list[float]] | None = None):
        self.vecs = vecs or VECS
        self.embedded: list[str] = []

    def __call__(self, texts: list[str]) -> list[list[float]]:
        self.embedded.extend(texts)
        return [self.vecs[t] for t in texts]


def _seed(tmp_path, values: list[str], name: str = "t") -> tuple[Project, int]:
    p = Project.create(tmp_path / f"{name}.frisket", name=name)
    sheet = p.add_sheet("data")
    cols = {"org": p.add_column(sheet, "org")}
    p.add_rows(sheet, [{"org": v} for v in values], cols)
    return p, sheet


# "ACME Corp" x3, "Banana Farms" x2 — frequency drives canonical + cap order
ROWS = [
    "ACME Corp",
    "ACME Corp",
    "ACME Corp",
    "Acme Corporation",
    "ACME Inc.",
    "Banana Farms",
    "Banana Farms",
    "Banana Farms LLC",
]


def _semantic(p, sid, **kw):
    """compute_semantic_clusters with the stub embedder unless one is passed."""
    embed = kw.pop("embed", StubEmbedder())
    embed_id = kw.pop("embed_id", "stub/v1")
    return compute_semantic_clusters(p, sid, "org", embed, embed_id, **kw)


def test_semantic_groups_by_pairwise_cosine(tmp_path):
    p, sid = _seed(tmp_path, ROWS)
    clusters, _distinct, _considered = _semantic(p, sid)
    assert len(clusters) == 2
    acme, banana = clusters  # largest-first
    assert acme["canonical"] == "ACME Corp"  # most frequent surface wins
    assert [v["value"] for v in acme["values"]] == [
        "ACME Corp",
        "ACME Inc.",
        "Acme Corporation",
    ]
    assert acme["size"] == 5 and acme["row_ids"] == sorted(acme["row_ids"])
    assert sum(v["count"] for v in acme["values"]) == acme["size"]
    assert banana["canonical"] == "Banana Farms" and banana["size"] == 3
    # fingerprinting can NOT collide any of these (no shared token keys) —
    # semantic finds what the default method misses
    assert compute_clusters(p, sid, "org") == []
    p.close()


def test_panel_contract_matches_fingerprint_shape(tmp_path):
    """The acceptance bar: semantic output is shape-identical to the fingerprint
    method, so the panel/receipt render it with zero changes."""
    p, sid = _seed(tmp_path, ROWS + JON_SMITH_COLLISION_PAIR)
    fp = compute_clusters(p, sid, "org")  # fingerprint method
    sem, _distinct, _considered = _semantic(p, sid)
    assert len(fp) == 1  # Jon Smith / Smith, Jon collide by fingerprint
    fp_cluster, sem_cluster = fp[0], sem[0]
    assert set(fp_cluster) == set(sem_cluster)
    for c in (fp_cluster, sem_cluster):
        assert isinstance(c["key"], str) and isinstance(c["canonical"], str)
        assert isinstance(c["size"], int) and isinstance(c["row_ids"], list)
        assert all(set(v) == {"value", "count"} for v in c["values"])
    # round-trips as JSON, like the HTTP layer will serve it
    json.dumps(sem)
    p.close()


def test_threshold_is_spec_overridable(tmp_path):
    p, sid = _seed(tmp_path, ROWS)
    clusters, _distinct, _considered = _semantic(p, sid, threshold=0.99)
    # at 0.99 only the 0.993 / 0.995 pairs survive; "ACME Corp" drops out
    grouped = [{v["value"] for v in c["values"]} for c in clusters]
    assert {"Acme Corporation", "ACME Inc."} in grouped
    assert {"Banana Farms", "Banana Farms LLC"} in grouped
    assert all("ACME Corp" not in g for g in grouped)
    p.close()


def test_vectors_cached_never_reembedded(tmp_path):
    """Second run embeds NOTHING — vectors come from the shared
    content-addressed sidecar cache (sha1(model_id + content))."""
    p, sid = _seed(tmp_path, ROWS)
    emb = StubEmbedder()
    compute_semantic_clusters(p, sid, "org", emb, "stub/v1")
    first = len(emb.embedded)
    assert first == 5  # one embed per DISTINCT value, not per row
    compute_semantic_clusters(p, sid, "org", emb, "stub/v1")
    assert len(emb.embedded) == first, f"re-embedded: {emb.embedded[first:]}"
    p.close()


def test_value_cap_bounds_pairwise_pass(tmp_path, monkeypatch):
    """O(n²) bound: only the cap's worth of most-frequent distinct values is
    embedded/compared; the return reports the truncation."""
    monkeypatch.setenv("FRISKET_CLUSTER_SEMANTIC_CAP", "3")
    p, sid = _seed(tmp_path, ROWS)
    emb = StubEmbedder()
    clusters, distinct, considered = compute_semantic_clusters(
        p, sid, "org", emb, "s/1"
    )
    # kept: ACME Corp (3 rows), Banana Farms (2), then lexicographic tie-break
    assert sorted(emb.embedded) == ["ACME Corp", "ACME Inc.", "Banana Farms"]
    assert distinct == 5 and considered == 3
    assert len(clusters) == 1  # the two kept ACMEs pair; Banana is a singleton
    assert {v["value"] for v in clusters[0]["values"]} == {"ACME Corp", "ACME Inc."}
    p.close()


def test_default_semantic_clustering_considers_501_distinct_values(
    tmp_path, monkeypatch
):
    """Embedding batches stay bounded without becoming a total-value cap."""
    monkeypatch.delenv("FRISKET_CLUSTER_SEMANTIC_CAP", raising=False)
    values = [f"organization-{index:03d}" for index in range(501)]
    project, sheet_id = _seed(tmp_path, values, name="semantic-501")

    class UniformEmbedder:
        def __init__(self) -> None:
            self.calls: list[list[str]] = []

        def __call__(self, texts: list[str]) -> list[list[float]]:
            self.calls.append(list(texts))
            return [[1.0, 0.0] for _text in texts]

    embedder = UniformEmbedder()
    clusters, distinct, considered = compute_semantic_clusters(
        project,
        sheet_id,
        "org",
        embedder,
        "uniform/v1",
    )

    assert distinct == 501
    assert considered == 501
    assert len(embedder.calls) > 1
    assert max(map(len, embedder.calls)) <= 500
    assert [text for call in embedder.calls for text in call] == values
    assert len(clusters) == 1
    assert clusters[0]["size"] == 501
    assert len(clusters[0]["values"]) == 501
    assert len(clusters[0]["row_ids"]) == 501
    project.close()


def test_min_size_filters_semantic_singletons(tmp_path):
    p, sid = _seed(tmp_path, ["ACME Corp", "Banana Farms"])
    clusters, _distinct, _considered = _semantic(p, sid)
    assert clusters == []
    p.close()


@pytest.mark.network
@pytest.mark.real
def test_real_model_groups_meaning_duplicates(tmp_path):
    """End-to-end with the bundled real local embedder:
    name variants cluster, the unrelated value stays out — at the default
    threshold, with no stubs."""
    from frisket.semantic import resolve_embedder

    backend = resolve_embedder()
    if backend is None:  # FRISKET_DISABLE_LOCAL_EMBED set in the environment
        pytest.skip("local embedder disabled")
    p, sid = _seed(
        tmp_path,
        [
            "World Health Organization",
            "the World Health Organization (WHO)",
            "banana bread recipe",
        ],
    )
    clusters, _distinct, _considered = compute_semantic_clusters(
        p, sid, "org", backend[0], backend[1]
    )
    grouped = [{v["value"] for v in c["values"]} for c in clusters]
    assert {
        "World Health Organization",
        "the World Health Organization (WHO)",
    } in grouped
    assert all("banana bread recipe" not in g for g in grouped)
    p.close()
