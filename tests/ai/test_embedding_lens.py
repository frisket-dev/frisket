"""Saved similarity Views share selection, ranking and prepared-vector reuse."""

from __future__ import annotations
from frisket.execution.purchases import LOCAL_PURCHASE_POLICY


from fastapi.testclient import TestClient

from frisket.ai.embeddings import build_batch_result
from frisket.engine.executor import ExecutorDeps, run_action_spec
from frisket.ai.llm import ModelRouter
from frisket.server.app import create_app

_VECTORS = {
    "cat": [1.0, 0.0, 0.0],
    "feline": [1.0, 0.0, 0.0],
    "kitten": [0.8, 0.6, 0.0],
    "airplane": [0.0, 0.0, 1.0],
}


class _Gateway:
    def embed(self, texts, *, provider, model, modality):
        out = [list(_VECTORS.get(t, [0.0, 0.0, 0.0])) + [0.0] * 381 for t in texts]
        return build_batch_result(
            out,
            provider_id=provider or "fastembed",
            provider_kind="local_process",
            actual_model_id=model or "fake",
            modality=modality,
        )


def _client(tmp_path):
    return TestClient(
        create_app(
            tmp_path / "ws",
            router=ModelRouter(model_call_policy=LOCAL_PURCHASE_POLICY, keys={}),
        )
    )


def _seed(client):
    pid = client.post("/api/projects", json={"name": "x"}).json()["id"]
    project = client.app.state.workspace.get(pid)
    sheet = project.add_sheet("s")
    col = project.add_column(sheet, "headline")
    project.add_rows(
        sheet,
        [{"headline": t} for t in ("cat", "kitten", "airplane")],
        {"headline": col},
    )
    return pid, project, sheet, col


def _create(project, pid, sheet):
    r = run_action_spec(
        project,
        {
            "action_id": "embedding.index_create",
            "scope": {"kind": "project"},
            "params": {
                "sheet_id": sheet,
                "source_columns": ["headline"],
                "modality": "text",
                "provider": "fastembed",
                "source_policy": {"kind": "text_cell"},
                "provider_policy": {"allow_remote": False},
            },
            "idempotency_key": "c@1",
        },
        project_id=pid,
    )
    return r.outputs[0].ref["index_id"]


def _refresh(project, pid, index_id, key="r@1"):
    run_action_spec(
        project,
        {
            "action_id": "embedding.index_refresh",
            "scope": {"kind": "project"},
            "params": {"index_id": index_id, "mode": "full"},
            "idempotency_key": key,
        },
        project_id=pid,
        deps=ExecutorDeps(embedding_gateway=_Gateway()),
    )


def _row(project, sheet, col, text):
    return next(r for r, v in project.get_values(sheet, col).items() if v == text)


def _setup(client):
    pid, project, sheet, col = _seed(client)
    index_id = _create(project, pid, sheet)
    _refresh(project, pid, index_id)
    return pid, project, sheet, col, index_id


def save_view(client, pid, sheet, index_id, anchor, *, limit=None, search=None):
    selection = {
        "scope": {"kind": "sheet", "sheet_id": sheet},
        "similarity": {
            "kind": "embedding_similarity",
            "scope": {"kind": "sheet", "sheet_id": sheet},
            "embedding_index_id": index_id,
            "anchor": anchor,
        },
        "limit": limit,
    }
    if search:
        selection.update(search={"text": search}, ranking="hybrid")
    return client.post(
        f"/api/projects/{pid}/views",
        json={"name": "Similar stories", "selection": selection},
    )


def test_saved_similarity_view_preserves_ranking_and_uses_no_provider(tmp_path):
    client = _client(tmp_path)
    pid, project, sheet, col, index = _setup(client)
    cat = _row(project, sheet, col, "cat")
    for anchor in (
        {"kind": "row", "row_id": cat},
        {"kind": "row_cell", "row_id": cat, "column_id": col},
    ):
        saved = save_view(client, pid, sheet, index, anchor)
        assert saved.status_code == 200, saved.text
        view = saved.json()
        assert view["spec"]["selection"]["similarity"]["anchor"]["row_id"] == cat
        opened = client.get(f"/api/projects/{pid}/views/{view['id']}/resolve")
        assert opened.status_code == 200, opened.text
        refs = opened.json()["refs"]
        assert [ref["row_id"] for ref in refs] == [
            _row(project, sheet, col, "kitten"),
            _row(project, sheet, col, "airplane"),
        ]
        assert refs[0]["similarity_score"] > refs[1]["similarity_score"]
    assert all(
        row["kind"] != "lens" for row in project.db.execute("SELECT kind FROM ops")
    )


def test_stale_and_incomplete_view_is_unavailable_until_refresh(tmp_path):
    client = _client(tmp_path)
    pid, project, sheet, col, index = _setup(client)
    cat = _row(project, sheet, col, "cat")
    view = save_view(client, pid, sheet, index, {"kind": "row", "row_id": cat}).json()
    project.add_rows(sheet, [{"headline": "new"}], {"headline": col})
    response = client.get(f"/api/projects/{pid}/views/{view['id']}/resolve")
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "embedding_index_incomplete"


