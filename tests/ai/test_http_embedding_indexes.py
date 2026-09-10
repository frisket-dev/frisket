"""Backend contract for ``GET /embeddings/v1/indexes``.

Read-only list of an (optional) sheet's embedding indexes with provider/model +
item counts + refresh-needed state. space_id is present (provenance) but not the
primary surface.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from frisket.ai.embeddings import build_batch_result
from frisket.engine.executor import ExecutorDeps, run_action_spec
from frisket.ai.llm import ModelRouter
from frisket.server.app import create_app


class _Gateway:
    def __init__(self, dim=384):
        self.dim = dim

    def embed(self, texts, *, provider, model, modality):
        return build_batch_result(
            [[1.0] + [0.0] * (self.dim - 1) for _ in texts],
            provider_id=provider or "fastembed",
            provider_kind="local_process",
            actual_model_id=model or "fake",
            modality=modality,
        )


def _client(tmp_path):
    return TestClient(create_app(tmp_path / "ws", router=ModelRouter(keys={})))


def _seed(client):
    pid = client.post("/api/projects", json={"name": "idx"}).json()["id"]
    project = client.app.state.workspace.get(pid)
    sheet = project.add_sheet("animals")
    cols = {"headline": project.add_column(sheet, "headline")}
    project.add_rows(sheet, [{"headline": t} for t in ("cat", "kitten")], cols)
    return pid, project, sheet


def _create(
    project, pid, sheet, *, provider="fastembed", policy=None, source_query=None
):
    params = {
        "sheet_id": sheet,
        "source_columns": ["headline"],
        "modality": "text",
        "provider": provider,
        "source_policy": {"kind": "text_cell"},
        "provider_policy": policy or {"allow_remote": False},
    }
    if source_query is not None:
        params["source_query"] = source_query
    r = run_action_spec(
        project,
        {
            "action_id": "embedding.index_create",
            "scope": {"kind": "project"},
            "params": params,
            "idempotency_key": f"c@{provider}",
        },
        project_id=pid,
    )
    return r.outputs[0].ref["index_id"]


def _refresh(project, pid, index_id):
    run_action_spec(
        project,
        {
            "action_id": "embedding.index_refresh",
            "scope": {"kind": "project"},
            "params": {"index_id": index_id, "mode": "full"},
            "idempotency_key": "r@1",
        },
        project_id=pid,
        deps=ExecutorDeps(embedding_gateway=_Gateway()),
    )


def _list(client, pid, sheet=None):
    suffix = f"?sheet_id={sheet}" if sheet is not None else ""
    return client.get(f"/api/projects/{pid}/embeddings/v1/indexes{suffix}")


def test_empty_list(tmp_path):
    client = _client(tmp_path)
    pid, _proj, _sheet = _seed(client)
    body = _list(client, pid).json()
    assert body["schema_version"] == "frisket.embedding_index_list.v1"
    assert body["indexes"] == []


def test_index_listed_with_provider_and_refresh_needed(tmp_path):
    client = _client(tmp_path)
    pid, project, sheet = _seed(client)
    index_id = _create(project, pid, sheet)
    body = _list(client, pid, sheet).json()
    [idx] = body["indexes"]
    assert idx["index_id"] == index_id
    assert idx["provider_id"] == "fastembed" and idx["modality"] == "text"
    assert idx["model_id"] and idx["dimension"] and idx["distance_metric"] == "cosine"
    assert idx["source_columns"] == ["headline"]
    assert idx["remote"] is False
    assert idx["space_id"]  # provenance present
    # created but never refreshed -> refresh needed
    assert idx["refresh_needed"] is True
    assert idx["ready_items"] == 0


def test_refresh_clears_refresh_needed(tmp_path):
    client = _client(tmp_path)
    pid, project, sheet = _seed(client)
    index_id = _create(project, pid, sheet)
    _refresh(project, pid, index_id)
    [idx] = _list(client, pid, sheet).json()["indexes"]
    assert idx["refresh_needed"] is False
    assert idx["ready_items"] == 2
    assert idx["last_refreshed_at"]


def test_remote_index_exposes_policy_flags(tmp_path):
    client = _client(tmp_path)
    pid, project, sheet = _seed(client)
    _create(
        project,
        pid,
        sheet,
        provider="openai",
        policy={"allow_remote": True, "allow_remote_automatic_refresh": False},
    )
    [idx] = _list(client, pid, sheet).json()["indexes"]
    assert idx["remote"] is True
    assert idx["provider_policy"]["allow_remote"] is True
    assert idx["provider_policy"]["allow_remote_automatic_refresh"] is False


def _headline_col(project, sheet):
    return next(c["id"] for c in project.columns(sheet) if c["name"] == "headline")


def _row_id(project, sheet, col_id, text):
    return next(
        rid for rid, v in project.get_values(sheet, col_id).items() if v == text
    )


def _cell_edit(project, pid, *, row_id, column_id, value, key):
    return run_action_spec(
        project,
        {
            "action_id": "cell.edit",
            "scope": {"kind": "project"},
            "params": {
                "edits": [
                    {
                        "row_id": row_id,
                        "column_id": column_id,
                        "value": value,
                    }
                ]
            },
            "idempotency_key": key,
        },
        project_id=pid,
    )


def test_editing_a_source_cell_marks_refresh_needed_before_similarity(tmp_path):
    # Regression: a full refresh clears refresh_needed, but editing a source cell
    # via the normal cell.edit action must flip it back to true in the LIST view —
    # the user should not have to run similarity-preview to discover staleness.
    client = _client(tmp_path)
    pid, project, sheet = _seed(client)
    index_id = _create(project, pid, sheet)
    _refresh(project, pid, index_id)
    assert _list(client, pid, sheet).json()["indexes"][0]["refresh_needed"] is False

    col = _headline_col(project, sheet)
    cat = _row_id(project, sheet, col, "cat")
    result = _cell_edit(
        project, pid, row_id=cat, column_id=col, value="panther", key="e@1"
    )
    assert result.status == "completed", result.errors

    after_edit = _list(client, pid, sheet).json()["indexes"][0]
    assert after_edit["refresh_needed"] is True
    assert after_edit["stale_source_items"] == 1

    # re-embedding the changed row clears it again
    run_action_spec(
        project,
        {
            "action_id": "embedding.index_refresh",
            "scope": {"kind": "project"},
            "params": {"index_id": index_id, "mode": "full"},
            "idempotency_key": "r@2",
        },
        project_id=pid,
        deps=ExecutorDeps(embedding_gateway=_Gateway()),
    )
    cleared = _list(client, pid, sheet).json()["indexes"][0]
    assert cleared["refresh_needed"] is False
    assert cleared["stale_source_items"] == 0


def test_appended_row_marks_refresh_needed_via_missing_source_items(tmp_path):
    # A new embeddable row that was never embedded must surface as refresh_needed
    # in the list (missing_source_items), so the UI doesn't show "fresh".
    client = _client(tmp_path)
    pid, project, sheet = _seed(client)
    index_id = _create(project, pid, sheet)
    _refresh(project, pid, index_id)
    assert _list(client, pid, sheet).json()["indexes"][0]["refresh_needed"] is False

    col = _headline_col(project, sheet)
    project.add_rows(sheet, [{"headline": "lion"}], {"headline": col})
    after = _list(client, pid, sheet).json()["indexes"][0]
    assert after["missing_source_items"] == 1
    assert after["stale_source_items"] == 0
    assert after["refresh_needed"] is True

    # re-embedding clears it
    run_action_spec(
        project,
        {
            "action_id": "embedding.index_refresh",
            "scope": {"kind": "project"},
            "params": {"index_id": index_id, "mode": "full"},
            "idempotency_key": "r@2",
        },
        project_id=pid,
        deps=ExecutorDeps(embedding_gateway=_Gateway()),
    )
    cleared = _list(client, pid, sheet).json()["indexes"][0]
    assert cleared["missing_source_items"] == 0
    assert cleared["refresh_needed"] is False


def _freshness(client, pid, sheet):
    return _list(client, pid, sheet).json()["indexes"][0]["freshness"]


def test_freshness_reason_fresh_then_never_refreshed(tmp_path):
    client = _client(tmp_path)
    pid, project, sheet = _seed(client)
    index_id = _create(project, pid, sheet)
    # created, never refreshed -> never_refreshed
    f0 = _freshness(client, pid, sheet)
    assert f0["reason"] == "never_refreshed" and f0["refresh_needed"] is True
    _refresh(project, pid, index_id)
    f1 = _freshness(client, pid, sheet)
    assert f1["reason"] == "fresh" and f1["refresh_needed"] is False
    assert f1["ready"] == 2 and f1["current"] == 2 and f1["missing"] == 0
    assert f1["maintenance_mode"] == "manual"
    assert f1["last_refresh_receipt_id"] and f1["last_refreshed_at"]


def test_freshness_reason_edited_row_is_stale_rows(tmp_path):
    client = _client(tmp_path)
    pid, project, sheet = _seed(client)
    index_id = _create(project, pid, sheet)
    _refresh(project, pid, index_id)
    col = _headline_col(project, sheet)
    cat = _row_id(project, sheet, col, "cat")
    _cell_edit(project, pid, row_id=cat, column_id=col, value="panther", key="e@1")
    f = _freshness(client, pid, sheet)
    assert f["reason"] == "stale_rows"
    assert f["stale"] == 1 and f["current"] == 1


def test_freshness_reason_appended_row_is_missing_rows(tmp_path):
    client = _client(tmp_path)
    pid, project, sheet = _seed(client)
    index_id = _create(project, pid, sheet)
    _refresh(project, pid, index_id)
    col = _headline_col(project, sheet)
    project.add_rows(sheet, [{"headline": "lion"}], {"headline": col})
    f = _freshness(client, pid, sheet)
    assert f["reason"] == "missing_rows"
    assert f["missing"] == 1 and f["ready"] == 2


def test_freshness_reason_empty_source_row_stays_fresh(tmp_path):
    # a row whose source columns are all empty is not embeddable -> not missing.
    client = _client(tmp_path)
    pid, project, sheet = _seed(client)
    index_id = _create(project, pid, sheet)
    _refresh(project, pid, index_id)
    col = _headline_col(project, sheet)
    project.add_rows(sheet, [{"headline": ""}], {"headline": col})
    f = _freshness(client, pid, sheet)
    assert f["reason"] == "fresh" and f["missing"] == 0


def test_freshness_reason_hidden_row_stays_fresh(tmp_path):
    # a hidden row is out of the visible scope -> not missing.
    client = _client(tmp_path)
    pid, project, sheet = _seed(client)
    index_id = _create(project, pid, sheet)
    _refresh(project, pid, index_id)
    col = _headline_col(project, sheet)
    project.add_rows(sheet, [{"headline": "lion"}], {"headline": col})
    lion = _row_id(project, sheet, col, "lion")
    project.db.execute("UPDATE rows SET hidden=1 WHERE id=?", (lion,))
    project.db.commit()
    f = _freshness(client, pid, sheet)
    assert f["reason"] == "fresh" and f["missing"] == 0


def test_freshness_reason_filtered_source_query_excludes_appended_row(tmp_path):
    # an index scoped by source_query=filter(headline eq cat) ignores rows outside
    # the filter, so appending a non-matching row stays fresh.
    client = _client(tmp_path)
    pid, project, sheet = _seed(client)
    source_query = {
        "kind": "filter",
        "sheet_id": sheet,
        "filter": {"headline": {"eq": "cat"}},
    }
    index_id = _create(project, pid, sheet, source_query=source_query)
    _refresh(project, pid, index_id)
    f0 = _freshness(client, pid, sheet)
    assert f0["reason"] == "fresh" and f0["ready"] == 1  # only "cat" in scope
    col = _headline_col(project, sheet)
    project.add_rows(sheet, [{"headline": "lion"}], {"headline": col})
    f1 = _freshness(client, pid, sheet)
    assert f1["reason"] == "fresh" and f1["missing"] == 0  # lion is out of scope


def test_pending_refresh_job_surfaced_in_freshness(tmp_path):
    # a queued embedding.index_refresh job for the index shows as pending.
    from frisket.engine.jobs import EMBEDDING_REFRESH_KIND

    client = _client(tmp_path)
    pid, project, sheet = _seed(client)
    index_id = _create(project, pid, sheet)
    _refresh(project, pid, index_id)
    queue = client.app.state.workspace.queue
    job_id = queue.enqueue(
        EMBEDDING_REFRESH_KIND,
        {"project_id": pid, "index_id": index_id, "mode": "incremental"},
    )
    f = _freshness(client, pid, sheet)
    assert f["pending_refresh_job_id"] == job_id


def test_malformed_policy_json_does_not_500(tmp_path):
    client = _client(tmp_path)
    pid, project, sheet = _seed(client)
    index_id = _create(project, pid, sheet)
    project.db.execute(
        "UPDATE embedding_indexes SET provider_policy_json='{bad', "
        "maintenance_policy_json='nope', source_columns_json='[' WHERE id=?",
        (index_id,),
    )
    project.db.commit()
    resp = _list(client, pid, sheet)
    assert resp.status_code == 200
    [idx] = resp.json()["indexes"]
    assert idx["provider_policy"] == {
        "allow_remote": False,
        "allow_remote_automatic_refresh": False,
        "max_cost_usd_per_refresh": None,
    }
    assert idx["maintenance"] == {"mode": "manual", "schedule": None}
    assert idx["source_columns"] == []


def test_corrupt_policy_leaves_are_verbatim_for_client_repair(tmp_path):
    client = _client(tmp_path)
    pid, project, sheet = _seed(client)
    index_id = _create(project, pid, sheet)
    project.db.execute(
        "UPDATE embedding_indexes SET provider_policy_json=?, maintenance_policy_json=? "
        "WHERE id=?",
        (
            '{"allow_remote":"false","allow_remote_automatic_refresh":1,'
            '"max_cost_usd_per_refresh":"unbounded","unknown":true}',
            '{"mode":"scheduled","schedule":12,"unknown":true}',
            index_id,
        ),
    )
    project.db.commit()

    response = _list(client, pid, sheet)
    assert response.status_code == 200, response.text
    [index] = response.json()["indexes"]
    assert index["provider_policy"] == {
        "allow_remote": "false",
        "allow_remote_automatic_refresh": 1,
        "max_cost_usd_per_refresh": "unbounded",
    }
    assert index["maintenance"] == {"mode": "scheduled", "schedule": 12}


def test_missing_project_404(tmp_path):
    client = _client(tmp_path)
    assert _list(client, "nope").status_code == 404
