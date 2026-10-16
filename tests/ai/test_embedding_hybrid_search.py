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
from frisket.retrieval.contracts import SourceLocator
from frisket.retrieval.source_adapter import resolve_cell_text_sources
from frisket.retrieval.store import (
    BindingDependency,
    LexicalInput,
    RetrievalStore,
    SourcePublication,
    StoredBinding,
)

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


def _publish_lexical_cells(project, sheet, column, row_ids):
    resolved = resolve_cell_text_sources(
        project,
        sheet_id=sheet,
        column_id=column,
        row_ids=row_ids,
    )
    with RetrievalStore(project) as retrieval:
        for row_id, source in resolved.items():
            binding = source.binding
            retrieval.begin_preparation(
                source.source, recipe_id="test-lexical-v1", channel="lexical"
            )
            assert retrieval.publish_source(
                SourcePublication(
                    source.source,
                    "test-lexical-v1",
                    "lexical",
                    parts=(),
                    bindings=(
                        StoredBinding(
                            f"lexical:{sheet}:{row_id}:{column}",
                            sheet,
                            row_id,
                            column,
                            (
                                BindingDependency(
                                    row_id,
                                    column,
                                    binding.retrieval_revision,
                                ),
                            ),
                        ),
                    ),
                    lexical=LexicalInput(
                        source.lexical_text, SourceLocator(kind="text")
                    ),
                )
            )


@pytest.fixture
def env(tmp_path):
    project = Project.create(tmp_path / "hy.frisket", name="hy")
    sheet = project.add_sheet("contracts")
    cols = {"headline": project.add_column(sheet, "headline")}
    project.add_rows(sheet, [{"headline": r} for r in _ROWS], cols)
    index_id = _create_index(project, sheet)
    _refresh(project, index_id, MappedGateway())
    _publish_lexical_cells(
        project,
        sheet,
        cols["headline"],
        project.visible_row_ids(sheet),
    )
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


def test_hybrid_refuses_lexical_only_result_without_vector_runtime(
    env, tmp_path, monkeypatch: pytest.MonkeyPatch
):
    from frisket.ai.embeddings.hybrid import resolve_embedding_hybrid
    from frisket.ai.embeddings.similarity import SimilarityError
    from frisket.retrieval import native

    project, sheet, _cols, index_id = env
    monkeypatch.setattr(native, "_NATIVE_ROOT", tmp_path / "missing-native")

    with pytest.raises(SimilarityError) as exc:
        resolve_embedding_hybrid(
            project, _hybrid_query(index_id, sheet, "drone"), gateway=MappedGateway()
        )

    assert exc.value.code == "embedding_backend_unavailable"


def test_hybrid_route_service_resolves_inside_source_snapshot(env, monkeypatch):
    from types import SimpleNamespace
    from frisket.server.services import embeddings

    project, sheet, _cols, index_id = env
    monkeypatch.setattr(
        embeddings, "EmbeddingGateway", lambda **kwargs: MappedGateway()
    )
    service = embeddings.EmbeddingRouteService(
        SimpleNamespace(
            get=lambda _id: project,
            router_for=lambda _project: None,
        )
    )
    result = service.hybrid_preview(PROJECT_ID, _hybrid_query(index_id, sheet, "drone"))
    assert result["sheet_id"] == sheet


def test_typed_preview_preserves_local_hybrid_and_receipt_facts(env, monkeypatch):
    from frisket.engine.store.receipts import ReceiptStore

    project, sheet, _cols, index_id = env
    calls = []

    def local_embedder(model_id=None, **_kwargs):
        def embed(texts):
            calls.append(list(texts))
            return [list(_VEC[text]) + [0.0] * 381 for text in texts]

        return embed, model_id or "BAAI/bge-small-en-v1.5"

    monkeypatch.setattr("frisket.ai.embeddings.backends.local_embedder", local_embedder)
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


def test_hybrid_saved_limit_above_public_cap_remains_valid(tmp_path):
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
        _hybrid_query(index_id, sheet, "drone", limit=750),
        gateway=MappedGateway(),
    )

    assert len(result.hits) == 501
    assert all(hit.keyword_rank is None for hit in result.hits)
    project.close()


def test_hybrid_preserves_fts_boolean_syntax(env):
    from frisket.ai.embeddings.hybrid import resolve_embedding_hybrid

    project, sheet, cols, index_id = env
    _VEC["drone OR tractor"] = [1.0, 0.0, 0.0]
    try:
        result = resolve_embedding_hybrid(
            project,
            _hybrid_query(index_id, sheet, "drone OR tractor"),
            gateway=MappedGateway(),
        )
    finally:
        _VEC.pop("drone OR tractor", None)

    tractor = _row(project, sheet, cols, "tractor")
    tractor_hit = next(hit for hit in result.hits if hit.row_id == tractor)
    assert tractor_hit.vector_rank is not None
    assert tractor_hit.keyword_rank is not None


def test_hybrid_keyword_channel_searches_whole_sheet(env):
    from frisket.ai.embeddings.hybrid import resolve_embedding_hybrid

    project, sheet, cols, index_id = env
    keyword_only = _row(project, sheet, cols, "drone bee colony")
    with RetrievalStore(project) as retrieval:
        retrieval.db.execute(
            "DELETE FROM named_dataset_membership "
            "WHERE dataset_id=? AND part_key IN ("
            "SELECT part.part_key FROM parts AS part JOIN bindings AS binding "
            "ON binding.source_key=part.source_key WHERE binding.row_id=?"
            ")",
            (index_id, keyword_only),
        )

    result = resolve_embedding_hybrid(
        project, _hybrid_query(index_id, sheet, "drone"), gateway=MappedGateway()
    )

    hit = next(hit for hit in result.hits if hit.row_id == keyword_only)
    assert hit.vector_rank is None
    assert hit.keyword_rank is not None


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