def test_manual_query_preparation_is_explicit_then_reused_for_view_and_alert(tmp_path):
    from frisket.ai.embeddings.similarity import resolve_embedding_similarity

    client = _client(tmp_path)
    pid, project, sheet, col, index = _setup(client)
    anchor = {"kind": "manual_text_query", "text": "feline"}
    saved = save_view(client, pid, sheet, index, anchor)
    assert saved.status_code == 200, saved.text
    view = saved.json()
    canonical = view["spec"]["selection"]["similarity"]
    assert canonical["anchor"]["terms"] == [{"text": "feline", "weight": 1.0}]
    path = f"/api/projects/{pid}/views/{view['id']}/resolve"
    assert client.get(path).json()["detail"]["code"] == "selection_needs_preparation"

    class Gateway(_Gateway):
        calls = 0

        def embed(self, *args, **kwargs):
            self.calls += 1
            return super().embed(*args, **kwargs)

    gateway = Gateway()
    resolve_embedding_similarity(project, canonical, gateway=gateway)
    assert gateway.calls == 1
    assert client.get(path).status_code == 200
    watch = client.post(
        f"/api/projects/{pid}/watches",
        json={"name": "Prepared query", "view_id": view["id"]},
    ).json()
    check = client.post(f"/api/projects/{pid}/watches/{watch['id']}/run")
    assert check.status_code == 200, check.text
    assert check.json()["run"]["status"] == "ok"
    assert gateway.calls == 1


def test_hybrid_saved_view_reuses_prepared_vector_and_preserves_union(tmp_path):
    from frisket.ai.embeddings.similarity import resolve_embedding_similarity
    from frisket.search_index import drain_index

    client = _client(tmp_path)
    pid, project, sheet, col, index = _setup(client)
    drain_index(project)
    anchor = {"kind": "manual_text_query", "text": "cat"}
    view = save_view(client, pid, sheet, index, anchor, search="cat").json()
    resolve_embedding_similarity(
        project, view["spec"]["selection"]["similarity"], gateway=_Gateway()
    )
    opened = client.get(f"/api/projects/{pid}/views/{view['id']}/resolve")
    assert opened.status_code == 200, opened.text
    assert (
        opened.json()["total"] == 3
    )  # semantic matches are not limited to lexical matches
    assert opened.json()["refs"][0]["row_id"] == _row(project, sheet, col, "cat")


def test_cell_anchor_cannot_claim_multi_column_representation(tmp_path):
    client = _client(tmp_path)
    pid, project, sheet, col, index = _setup(client)
    # Metadata corruption models an old saved whole-row alias, without buying model work.
    import json

    project.add_column(sheet, "other")
    project.db.execute(
        "UPDATE embedding_indexes SET source_columns_json=? WHERE id=?",
        (json.dumps(["headline", "other"]), index),
    )
    project.db.commit()
    response = save_view(
        client,
        pid,
        sheet,
        index,
        {
            "kind": "row_cell",
            "row_id": _row(project, sheet, col, "cat"),
            "column_id": col,
        },
    )
    assert response.status_code == 400, response.text
    assert response.json()["detail"]["code"] == "embedding_cell_anchor_unavailable"


def test_similarity_view_does_not_inherit_preview_fifty_row_cap(tmp_path):
    client = _client(tmp_path)
    pid, project, sheet, col = _seed(client)
    cat = _row(project, sheet, col, "cat")
    project.add_rows(
        sheet, [{"headline": "kitten"} for _ in range(61)], {"headline": col}
    )
    index = _create(project, pid, sheet)
    _refresh(project, pid, index)
    view = save_view(
        client, pid, sheet, index, {"kind": "row", "row_id": cat}, limit=60
    ).json()
    opened = client.get(
        f"/api/projects/{pid}/views/{view['id']}/resolve", params={"limit": 100}
    )
    assert opened.status_code == 200, opened.text
    assert opened.json()["total"] == 60
    assert len(opened.json()["refs"]) == 60


def test_saved_weighted_query_preserves_terms_and_reuses_cached_vectors(tmp_path):
    client = _client(tmp_path)
    pid, project, sheet, col, index = _setup(client)
    anchor = {
        "kind": "manual_text_query",
        "terms": [{"text": "cat", "weight": 2.0}, {"text": "kitten", "weight": 1.0}],
    }
    saved = save_view(client, pid, sheet, index, anchor)
    assert saved.status_code == 200, saved.text
    assert (
        saved.json()["spec"]["selection"]["similarity"]["anchor"]["terms"]
        == anchor["terms"]
    )
    assert (
        client.get(
            f"/api/projects/{pid}/views/{saved.json()['id']}/resolve"
        ).status_code
        == 200
    )
