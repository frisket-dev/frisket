from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from helpers import make_client as _client
from frisket.contracts.action import ActionResult, Receipt


def _seed_project(
    client: TestClient,
) -> tuple[str, int, dict[str, int], dict[str, int]]:
    pid = client.post("/api/projects", json={"name": "Filter Mutation"}).json()["id"]
    project = client.app.state.workspace.get(pid)
    sheet_id = project.add_sheet("tasks")
    columns = {
        "title": project.add_column(sheet_id, "title"),
        "status": project.add_column(sheet_id, "status"),
        "priority": project.add_column(sheet_id, "priority"),
    }
    row_ids = project.add_rows(
        sheet_id,
        [
            {"title": "A", "status": "open", "priority": "low"},
            {"title": "B", "status": "open", "priority": "high"},
            {"title": "C", "status": "closed", "priority": "low"},
        ],
        columns,
    )
    return (
        pid,
        sheet_id,
        {"a": row_ids[0], "b": row_ids[1], "c": row_ids[2]},
        columns,
    )


def _query(sheet_id: int) -> dict[str, Any]:
    return {
        "schema_version": "frisket.query.v1",
        "kind": "sheet.filter",
        "scope": {"kind": "sheet", "sheet_id": sheet_id},
        "filter": {"status": {"eq": "open"}},
        "sort": [{"column": "title", "dir": "asc"}],
    }


def _edit_query_action(
    sheet_id: int,
    status_column_id: int,
    *,
    key: str = "filter-cell-edit@sha256:v1",
    filter_: dict[str, Any] | None = None,
) -> dict[str, Any]:
    query = _query(sheet_id)
    if filter_ is not None:
        query["filter"] = filter_
    return {
        "action_id": "cell.edit_query",
        "scope": {"kind": "project"},
        "params": {
            "query": query,
            "column_id": status_column_id,
            "value": "closed",
        },
        "idempotency_key": key,
    }


def _operation_action(kind: str, *, key: str, expected_op_id: int) -> dict[str, Any]:
    return {
        "action_id": kind,
        "scope": {"kind": "project"},
        "params": {"expected_op_id": expected_op_id},
        "idempotency_key": key,
    }


def _grid_row_ids(
    client: TestClient,
    pid: str,
    sheet_id: int,
    query: dict[str, Any],
) -> list[int]:
    response = client.get(
        f"/api/projects/{pid}/sheets/{sheet_id}/data",
        params={
            "filter": json.dumps(query["filter"]),
            "sort": json.dumps(query["sort"]),
        },
    )
    assert response.status_code == 200, response.text
    return [int(row["id"]) for row in response.json()["rows"]]


def _live_values(
    client: TestClient,
    pid: str,
    sheet_id: int,
    rows: dict[str, int],
    status_column_id: int,
) -> dict[str, Any]:
    project = client.app.state.workspace.get(pid)
    return {
        name: project.get_values(sheet_id, status_column_id, row_ids=[row_id])[row_id]
        for name, row_id in rows.items()
    }


