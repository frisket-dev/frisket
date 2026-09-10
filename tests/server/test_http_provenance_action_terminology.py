from __future__ import annotations

import json

from fastapi.testclient import TestClient

from frisket.contracts.action import ActionResult
from frisket.server.app import create_app
from action_test_helpers import typed_map_request
from http_test_helpers import drain_queue


CSV = "note\nCall 212-555-0123\nNo phone\n"


def test_http_project_provenance_uses_run_kind_public_shape(tmp_path) -> None:
    client = TestClient(create_app(tmp_path / "ws"))
    project_id = client.post("/api/projects", json={"name": "Provenance v1"}).json()[
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
        params={},
        json=typed_map_request(
            "map.regex_extract",
            sheet_id,
            params={"input_columns": ["note"], "pattern": r"\d{3}-\d{3}-\d{4}"},
            output_names={"extracted": "phone"},
            idempotency_key="provenance-v1-regex@sha256:stable",
        ),
    )
    assert run.status_code == 200, run.text
    result = ActionResult.model_validate(run.json())
    assert result.status == "queued"
    assert result.run_id is not None
    assert result.job_id is not None
    drain_queue(client)

    response = client.get(f"/api/projects/{project_id}/provenance")
    assert response.status_code == 200, response.text
    body = response.json()

    assert "recipes" not in body
    assert "action_kinds" in body
    assert "recipe" not in json.dumps(body).lower()
    assert body["action_kinds"] == [
        {
            "action_kind": "map.regex_extract",
            "action_name": "Extract text with regex",
            "runs": 1,
            "rows": 2,
            "failed_rows": 0,
            "cost": 0.0,
        }
    ]
    assert body["runs"] == [
        {
            "run_id": result.run_id,
            "sheet_id": sheet_id,
            "action_kind": "map.regex_extract",
            "action_name": "Extract text with regex",
            "model": None,
            "provider": None,
            "status": "completed",
            "total_rows": 2,
            "completed_rows": 2,
            "failed_rows": 0,
            "cost": 0.0,
            "started_at": body["runs"][0]["started_at"],
            "finished_at": body["runs"][0]["finished_at"],
        }
    ]
