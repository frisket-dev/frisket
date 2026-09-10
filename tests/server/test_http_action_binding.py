from __future__ import annotations

import json
from typing import Any

from fastapi.testclient import TestClient

from frisket.contracts.action import ActionCatalog, ActionResult, Receipt
from frisket.ai.llm import ModelRouter
from frisket.server.app import create_app


def _client(tmp_path) -> TestClient:
    return TestClient(
        create_app(
            tmp_path / "workspace",
            router=ModelRouter(cache=None, cache_mode="off"),
        )
    )


def _import_rows_action(
    *,
    sheet_name: str = "HTTP Rows",
    idempotency_key: str | None = "http-import-rows@sha256:stable",
) -> dict[str, Any]:
    return {
        "action_id": "import.rows",
        "scope": {"kind": "project"},
        "sheet_name": sheet_name,
        "params": {
            "columns": [
                {"name": "headline", "type": "text"},
                {"name": "source_url", "type": "url"},
                {"name": "score", "type": "integer"},
            ],
            "rows": [
                {
                    "headline": "City hall awarded a no-bid contract.",
                    "source_url": "https://example.com/story/1",
                    "score": 9,
                },
                {
                    "headline": "Routine road work finished early.",
                    "source_url": "https://example.com/story/2",
                    "score": 2,
                },
            ],
            "source": {
                "kind": "inline",
                "label": "http seed fixture",
                "fingerprint": "sha256:http-stable",
            },
        },
        "idempotency_key": idempotency_key,
    }


def _table_count(client: TestClient, project_id: str, table: str) -> int:
    project = client.app.state.workspace.get(project_id)
    return int(project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def test_http_v1_action_catalog_and_import_rows_executor_binding(tmp_path) -> None:
    client = _client(tmp_path)
    project_id = client.post("/api/projects", json={"name": "HTTP v1"}).json()["id"]

    catalog_response = client.get("/api/actions/v1/catalog")
    assert catalog_response.status_code == 200, catalog_response.text
    catalog = catalog_response.json()
    assert catalog["schema_version"] == "frisket.action_catalog.v2"
    assert {entry["kind"] for entry in catalog["actions"]} >= {"import.rows"}
    assert "run_recipe" not in json.dumps(catalog)
    ActionCatalog.model_validate(catalog)

    response = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json=_import_rows_action(),
    )
    assert response.status_code == 200, response.text
    result = ActionResult.model_validate(response.json())
    assert result.schema_version == "frisket.action_result.v1"
    assert result.project_id == project_id
    assert result.status == "completed"
    assert result.action.kind == "import.rows"
    assert result.receipt_id is not None
    assert result.op_ids == [1]
    assert [output.kind for output in result.outputs] == [
        "sheet",
        "column",
        "column",
        "column",
        "rows",
    ]

    project = client.app.state.workspace.get(project_id)
    sheet = project.db.execute("SELECT * FROM sheets WHERE name='HTTP Rows'").fetchone()
    assert sheet is not None
    assert project.row_count(int(sheet["id"])) == 2
    receipt_row = project.db.execute(
        "SELECT * FROM receipts WHERE id=?",
        (result.receipt_id,),
    ).fetchone()
    assert receipt_row is not None
    assert receipt_row["action_kind"] == "import.rows"
    receipt = Receipt.model_validate(json.loads(receipt_row["body"]))
    assert receipt.schema_version == "frisket.receipt.v1"
    assert receipt.project_id == project_id

    needs_confirmation_response = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json={
            "action_id": "research.web_search",
            "scope": {"kind": "sheet_rows", "sheet_id": int(sheet["id"])},
            "params": {
                "query": {"text": "background on {{headline}}"},
                "max_results": 2,
            },
            "output_names": {"search_results": "search_results"},
            "idempotency_key": "http-research-search@sha256:needs-confirmation",
        },
    )
    assert needs_confirmation_response.status_code == 402
    needs_confirmation = ActionResult.model_validate(needs_confirmation_response.json())
    assert needs_confirmation.status == "needs_confirmation"
    assert needs_confirmation.errors[0].schema_version == "frisket.action_error.v1"
    assert needs_confirmation.errors[0].code == "external_cost_requires_confirmation"

    before = {
        "sheets": _table_count(client, project_id, "sheets"),
        "columns": _table_count(client, project_id, "columns"),
        "rows": _table_count(client, project_id, "rows"),
        "ops": _table_count(client, project_id, "ops"),
        "receipts": _table_count(client, project_id, "receipts"),
    }
    replay_response = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json=_import_rows_action(),
    )
    assert replay_response.status_code == 200, replay_response.text
    replay_result = ActionResult.model_validate(replay_response.json())
    assert replay_result.receipt_id == result.receipt_id
    assert replay_result.op_ids == result.op_ids
    assert {
        "sheets": _table_count(client, project_id, "sheets"),
        "columns": _table_count(client, project_id, "columns"),
        "rows": _table_count(client, project_id, "rows"),
        "ops": _table_count(client, project_id, "ops"),
        "receipts": _table_count(client, project_id, "receipts"),
    } == before

    conflict_response = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json=_import_rows_action(sheet_name="Different Rows"),
    )
    assert conflict_response.status_code == 409, conflict_response.text
    conflict = ActionResult.model_validate(conflict_response.json())
    assert conflict.status == "failed"
    assert conflict.errors[0].schema_version == "frisket.action_error.v1"
    assert conflict.errors[0].code == "idempotency_conflict"

    invalid_request = _import_rows_action(
        idempotency_key="http-import-rows@sha256:invalid-scope",
    )
    invalid_request["scope"] = {"kind": "sheet_rows", "sheet_id": int(sheet["id"])}
    invalid_response = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json=invalid_request,
    )
    assert invalid_response.status_code == 400, invalid_response.text
    invalid = ActionResult.model_validate(invalid_response.json())
    assert invalid.status == "failed"
    assert invalid.errors[0].schema_version == "frisket.action_error.v1"
    assert invalid.errors[0].code == "invalid_action_request"
    assert {
        table: _table_count(client, project_id, table) for table in before
    } == before
