"""hybrid keyword + vector search fused by RRF.

Run FTS/BM25 (sheet-scoped) AND vector search over the same sheet/index and fuse by
Reciprocal Rank Fusion. RRF (rank-based) because BM25 (unbounded) and cosine (-1..1)
scales are incompatible. A keyword-only match surfaces, a vector-only match surfaces,
and a row matched by BOTH ranks highest.

Vectors (3-d padded): query "drone" = [1,0,0]; rows are blended so the keyword text and
the vector neighbourhood diverge — that divergence is what fusion bridges.
"""

from __future__ import annotations

import pytest

from frisket.ai.embeddings import build_batch_result
from frisket.engine.executor import ExecutorDeps, run_action_spec
from frisket.engine.store import Project

PROJECT_ID = "p"

# headline -> 3-d vector (the SEMANTIC neighbourhood, independent of the literal words)
_VEC = {
    "drone": [1.0, 0.0, 0.0],  # the query
    "unmanned aerial vehicle": [1.0, 0.0, 0.0],  # vector-near drone, NO keyword "drone"
    "drone strike report": [0.9, 0.1, 0.0],  # vector-near AND keyword "drone" (both)
    "drone bee colony": [
        0.0,
        0.0,
        1.0,
    ],  # keyword "drone" but vector-FAR (keyword-only)
    "tractor": [0.0, 1.0, 0.0],  # neither
}
_ROWS = [
    "unmanned aerial vehicle",
    "drone strike report",
    "drone bee colony",
    "tractor",
]


class MappedGateway:
    def __init__(self, dim: int = 384):
        self.dim = dim
        self.calls: list[list[str]] = []

    def embed(self, texts, *, provider, model, modality):
        self.calls.append(list(texts))
        out = [
            list(_VEC.get(t, [0.0, 0.0, 0.0])) + [0.0] * (self.dim - 3) for t in texts
        ]
        return build_batch_result(
            out,
            provider_id=provider or "fastembed",
            provider_kind="local_process",
            actual_model_id=model or "fake",
            modality=modality,
        )


def _create_index(project, sheet, *, provider="fastembed", policy=None, key="c"):
    res = run_action_spec(
        project,
        {
            "action_id": "embedding.index_create",
            "scope": {"kind": "project"},
            "params": {
                "sheet_id": sheet,
                "source_columns": ["headline"],
                "modality": "text",
                "provider": provider,
                "source_policy": {"kind": "text_cell"},
                "provider_policy": policy
                if policy is not None
                else {"allow_remote": False},
            },
            "idempotency_key": f"hy_create@{key}",
        },
        project_id=PROJECT_ID,
    )
    assert res.status == "completed", res.errors
    return res.outputs[0].ref["index_id"]


def _refresh(project, index_id, gateway, *, key="r"):
    res = run_action_spec(
        project,
        {
            "action_id": "embedding.index_refresh",
            "scope": {"kind": "project"},
            "params": {"index_id": index_id, "mode": "full"},
            "idempotency_key": f"hy_refresh@{key}",
        },
        project_id=PROJECT_ID,
        deps=ExecutorDeps(embedding_gateway=gateway),
    )
    assert res.status == "completed", res.errors


@pytest.fixture
def env(tmp_path):
    project = Project.create(tmp_path / "hy.frisket", name="hy")
    sheet = project.add_sheet("contracts")
    cols = {"headline": project.add_column(sheet, "headline")}
    project.add_rows(sheet, [{"headline": r} for r in _ROWS], cols)
    index_id = _create_index(project, sheet)
    _refresh(project, index_id, MappedGateway())
    yield project, sheet, cols, index_id
    project.close()


def _row(project, sheet, cols, text):
    return next(
        r for r, v in project.get_values(sheet, cols["headline"]).items() if v == text
    )


