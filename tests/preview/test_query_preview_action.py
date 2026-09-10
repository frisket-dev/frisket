from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from helpers import make_client as _client


def _seed_project(
    client: TestClient,
) -> tuple[str, int, dict[str, int], dict[str, int]]:
    pid = client.post("/api/projects", json={"name": "Query Preview"}).json()["id"]
    project = client.app.state.workspace.get(pid)
    sheet_id = project.add_sheet("tasks")
    columns = {
        "title": project.add_column(sheet_id, "title"),
        "status": project.add_column(sheet_id, "status"),
        "published": project.add_column(sheet_id, "published", type="date"),
    }
    row_ids = project.add_rows(
        sheet_id,
        [
            {"title": "A start", "status": "todo", "published": "2026-01-01"},
            {"title": "B done", "status": "done", "published": "2026-02-01"},
            {"title": "C done", "status": "done", "published": "2026-03-01"},
        ],
        columns,
    )
    return (
        pid,
        sheet_id,
        {"a": row_ids[0], "b": row_ids[1], "c": row_ids[2]},
        columns,
    )


def _query(sheet_id: int) -> dict:
    return {
        "schema_version": "frisket.query.v1",
        "kind": "sheet.filter",
        "scope": {"kind": "sheet", "sheet_id": sheet_id},
        "filter": {"status": {"eq": "done"}},
        "sort": [{"column": "published", "dir": "desc"}],
    }


def _action(query: dict, *, key: str = "query-preview@sha256:v1") -> dict:
    return {
        "action_id": "query.preview",
        "scope": {"kind": "project"},
        "params": {"query": query, "limit": 50, "offset": 0},
        "idempotency_key": key,
    }


def test_query_preview_route_and_action_share_rowset_and_receipt_replays(
    tmp_path: Path,
) -> None:
    from frisket.contracts.action import (
        ActionCatalog,
        ActionResult,
        Receipt,
    )
    from frisket.actions.registry import COPILOT_ACTION_IDS
    from frisket.actions.system import root_action_catalog, validate_root_action

    client = _client(tmp_path)
    pid, sheet_id, rows, columns = _seed_project(client)
    query = _query(sheet_id)

    catalog = ActionCatalog.model_validate(root_action_catalog())
    entry = next(item for item in catalog.actions if item.kind == "query.preview")
    assert "query.preview" not in COPILOT_ACTION_IDS
    assert entry.execution_mode == "whole_project"
    assert entry.async_mode == "sync"
    assert entry.writes_project is False
    assert entry.required_capabilities == ["project:read"]
    assert entry.receipt_policy == "writes_receipt"
    assert set(entry.side_effects) == {
        "read_query_rowset",
        "evaluate_local_embedding_query",
        "write_receipt",
    }
    assert entry.cost_policy.kind == "none"
    assert entry.cost_policy.requires_confirmation is False
    assert {
        "schema_version",
        "query",
        "query_hash",
        "sheet_id",
        "row_ids",
        "row_count",
        "total",
        "offset",
        "limit",
        "evaluator",
        "scores",
    } <= set(entry.output_schema["properties"])
    assert "receipt_id" not in entry.output_schema["properties"]

    validation = validate_root_action(_action(query))
    assert validation.ok, validation.error
    assert validation.action is not None
    assert validation.action.action_id == "query.preview"

    preview = client.post(
        f"/api/projects/{pid}/queries/v1/preview",
        json={"query": query, "limit": 50, "offset": 0},
    )
    assert preview.status_code == 200, preview.text
    preview_payload = preview.json()
    assert preview_payload["schema_version"] == "frisket.query_preview.v1"
    assert preview_payload["row_ids"] == [rows["c"], rows["b"]]
    assert preview_payload["row_count"] == 2
    assert preview_payload["total"] == 2
    assert preview_payload["query"]["kind"] == "sheet.filter"
    assert preview_payload["query_hash"].startswith("sha256:")
    assert preview_payload["evaluator"] == {
        "kind": "frisket.querysets.sheet_filter",
        "version": "v1",
    }

    run = client.post(f"/api/projects/{pid}/actions/v1/run", json=_action(query))
    assert run.status_code == 200, run.text
    result = ActionResult.model_validate(run.json())
    assert result.status == "completed"
    assert result.action.kind == "query.preview"
    assert result.op_ids == []
    assert result.run_id is None
    assert result.receipt_id is not None
    assert len(result.outputs) == 1
    assert result.outputs[0].kind == "query_preview"
    assert result.outputs[0].row_ids == preview_payload["row_ids"]
    output_ref = result.outputs[0].ref
    assert output_ref["kind"] == "query_preview"
    assert output_ref["query_hash"] == preview_payload["query_hash"]
    assert output_ref["row_ids"] == preview_payload["row_ids"]
    assert output_ref["total"] == preview_payload["total"]
    assert output_ref["evaluator"] == preview_payload["evaluator"]

    project = client.app.state.workspace.get(pid)
    receipt_row = project.db.execute(
        "SELECT * FROM receipts WHERE id=?", (result.receipt_id,)
    ).fetchone()
    assert receipt_row is not None
    receipt = Receipt.model_validate(json.loads(receipt_row["body"]))
    assert receipt.action_kind == "query.preview"
    assert receipt.status == "completed"
    assert receipt.run_id is None
    assert receipt.op_ids == []
    assert receipt.outputs[0].ref == output_ref
    ref_kinds = {item.ref["kind"] for item in receipt.evidence}
    assert ref_kinds == {"query_preview_rowset"}
    assert result.value == preview_payload
    assert receipt.value == preview_payload
    assert "query" not in output_ref
    assert "scores" not in output_ref

    project.apply_edits(
        [
            {
                "row_id": rows["c"],
                "column_id": columns["status"],
                "value": "archived",
            }
        ],
        label="move row out of preview",
    )

    changed_preview = client.post(
        f"/api/projects/{pid}/queries/v1/preview",
        json={"query": query, "limit": 50, "offset": 0},
    )
    assert changed_preview.status_code == 200, changed_preview.text
    assert changed_preview.json()["row_ids"] == [rows["b"]]

    replay = client.post(f"/api/projects/{pid}/actions/v1/run", json=_action(query))
    assert replay.status_code == 200, replay.text
    replay_result = ActionResult.model_validate(replay.json())
    assert replay_result.receipt_id == result.receipt_id
    assert replay_result.value == preview_payload
    assert replay_result.outputs[0].row_ids == [rows["c"], rows["b"]]
    assert (
        project.db.execute(
            "SELECT COUNT(*) FROM receipts WHERE action_kind='query.preview'"
        ).fetchone()[0]
        == 1
    )


