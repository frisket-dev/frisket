from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from helpers import make_client as _client
from http_test_helpers import (
    post_column_add_as_v1_action,
    post_operation_redo_as_v1_action,
    post_operation_undo_as_v1_action,
)


def _seed_sheet(client: TestClient) -> tuple[str, int]:
    pid = client.post("/api/projects", json={"name": "Cols"}).json()["id"]
    resp = client.post(
        f"/api/projects/{pid}/import/csv",
        files={"file": ("people.csv", "name,age\nAda,36\n", "text/csv")},
    )
    assert resp.status_code == 200, resp.text
    return pid, resp.json()["sheet_id"]


def _added_column(body: dict) -> dict:
    assert body["schema_version"] == "frisket.action_result.v1"
    assert body["status"] == "completed", body
    output = next(item for item in body["outputs"] if item["kind"] == "column")
    return output


def _visible_columns(client: TestClient, pid: str, sheet_id: int) -> list[str]:
    project = client.app.state.workspace.get(pid)
    return [c["name"] for c in project.columns(sheet_id)]


def test_column_add_appends_and_is_visible(tmp_path: Path) -> None:
    client = _client(tmp_path)
    pid, sheet_id = _seed_sheet(client)

    resp = post_column_add_as_v1_action(client, pid, sheet_id, "notes")
    assert resp.status_code == 200, resp.text
    output = _added_column(resp.json())
    assert output["name"] == "notes"
    assert output["ref"]["type"] == "text"

    cols = _visible_columns(client, pid, sheet_id)
    assert cols == ["name", "age", "notes"]


def test_column_add_position_inserts_and_shifts(tmp_path: Path) -> None:
    client = _client(tmp_path)
    pid, sheet_id = _seed_sheet(client)

    # position=2 => insert before the 2nd visible column ("age").
    resp = post_column_add_as_v1_action(
        client, pid, sheet_id, "middle", type="integer", position=2
    )
    assert resp.status_code == 200, resp.text
    output = _added_column(resp.json())
    assert output["ref"]["type"] == "integer"

    cols = _visible_columns(client, pid, sheet_id)
    assert cols == ["name", "middle", "age"]


def test_column_add_rejects_duplicate_name(tmp_path: Path) -> None:
    client = _client(tmp_path)
    pid, sheet_id = _seed_sheet(client)

    resp = post_column_add_as_v1_action(client, pid, sheet_id, "age")
    assert resp.status_code == 400, resp.text
    assert _visible_columns(client, pid, sheet_id) == ["name", "age"]


def test_column_add_rejects_blank_name(tmp_path: Path) -> None:
    client = _client(tmp_path)
    pid, sheet_id = _seed_sheet(client)

    resp = post_column_add_as_v1_action(client, pid, sheet_id, "   ")
    assert resp.status_code == 400, resp.text
    assert resp.json()["errors"][0]["code"] == "invalid_column_name"
    assert _visible_columns(client, pid, sheet_id) == ["name", "age"]


def test_column_add_replays_before_duplicate_lookup_with_canonical_defaults(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    pid, sheet_id = _seed_sheet(client)
    key = "column-add-replay@sha256:stable"

    first = post_column_add_as_v1_action(
        client, pid, sheet_id, "notes", idempotency_key=key
    )
    assert first.status_code == 200, first.text
    replay = client.post(
        f"/api/projects/{pid}/actions/v1/run",
        json={
            "action_id": "column.add",
            "scope": {"kind": "project"},
            "params": {
                "sheet_id": sheet_id,
                "name": "notes",
                "type": "text",
                "position": None,
            },
            "idempotency_key": key,
        },
    )

    assert replay.status_code == 200, replay.text
    assert replay.json()["receipt_id"] == first.json()["receipt_id"]
    assert replay.json()["outputs"] == first.json()["outputs"]
    assert _visible_columns(client, pid, sheet_id) == ["name", "age", "notes"]


def test_column_add_undo_hides_and_redo_restores(tmp_path: Path) -> None:
    client = _client(tmp_path)
    pid, sheet_id = _seed_sheet(client)

    resp = post_column_add_as_v1_action(client, pid, sheet_id, "notes")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    output = _added_column(body)
    column_id = int(output["column_id"])
    add_op_id = body["op_ids"][0]

    project = client.app.state.workspace.get(pid)
    op = project.db.execute(
        "SELECT id, undo_info FROM ops WHERE kind='add_column' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert op["id"] == add_op_id
    assert json.loads(op["undo_info"]) == {"created_columns": [column_id]}

    undo = post_operation_undo_as_v1_action(client, pid, expected_op_id=add_op_id)
    assert undo.status_code == 200, undo.text
    assert undo.json()["op_ids"] == [add_op_id]
    assert "notes" not in _visible_columns(client, pid, sheet_id)
    hidden = project.db.execute(
        "SELECT hidden FROM columns WHERE id=?", (column_id,)
    ).fetchone()
    assert hidden["hidden"] == 1

    redo = post_operation_redo_as_v1_action(client, pid, expected_op_id=add_op_id)
    assert redo.status_code == 200, redo.text
    assert "notes" in _visible_columns(client, pid, sheet_id)


def test_column_add_is_in_action_catalog(tmp_path: Path) -> None:
    client = _client(tmp_path)
    pid, _ = _seed_sheet(client)
    resp = client.get(f"/api/projects/{pid}/actions/v1/catalog")
    assert resp.status_code == 200, resp.text
    kinds = {entry["kind"] for entry in resp.json()["actions"]}
    assert "column.add" in kinds