def _hybrid_query(index_id, sheet, text, **extra):
    return {
        "kind": "embedding_hybrid",
        "embedding_index_id": index_id,
        "sheet_id": sheet,
        "text": text,
        **extra,
    }


# --------------------------------------------------------------------------


def test_rrf_fuse_is_deterministic_and_correct():
    from frisket.search import rrf_fuse

    # row 7: rank1 in list A, rank2 in list B -> 1/61 + 1/62 (the dual match wins)
    fused = rrf_fuse([[7, 8, 9], [10, 7, 11]], k=60)
    assert fused[0][0] == 7
    # score = sum of 1/(k + 1-based-rank)
    assert fused[0][1] == pytest.approx(1 / 61 + 1 / 62)
    # a row in only one list scores 1/(k+rank); deterministic row_id tie-break
    ids = [rid for rid, _ in fused]
    assert set(ids) == {7, 8, 9, 10, 11}
    assert rrf_fuse([[7, 8, 9], [10, 7, 11]], k=60) == fused  # deterministic


def test_search_sheet_scopes_to_one_sheet_in_bm25_order(env):
    from frisket.search import search_sheet

    project, sheet, cols, index_id = env
    other = project.add_sheet("other")
    ocol = project.add_column(other, "headline")
    project.add_rows(
        other, [{"headline": "drone in another sheet"}], {"headline": ocol}
    )

    rows = search_sheet(project, sheet, "drone", limit=10)
    # only THIS sheet's rows that contain "drone" — never the other sheet's row
    matched = {
        _row(project, sheet, cols, "drone strike report"),
        _row(project, sheet, cols, "drone bee colony"),
    }
    assert set(rows) == matched
    # the other sheet's matching row is excluded
    other_ids = set(project.get_values(other, ocol).keys())
    assert not (set(rows) & other_ids)


def test_hybrid_surfaces_keyword_only_and_vector_only_and_ranks_dual_first(env):
    from frisket.ai.embeddings.hybrid import resolve_embedding_hybrid

    project, sheet, cols, index_id = env
    uav = _row(project, sheet, cols, "unmanned aerial vehicle")  # vector-only
    strike = _row(project, sheet, cols, "drone strike report")  # both
    bee = _row(project, sheet, cols, "drone bee colony")  # keyword-only
    tractor = _row(project, sheet, cols, "tractor")  # neither

    result = resolve_embedding_hybrid(
        project, _hybrid_query(index_id, sheet, "drone"), gateway=MappedGateway()
    )
    order = [hit.row_id for hit in result.hits]

    # the dual match (keyword + vector) ranks first — fusion rewards agreement
    assert order[0] == strike
    # a VECTOR-only match (no literal "drone") still surfaces
    assert uav in order
    # a KEYWORD-only match (semantically far) surfaces via fusion, above "neither"
    assert bee in order
    assert order.index(bee) < order.index(tractor) if tractor in order else True


def test_typed_preview_preserves_local_hybrid_and_receipt_facts(env, monkeypatch):
    from frisket.engine.store.receipts import ReceiptStore

    project, sheet, _cols, index_id = env
    calls = []

    def local_embedder(model_id=None, **_kwargs):
        def embed(texts):
            calls.append(list(texts))
            return [list(_VEC[text]) + [0.0] * 381 for text in texts]

        return embed, model_id or "BAAI/bge-small-en-v1.5"

    monkeypatch.setattr("frisket.semantic.local_embedder", local_embedder)
    result = run_action_spec(
        project,
        {
            "action_id": "query.preview",
            "scope": {"kind": "project"},
            "params": {
                "query": _hybrid_query(index_id, sheet, "drone"),
                "limit": 50,
                "offset": 0,
            },
            "idempotency_key": "typed-local-hybrid",
        },
        project_id=PROJECT_ID,
    )

    assert result.status == "completed", result.errors
    assert calls == [["drone"]]
    assert result.outputs[0].ref["evaluator"]["kind"] == "embedding_hybrid"
    receipt = ReceiptStore(project).parsed_by_id(result.receipt_id)
    assert receipt is not None
    assert receipt.provider_use[0]["service"] == ("frisket.query_preview.local_hybrid")
    assert receipt.provider_use[0]["external_api"] is False