def test_filter_scoped_cell_edit_resolves_rowset_before_mutation_and_replay(
    tmp_path: Path,
) -> None:
    from frisket.actions.system import root_action_catalog

    client = _client(tmp_path)
    pid, sheet_id, rows, columns = _seed_project(client)
    query = _query(sheet_id)
    action = _edit_query_action(sheet_id, columns["status"])

    catalog = root_action_catalog()
    entry = next(item for item in catalog.actions if item.kind == "cell.edit_query")
    assert entry.execution_mode == "whole_project"
    assert entry.async_mode == "sync"
    assert entry.writes_project is True
    assert entry.required_capabilities == ["project:write"]
    assert entry.receipt_policy == "writes_receipt"
    assert set(entry.side_effects) == {
        "read_query_rowset",
        "read_source_cell",
        "write_edit_overlay",
        "write_edit_op",
        "write_receipt",
    }

    assert _grid_row_ids(client, pid, sheet_id, query) == [rows["a"], rows["b"]]

    run = client.post(f"/api/projects/{pid}/actions/v1/run", json=action)
    assert run.status_code == 200, run.text
    result = ActionResult.model_validate(run.json())
    assert result.status == "completed"
    assert result.action.kind == "cell.edit_query"
    assert result.receipt_id is not None
    assert len(result.op_ids) == 1
    edit_op_id = result.op_ids[0]
    assert result.outputs[0].kind == "edit"
    assert result.outputs[0].sheet_id == sheet_id
    assert result.outputs[0].column_id == columns["status"]
    assert result.outputs[0].row_ids == [rows["a"], rows["b"]]
    assert result.outputs[0].ref["kind"] == "query_cell_edit_batch"
    assert result.outputs[0].ref["query_hash"].startswith("sha256:")
    assert result.outputs[0].ref["row_ids"] == [rows["a"], rows["b"]]
    assert result.outputs[0].ref["evaluator"] == {
        "kind": "frisket.querysets.sheet_filter",
        "version": "v1",
    }
    assert _grid_row_ids(client, pid, sheet_id, query) == []
    assert _live_values(client, pid, sheet_id, rows, columns["status"]) == {
        "a": "closed",
        "b": "closed",
        "c": "closed",
    }

    project = client.app.state.workspace.get(pid)
    receipt_row = project.db.execute(
        "SELECT * FROM receipts WHERE id=?", (result.receipt_id,)
    ).fetchone()
    assert receipt_row is not None
    receipt = Receipt.model_validate(json.loads(receipt_row["body"]))
    assert receipt.action_kind == "cell.edit_query"
    assert receipt.op_ids == [edit_op_id]
    assert receipt.outputs[0].ref == result.outputs[0].ref
    assert receipt.provider_use == [
        {
            "provider": "local",
            "service": "frisket.querysets.sheet_filter",
            "version": "v1",
            "external_api": False,
            "cost_actual": 0.0,
        }
    ]
    evidence_kinds = {item.ref["kind"] for item in receipt.evidence}
    assert {"query_cell_edit_rowset", "manual_edit_overlay"} <= evidence_kinds
    rowset_ref = next(
        item.ref
        for item in receipt.evidence
        if item.ref["kind"] == "query_cell_edit_rowset"
    )
    assert rowset_ref["row_ids"] == [rows["a"], rows["b"]]
    assert rowset_ref["total"] == 2

    # Change the live filter membership through an ordinary subsequent edit.
    later_edit_id = project.apply_edits(
        [{"row_id": rows["c"], "column_id": columns["status"], "value": "open"}]
    )
    assert _grid_row_ids(client, pid, sheet_id, query) == [rows["c"]]

    replay = client.post(f"/api/projects/{pid}/actions/v1/run", json=action)
    assert replay.status_code == 200, replay.text
    replay_result = ActionResult.model_validate(replay.json())
    assert replay_result.receipt_id == result.receipt_id
    assert replay_result.outputs == result.outputs
    assert (
        project.db.execute(
            "SELECT COUNT(*) FROM receipts WHERE action_kind='cell.edit_query'"
        ).fetchone()[0]
        == 1
    )
    assert _live_values(client, pid, sheet_id, rows, columns["status"]) == {
        "a": "closed",
        "b": "closed",
        "c": "open",
    }

    # Undo that later edit first: history is ordered, and replay must not have
    # inserted another operation or changed which operation is undoable.
    undo_later = client.post(
        f"/api/projects/{pid}/actions/v1/run",
        json=_operation_action(
            "operation.undo",
            key="undo-later-edit@sha256:v1",
            expected_op_id=later_edit_id,
        ),
    )
    assert undo_later.status_code == 200, undo_later.text
    assert ActionResult.model_validate(undo_later.json()).status == "completed"

    undo = client.post(
        f"/api/projects/{pid}/actions/v1/run",
        json=_operation_action(
            "operation.undo",
            key="undo-filter-cell-edit@sha256:v1",
            expected_op_id=edit_op_id,
        ),
    )
    assert undo.status_code == 200, undo.text
    assert _live_values(client, pid, sheet_id, rows, columns["status"]) == {
        "a": "open",
        "b": "open",
        "c": "closed",
    }
    assert _grid_row_ids(client, pid, sheet_id, query) == [
        rows["a"],
        rows["b"],
    ]

    redo = client.post(
        f"/api/projects/{pid}/actions/v1/run",
        json=_operation_action(
            "operation.redo",
            key="redo-filter-cell-edit@sha256:v1",
            expected_op_id=edit_op_id,
        ),
    )
    assert redo.status_code == 200, redo.text
    assert _live_values(client, pid, sheet_id, rows, columns["status"]) == {
        "a": "closed",
        "b": "closed",
        "c": "closed",
    }
    assert _grid_row_ids(client, pid, sheet_id, query) == []


def test_filter_scoped_cell_edit_stales_only_exact_prior_evidence(
    tmp_path: Path,
) -> None:
    from frisket.engine.store.evidence import record_evidence_link

    client = _client(tmp_path)
    pid, sheet_id, rows, columns = _seed_project(client)
    project = client.app.state.workspace.get(pid)
    _values, refs = project.get_values_with_refs(
        sheet_id, columns["status"], row_ids=[rows["a"]]
    )
    current_ref = refs[rows["a"]]
    exact = record_evidence_link(
        project,
        subject_kind="cell_value",
        subject_ref=current_ref,
        spans=[],
        sheet_id=sheet_id,
        row_id=rows["a"],
        column_id=columns["status"],
    )
    unrelated = record_evidence_link(
        project,
        subject_kind="cell_value",
        subject_ref={**current_ref, "kind": "other_value"},
        spans=[],
        sheet_id=sheet_id,
        row_id=rows["a"],
        column_id=columns["status"],
    )

    response = client.post(
        f"/api/projects/{pid}/actions/v1/run",
        json=_edit_query_action(
            sheet_id,
            columns["status"],
            key="filter-cell-edit-evidence@sha256:v1",
        ),
    )
    assert response.status_code == 200, response.text
    statuses = {
        str(row["stable_id"]): (str(row["status"]), row["stale_reason"])
        for row in project.db.execute(
            "SELECT stable_id, status, stale_reason FROM evidence_links"
        ).fetchall()
    }
    assert statuses[exact["stable_id"]] == ("stale", "manual_cell_edit")
    assert statuses[unrelated["stable_id"]] == ("active", None)


def test_filter_scoped_cell_edit_rejects_empty_rowset_without_history(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    pid, sheet_id, _rows, columns = _seed_project(client)
    action = _edit_query_action(
        sheet_id,
        columns["status"],
        key="empty-filter-cell-edit@sha256:v1",
        filter_={"status": {"eq": "missing"}},
    )
    project = client.app.state.workspace.get(pid)
    before = {
        table: int(project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in ("ops", "edits", "receipts")
    }

    run = client.post(f"/api/projects/{pid}/actions/v1/run", json=action)
    assert run.status_code == 400, run.text
    result = ActionResult.model_validate(run.json())
    assert result.status == "failed"
    assert result.errors[0].code == "query_rowset_empty"
    assert result.errors[0].message == "cell.edit_query resolved zero rows to edit"
    after = {
        table: int(project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in ("ops", "edits", "receipts")
    }
    assert after == before
