from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from helpers import make_client as _client
from frisket.engine.runner.review import queue_count, review_queue
from frisket.engine.store.runs import RunResultStore
from helpers import write_claimed_test_results
from http_test_helpers import (
    post_cell_edit_as_v1_action,
    post_operation_redo_as_v1_action,
    post_operation_undo_as_v1_action,
    post_review_decision_as_v1_action,
    post_row_add_as_v1_action,
)


def _seed_sheet(client: TestClient) -> tuple[str, int]:
    pid = client.post("/api/projects", json={"name": "Rows"}).json()["id"]
    resp = client.post(
        f"/api/projects/{pid}/import/csv",
        files={"file": ("people.csv", "name\nAda\n", "text/csv")},
    )
    assert resp.status_code == 200, resp.text
    return pid, resp.json()["sheet_id"]


def _seed_ai_column(client: TestClient, pid: str, sheet_id: int) -> int:
    project = client.app.state.workspace.get(pid)
    source_row_id = project.visible_row_ids(sheet_id)[0]
    col_id = project.add_column(sheet_id, "summary", ai_generated=True)
    op_id = project.append_op(
        "map", {"action_kind": "map.classify"}, label="seed summary"
    )
    run_id = RunResultStore(project).start_run(
        op_id,
        sheet_id,
        "map.classify",
        params={"sheet_id": sheet_id},
        total_rows=1,
    )
    write_claimed_test_results(
        project,
        run_id,
        [{"row_id": source_row_id, "column_id": col_id, "value": "done"}],
    )
    RunResultStore(project).finish_run(run_id)
    RunResultStore(project).point_column_at_run(op_id, col_id, run_id)
    return col_id


def _added_row_result(body: dict) -> tuple[int, int]:
    assert body["schema_version"] == "frisket.action_result.v1"
    assert body["status"] == "completed", body
    output = next(
        item
        for item in body["outputs"]
        if item["kind"] == "rows" and item["name"] == "added_rows"
    )
    return int(output["row_ids"][0]), int(output["ref"]["total"])


def test_add_row_undo_hides_row_and_redo_restores_it(tmp_path: Path) -> None:
    client = _client(tmp_path)
    pid, sheet_id = _seed_sheet(client)
    ai_col_id = _seed_ai_column(client, pid, sheet_id)

    add = post_row_add_as_v1_action(client, pid, sheet_id, {"name": "Grace"})
    assert add.status_code == 200, add.text
    add_body = add.json()
    added_row_id, total = _added_row_result(add_body)
    add_op_id = add_body["op_ids"][0]
    assert total == 2
    project = client.app.state.workspace.get(pid)
    op = project.db.execute(
        "SELECT id, undo_info FROM ops WHERE kind='add_row' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert op["id"] == add_op_id
    assert json.loads(op["undo_info"]) == {"created_rows": [added_row_id]}

    with_added = client.get(f"/api/projects/{pid}/sheets/{sheet_id}/data").json()
    assert with_added["total"] == 2
    added = next(r for r in with_added["rows"] if r["id"] == added_row_id)
    assert added["cells"]
    assert added["meta"][str(ai_col_id)]["state"] == "incomplete"

    undo = post_operation_undo_as_v1_action(client, pid, expected_op_id=add_op_id)
    assert undo.status_code == 200, undo.text
    assert undo.json()["schema_version"] == "frisket.action_result.v1"
    assert undo.json()["status"] == "completed"
    assert undo.json()["op_ids"] == [add_op_id]

    after_undo = client.get(f"/api/projects/{pid}/sheets/{sheet_id}/data").json()
    assert after_undo["total"] == 1
    assert added_row_id not in {r["id"] for r in after_undo["rows"]}

    hidden = project.db.execute(
        "SELECT hidden FROM rows WHERE id=?", (added_row_id,)
    ).fetchone()
    assert hidden is not None
    assert hidden["hidden"] == 1
    raw_cells = project.db.execute(
        "SELECT value FROM cells WHERE row_id=?", (added_row_id,)
    ).fetchall()
    assert [json.loads(r["value"]) for r in raw_cells] == ["Grace"]
    export = client.get(
        f"/api/projects/{pid}/exports/sheets?sheet_id={sheet_id}&format=csv"
    )
    assert export.status_code == 200, export.text
    assert "Grace" not in export.text

    current_run_id = int(
        project.db.execute(
            "SELECT run_id FROM cell_result_heads WHERE column_id=? LIMIT 1",
            (ai_col_id,),
        ).fetchone()[0]
    )
    assert added_row_id not in project.get_values(sheet_id, ai_col_id)
    assert queue_count(project) == 1
    assert added_row_id not in {item["row_id"] for item in review_queue(project)}
    stale_review = post_review_decision_as_v1_action(
        client,
        pid,
        run_id=current_run_id,
        row_id=added_row_id,
        column_id=ai_col_id,
        decision="accept",
    )
    assert stale_review.status_code == 400
    stale_edit = post_cell_edit_as_v1_action(
        client,
        pid,
        [
            {
                "row_id": added_row_id,
                "column_id": ai_col_id,
                "value": "x",
            }
        ],
    )
    assert stale_edit.status_code == 400
    redo = post_operation_redo_as_v1_action(client, pid, expected_op_id=add_op_id)
    assert redo.status_code == 200, redo.text
    assert redo.json()["schema_version"] == "frisket.action_result.v1"
    assert redo.json()["status"] == "completed"
    assert redo.json()["op_ids"] == [add_op_id]

    after_redo = client.get(f"/api/projects/{pid}/sheets/{sheet_id}/data").json()
    assert after_redo["total"] == 2
    restored = next(r for r in after_redo["rows"] if r["id"] == added_row_id)
    assert restored["meta"][str(ai_col_id)]["state"] == "incomplete"