def test_query_preview_route_and_action_return_typed_filter_errors(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    pid, sheet_id, _rows, _columns = _seed_project(client)
    bad_query = {
        "kind": "filter",
        "sheet_id": sheet_id,
        "filter": {"missing": {"eq": "done"}},
    }

    preview = client.post(
        f"/api/projects/{pid}/queries/v1/preview",
        json={"query": bad_query},
    )
    assert preview.status_code == 400
    assert preview.json()["detail"] == {
        "code": "invalid_query_filter",
        "message": "unknown filter column: missing",
        "field": "query.filter",
    }

    bad_limit = client.post(
        f"/api/projects/{pid}/queries/v1/preview",
        json={"query": {"kind": "filter", "sheet_id": sheet_id}, "limit": True},
    )
    assert bad_limit.status_code == 400
    assert bad_limit.json()["detail"] == {
        "code": "invalid_query_params",
        "message": "query.preview limit and offset must be integers",
        "field": "limit",
    }

    run = client.post(
        f"/api/projects/{pid}/actions/v1/run",
        json=_action(bad_query, key="query-preview-bad@sha256:v1"),
    )
    assert run.status_code == 400, run.text
    payload = run.json()
    assert payload["status"] == "failed"
    assert payload["errors"][0]["code"] == "invalid_query_filter"
    assert "unknown filter column" in payload["errors"][0]["message"]


@pytest.mark.parametrize(
    "params",
    (
        {"query": []},
        {"query": {"kind": "filter", "sheet_id": 1}, "limit": True},
        {"query": {"kind": "filter", "sheet_id": 1}, "limit": "5"},
        {"query": {"kind": "filter", "sheet_id": 1}, "offset": "0"},
    ),
)
def test_typed_query_preview_params_reject_malformed_shapes(params) -> None:
    from frisket.actions.system import validate_root_action

    result = validate_root_action(
        {
            "action_id": "query.preview",
            "scope": {"kind": "project"},
            "params": params,
            "idempotency_key": "typed-shape",
        }
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "invalid_action_request"
