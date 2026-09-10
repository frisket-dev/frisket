"""watchlists over embedding_similarity (row-anchor 'rows similar to X').

A watch binds to an embedding_similarity QuerySpec; the existing watch_seen_rows
snapshot machinery turns the resolved similar-row set into enter/exit + dedupe +
notifications — no new watch inbox. A stale index produces a typed blocked watch
run (cursor not advanced) until a refresh clears it; the watch never calls a
provider (row anchors only).
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from frisket.ai.embeddings import build_batch_result
from frisket.engine.executor import ExecutorDeps, run_action_spec
from frisket.ai.llm import ModelRouter
from frisket.server.app import create_app

_VECTORS = {
    "cat": [1.0, 0.0, 0.0],
    "kitten": [0.8, 0.6, 0.0],
    "airplane": [0.0, 0.0, 1.0],
}


class _Gateway:
    def __init__(self, dim=384):
        self.dim = dim

    def embed(self, texts, *, provider, model, modality):
        out = [
            list(_VECTORS.get(t, [0.0, 0.0, 0.0])) + [0.0] * (self.dim - 3)
            for t in texts
        ]
        return build_batch_result(
            out,
            provider_id=provider or "fastembed",
            provider_kind="local_process",
            actual_model_id=model or "fake",
            modality=modality,
        )


def _client(tmp_path):
    return TestClient(create_app(tmp_path / "ws", router=ModelRouter(keys={})))


def _seed(client):
    pid = client.post("/api/projects", json={"name": "w"}).json()["id"]
    project = client.app.state.workspace.get(pid)
    sheet = project.add_sheet("animals")
    col = project.add_column(sheet, "headline")
    project.add_rows(sheet, [{"headline": t} for t in _VECTORS], {"headline": col})
    return pid, project, sheet, col


def _create_index(project, pid, sheet):
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


def _edit_cell(project, pid, *, row_id, column_id, value, key):
    run_action_spec(
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


def _row_id(project, sheet, col, text):
    return next(rid for rid, v in project.get_values(sheet, col).items() if v == text)


def _make_watch(
    client, pid, sheet, index_id, anchor_row, *, policy=None, **anchor_extra
):
    body = {
        "name": "similar to cat",
        "scope": {"kind": "sheet", "sheet_id": sheet},
        "query": {
            "kind": "embedding_similarity",
            "embedding_index_id": index_id,
            "anchor": {"kind": "row", "row_id": anchor_row, **anchor_extra},
            "limit": 10,
        },
        "detection_policy": policy or {"kind": "new_matches"},
    }
    return client.post(f"/api/projects/{pid}/watches", json=body)


def _run(client, pid, watch_id):
    return client.post(f"/api/projects/{pid}/watches/{watch_id}/run").json()


def _events(client, pid, watch_id, run_id):
    return client.get(
        f"/api/projects/{pid}/watches/{watch_id}/runs/{run_id}/events"
    ).json()


def _notifications(client, pid):
    return client.get(f"/api/projects/{pid}/notifications").json()


# --------------------------------------------------------------------------


def test_fresh_watch_emits_enter_events_and_a_notification(tmp_path):
    client = _client(tmp_path)
    pid, project, sheet, col = _seed(client)
    index_id = _create_index(project, pid, sheet)
    _refresh(project, pid, index_id)
    cat = _row_id(project, sheet, col, "cat")
    watch = _make_watch(client, pid, sheet, index_id, cat).json()

    first = _run(client, pid, watch["id"])
    assert first["run"]["status"] == "ok"
    # the anchor is excluded; the other two rows are the similar set, all new
    assert first["run"]["matched_rows"] == 2
    assert first["run"]["new_rows"] == 2
    events = _events(client, pid, watch["id"], first["run"]["id"])
    assert events["total"] == 2
    assert {e["event_kind"] for e in events["events"]} == {"row_entered"}
    # reuses the notification substrate (source_kind=watch), not a new inbox
    notifs = _notifications(client, pid)
    assert notifs["total"] == 1
    assert notifs["notifications"][0]["source_kind"] == "watch"


def test_no_duplicate_events_across_reruns(tmp_path):
    client = _client(tmp_path)
    pid, project, sheet, col = _seed(client)
    index_id = _create_index(project, pid, sheet)
    _refresh(project, pid, index_id)
    cat = _row_id(project, sheet, col, "cat")
    watch = _make_watch(client, pid, sheet, index_id, cat).json()

    first = _run(client, pid, watch["id"])
    assert first["run"]["new_rows"] == 2
    # rerun with an unchanged index: no new rows, no new events, no new notification
    second = _run(client, pid, watch["id"])
    assert second["run"]["status"] == "ok"
    assert second["run"]["new_rows"] == 0
    assert _events(client, pid, watch["id"], second["run"]["id"])["total"] == 0
    assert _notifications(client, pid)["total"] == 1


def test_stale_index_blocks_watch_with_typed_result(tmp_path):
    client = _client(tmp_path)
    pid, project, sheet, col = _seed(client)
    index_id = _create_index(project, pid, sheet)
    _refresh(project, pid, index_id)
    cat = _row_id(project, sheet, col, "cat")
    kitten = _row_id(project, sheet, col, "kitten")
    watch = _make_watch(client, pid, sheet, index_id, cat).json()
    _run(client, pid, watch["id"])  # first ok run

    # a source cell changes -> the index is stale (a candidate's vector is stale)
    _edit_cell(project, pid, row_id=kitten, column_id=col, value="panther", key="e@1")
    blocked = _run(client, pid, watch["id"])
    assert blocked["run"]["status"] == "error"
    assert blocked["run"]["error_code"] == "embedding_index_stale"
    assert blocked["run"]["new_rows"] == 0
    # no spurious notification for a blocked run
    assert _notifications(client, pid)["total"] == 1


def test_refresh_then_evaluate_clears_the_block(tmp_path):
    client = _client(tmp_path)
    pid, project, sheet, col = _seed(client)
    index_id = _create_index(project, pid, sheet)
    _refresh(project, pid, index_id)
    cat = _row_id(project, sheet, col, "cat")
    kitten = _row_id(project, sheet, col, "kitten")
    watch = _make_watch(client, pid, sheet, index_id, cat).json()
    _run(client, pid, watch["id"])
    _edit_cell(project, pid, row_id=kitten, column_id=col, value="panther", key="e@1")
    assert _run(client, pid, watch["id"])["run"]["status"] == "error"

    # the configured refresh path re-embeds; the watch then evaluates fresh again,
    # diffing against the last OK run (so an unchanged membership emits no events).
    _refresh(project, pid, index_id, key="r@2")
    ok = _run(client, pid, watch["id"])
    assert ok["run"]["status"] == "ok"
    assert ok["run"]["matched_rows"] == 2
    assert _events(client, pid, watch["id"], ok["run"]["id"])["total"] == 0


def test_appended_unembedded_row_blocks_watch_then_refresh_emits_enter(tmp_path):
    # A new embeddable row that entered the index scope but was never embedded makes
    # the index INCOMPLETE — the watch must block (not silently miss the row), and a
    # refresh then embeds it and emits its row_entered.
    client = _client(tmp_path)
    pid, project, sheet, col = _seed(client)
    index_id = _create_index(project, pid, sheet)
    _refresh(project, pid, index_id)
    cat = _row_id(project, sheet, col, "cat")
    watch = _make_watch(client, pid, sheet, index_id, cat).json()
    _run(client, pid, watch["id"])  # ok, entered {kitten, airplane}

    # append a new embeddable row WITHOUT refreshing
    project.add_rows(sheet, [{"headline": "lion"}], {"headline": col})
    blocked = _run(client, pid, watch["id"])
    assert blocked["run"]["status"] == "error"
    assert blocked["run"]["error_code"] == "embedding_index_incomplete"
    assert blocked["run"]["new_rows"] == 0
    assert _notifications(client, pid)["total"] == 1  # no spurious notification

    # refresh embeds the new row; the watch then evaluates ok and the new row enters
    _refresh(project, pid, index_id, key="r@2")
    lion = _row_id(project, sheet, col, "lion")
    ok = _run(client, pid, watch["id"])
    assert ok["run"]["status"] == "ok"
    assert ok["run"]["matched_rows"] == 3
    events = _events(client, pid, watch["id"], ok["run"]["id"])["events"]
    assert {e["event_kind"] for e in events} == {"row_entered"}
    assert lion in {e["subject_ref"]["row_id"] for e in events}


def test_create_rejects_watch_sheet_mismatching_index_sheet(tmp_path):
    client = _client(tmp_path)
    pid, project, sheet, col = _seed(client)
    index_id = _create_index(project, pid, sheet)
    _refresh(project, pid, index_id)
    other_sheet = project.add_sheet("other")
    project.add_column(other_sheet, "headline")
    cat = _row_id(project, sheet, col, "cat")
    resp = client.post(
        f"/api/projects/{pid}/watches",
        json={
            "name": "cross sheet",
            "scope": {"kind": "sheet", "sheet_id": other_sheet},
            "query": {
                "kind": "embedding_similarity",
                "embedding_index_id": index_id,
                "anchor": {"kind": "row", "row_id": cat},
            },
        },
    )
    assert resp.status_code == 400
    assert "does not match" in resp.json()["detail"]


def test_evaluator_blocks_legacy_cross_sheet_watch(tmp_path):
    # a directly-stored (legacy) watch whose sheet != the index sheet must block in
    # the evaluator, never recording cross-sheet hits.
    from frisket.features.watchlists.service import run_watch_evaluation

    client = _client(tmp_path)
    pid, project, sheet, col = _seed(client)
    index_id = _create_index(project, pid, sheet)
    _refresh(project, pid, index_id)
    cat = _row_id(project, sheet, col, "cat")
    other_sheet = project.add_sheet("other")
    watch_id = project.add_watch(
        "legacy cross sheet",
        scope="sheet",
        sheet_id=other_sheet,
        query={
            "kind": "embedding_similarity",
            "scope": {"kind": "sheet", "sheet_id": other_sheet},
            "embedding_index_id": index_id,
            "anchor": {"kind": "row", "row_id": cat},
        },
    )
    result = run_watch_evaluation(project, project.get_watch(watch_id))
    assert result["run"]["status"] == "error"
    assert result["run"]["error_code"] == "embedding_index_sheet_mismatch"


def test_row_cell_anchor_is_rejected(tmp_path):
    # row_cell is not column-specific yet (resolver uses the whole-row vector), so
    # it is not watchable — the create must 400.
    client = _client(tmp_path)
    pid, project, sheet, col = _seed(client)
    index_id = _create_index(project, pid, sheet)
    _refresh(project, pid, index_id)
    cat = _row_id(project, sheet, col, "cat")
    resp = client.post(
        f"/api/projects/{pid}/watches",
        json={
            "name": "row cell",
            "scope": {"kind": "sheet", "sheet_id": sheet},
            "query": {
                "kind": "embedding_similarity",
                "embedding_index_id": index_id,
                "anchor": {"kind": "row_cell", "row_id": cat, "column_id": col},
            },
        },
    )
    assert resp.status_code == 400


def test_manual_text_query_anchor_is_rejected(tmp_path):
    # a manual_text_query would re-embed (and possibly egress) on every run, so it
    # is not watchable in this slice — the create must 400.
    client = _client(tmp_path)
    pid, project, sheet, col = _seed(client)
    index_id = _create_index(project, pid, sheet)
    _refresh(project, pid, index_id)
    resp = client.post(
        f"/api/projects/{pid}/watches",
        json={
            "name": "manual text",
            "scope": {"kind": "sheet", "sheet_id": sheet},
            "query": {
                "kind": "embedding_similarity",
                "embedding_index_id": index_id,
                "anchor": {"kind": "manual_text_query", "text": "cat"},
            },
        },
    )
    assert resp.status_code == 400
