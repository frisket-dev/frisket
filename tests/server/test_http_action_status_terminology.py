from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from frisket.contracts.action import ActionResult
from frisket.server.app import create_app
from action_test_helpers import typed_map_request
from helpers import write_claimless_test_model_calls
from http_test_helpers import drain_queue


CSV = "note\nCall 212-555-0123\nNo phone\n"
ROOT = Path(__file__).resolve().parents[2]


def _assert_no_recipe_keys(value: object) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            assert key not in {"recipe", "recipe_version"}
            _assert_no_recipe_keys(child)
    elif isinstance(value, list):
        for child in value:
            _assert_no_recipe_keys(child)


def test_action_run_status_uses_action_metadata_without_recipe_bridge(tmp_path) -> None:
    client = TestClient(create_app(tmp_path / "ws"))
    project_id = client.post("/api/projects", json={"name": "Status v1"}).json()["id"]
    imported = client.post(
        f"/api/projects/{project_id}/import/csv",
        files={"file": ("calls.csv", CSV, "text/csv")},
    )
    assert imported.status_code == 200, imported.text
    sheet_id = imported.json()["sheet_id"]

    run = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json=typed_map_request(
            "map.regex_extract",
            sheet_id,
            params={"input_columns": ["note"], "pattern": r"\d{3}-\d{3}-\d{4}"},
            output_names={"extracted": "phone"},
            idempotency_key="action-status-v1-regex@sha256:stable",
        ),
    )
    assert run.status_code == 200, run.text
    result = ActionResult.model_validate(run.json())
    assert result.status == "queued"
    assert result.run_id is not None
    assert result.job_id is not None
    drain_queue(client)

    response = client.get(
        f"/api/projects/{project_id}/actions/runs/{result.run_id}/status"
    )
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["schema_version"] == "frisket.actions.v1"
    assert body["action"] == "run_status"
    assert body["project_id"] == project_id
    _assert_no_recipe_keys(body)

    run_status = body["run"]
    assert run_status["id"] == result.run_id
    assert run_status["sheet_id"] == sheet_id
    assert run_status["action_kind"] == "map.regex_extract"
    assert run_status["action_kind"] == "map.regex_extract"
    assert run_status["action_name"] == "Extract text with regex"
    assert run_status["status"] == "completed"
    assert run_status["total_rows"] == 2
    assert run_status["completed_rows"] == 2
    assert run_status["failed_rows"] == 0

    public_status = run_status["public_status"]
    assert public_status["run_id"] == result.run_id
    assert public_status["action_kind"] == "map.regex_extract"
    assert public_status["action_kind"] == "map.regex_extract"
    assert public_status["action_name"] == "Extract text with regex"
    assert public_status["status"] == "completed"
    assert public_status["total"] == 2
    assert public_status["completed"] == 2
    assert public_status["failed"] == 0


def test_action_run_status_presents_unknown_cost_as_null_not_zero(tmp_path) -> None:
    """A run whose live calls include an unknown provider cost projects
    ``runs.cost_actual`` NULL, and the status boundary passes that through as
    JSON null on both ``run.cost_actual`` and ``public_status.cost`` — never a
    confident 0."""

    client = TestClient(create_app(tmp_path / "ws"))
    project_id = client.post("/api/projects", json={"name": "Unknown cost"}).json()[
        "id"
    ]
    imported = client.post(
        f"/api/projects/{project_id}/import/csv",
        files={"file": ("calls.csv", CSV, "text/csv")},
    )
    assert imported.status_code == 200, imported.text
    sheet_id = imported.json()["sheet_id"]

    run = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json=typed_map_request(
            "map.regex_extract",
            sheet_id,
            params={"input_columns": ["note"], "pattern": r"\d{3}-\d{3}-\d{4}"},
            output_names={"extracted": "phone"},
            idempotency_key="action-status-null-cost@sha256:stable",
        ),
    )
    assert run.status_code == 200, run.text
    result = ActionResult.model_validate(run.json())
    assert result.run_id is not None
    drain_queue(client)

    project = client.app.state.workspace.get(project_id)
    row_id = int(project.db.execute("SELECT MIN(id) FROM rows").fetchone()[0])
    write_claimless_test_model_calls(
        project,
        result.run_id,
        [
            {
                "row_id": row_id,
                "column_id": None,
                "model_calls": [
                    {
                        "id": "unpriced-live-call",
                        "fact_version": "frisket.model-call-fact.v1",
                        "capability": "classify",
                        "engine": "fixture",
                        "provider": "fixture",
                        "provider_kind": "test",
                        "credential_source": "platform_key",
                        "provider_cost_usd": None,
                    }
                ],
            }
        ],
    )
    project.db.commit()

    response = client.get(
        f"/api/projects/{project_id}/actions/runs/{result.run_id}/status"
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["run"]["cost_actual"] is None
    assert body["run"]["public_status"]["cost"] is None