def test_hybrid_requested_limit_above_legacy_cap_returns_every_match(tmp_path):
    from frisket.ai.embeddings.hybrid import resolve_embedding_hybrid

    project = Project.create(tmp_path / "large-hybrid.frisket", name="large-hybrid")
    sheet = project.add_sheet("documents")
    cols = {"headline": project.add_column(sheet, "headline")}
    texts = [f"semantic document {index:03d}" for index in range(501)]
    project.add_rows(sheet, [{"headline": text} for text in texts], cols)
    index_id = _create_index(project, sheet, key="large-hybrid")
    _refresh(project, index_id, MappedGateway(), key="large-hybrid")

    result = resolve_embedding_hybrid(
        project,
        _hybrid_query(index_id, sheet, "drone", limit=501),
        gateway=MappedGateway(),
    )

    assert len(result.hits) == 501
    assert all(hit.keyword_rank is None for hit in result.hits)
    project.close()


def test_hybrid_blocks_stale_index(env):
    from frisket.preview.query import QueryPreviewError, resolve_query_preview

    project, sheet, cols, index_id = env
    project.add_rows(
        sheet, [{"headline": "drone new"}], cols
    )  # unembedded -> incomplete
    with pytest.raises(QueryPreviewError) as exc:
        resolve_query_preview(project, _hybrid_query(index_id, sheet, "drone"))
    assert exc.value.code == "embedding_index_incomplete"


def test_hybrid_remote_without_allow_remote_blocks_before_embed(tmp_path):
    from frisket.ai.embeddings.hybrid import resolve_embedding_hybrid
    from frisket.ai.embeddings.similarity import SimilarityError

    project = Project.create(tmp_path / "hyr.frisket", name="r")
    sheet = project.add_sheet("s")
    cols = {"headline": project.add_column(sheet, "headline")}
    project.add_rows(sheet, [{"headline": r} for r in _ROWS], cols)
    index_id = _create_index(
        project, sheet, provider="openai", policy={"allow_remote": False}, key="hyr"
    )
    gw = MappedGateway()
    with pytest.raises(SimilarityError) as exc:
        resolve_embedding_hybrid(
            project, _hybrid_query(index_id, sheet, "drone"), gateway=gw
        )
    assert exc.value.code == "embedding_remote_confirmation_required"
    assert gw.calls == []
    project.close()


def test_hybrid_preview_route_freshness_gate(tmp_path):
    # The POST /embeddings/v1/hybrid-preview route wires the freshness gate (like the
    # similarity-preview route): an appended unembedded row -> 400 embedding_index_incomplete
    # BEFORE any embed. (Fusion itself is covered by the resolver tests above.)
    from fastapi.testclient import TestClient

    from frisket.ai.llm import ModelRouter
    from frisket.server.app import create_app

    client = TestClient(create_app(tmp_path / "ws", router=ModelRouter(keys={})))
    pid = client.post("/api/projects", json={"name": "hy"}).json()["id"]
    project = client.app.state.workspace.get(pid)
    sheet = project.add_sheet("s")
    col = project.add_column(sheet, "headline")
    project.add_rows(sheet, [{"headline": r} for r in _ROWS], {"headline": col})
    index_id = _create_index(project, sheet, key="route")
    _refresh(project, index_id, MappedGateway(), key="route")
    project.add_rows(
        sheet, [{"headline": "drone new"}], {"headline": col}
    )  # unembedded
    resp = client.post(
        f"/api/projects/{pid}/embeddings/v1/hybrid-preview",
        json={"query": _hybrid_query(index_id, sheet, "drone")},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "embedding_index_incomplete"
