from __future__ import annotations

import json

from fastapi.testclient import TestClient

from frisket.contracts.action import ActionResult
from frisket.server.app import create_app
from action_test_helpers import typed_map_request
from http_test_helpers import drain_queue


CSV = "note\nCall 212-555-0123\nNo phone\n"


def test_http_column_runs_use_action_metadata(tmp_path) -> None:
    client = TestClient(create_app(tmp_path / "ws"))
    project_id = client.post("/api/projects", json={"name": "Column runs v1"}).json()[
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
            idempotency_key="column-runs-v1-regex@sha256:stable",
        ),
    )
    assert run.status_code == 200, run.text
    result = ActionResult.model_validate(run.json())
    assert result.status == "queued"
    assert result.run_id is not None
    assert result.job_id is not None
    drain_queue(client)

    data = client.get(
        f"/api/projects/{project_id}/sheets/{sheet_id}/data?offset=0&limit=0"
    )
    assert data.status_code == 200, data.text
    phone_column = next(c for c in data.json()["columns"] if c["name"] == "phone")

    response = client.get(
        f"/api/projects/{project_id}/columns/{phone_column['id']}/runs"
    )
    assert response.status_code == 200, response.text
    body = response.json()

    assert "recipe" not in json.dumps(body).lower()
    assert body["column"] == {
        "id": phone_column["id"],
        "name": "phone",
        "type": "text",
        "ai_generated": True,
        # regex_extract is generation-managed: the wire deliberately reports
        # no scalar current run for managed columns — exact heads own value
        # authority, and latest_run_id/mixed_origins carry the chronology.
        "current_run_id": None,
        "latest_run_id": result.run_id,
        "mixed_origins": False,
    }
    assert len(body["runs"]) == 1
    run_body = body["runs"][0]
    assert run_body == {
        "run_id": result.run_id,
        "action_kind": "map.regex_extract",
        "action_name": "Extract text with regex",
        "model": None,
        "status": "completed",
        "spec": run_body["spec"],
        "total_rows": 2,
        "completed_rows": 2,
        "failed_rows": 0,
        "cost_actual": 0.0,
        "started_at": run_body["started_at"],
        "finished_at": run_body["finished_at"],
        "duration_ms": run_body["duration_ms"],
        "tokens_in": None,
        "tokens_out": None,
        "human_score": {"graded": 0, "passed": 0},
        "judge_scores": [],
        # Managed columns have no scalar "current" run — exact heads own
        # value authority — so the run-level flag stays False and the
        # column-level latest_run_id above carries the chronology.
        "current": False,
    }
    spec = run_body["spec"]
    assert {
        key: spec[key]
        for key in (
            "action_kind",
            "action_version",
            "sheet_id",
            "input_columns",
            "output_names",
            "params",
        )
    } == {
        "action_kind": "map.regex_extract",
        "action_version": "1",
        "sheet_id": sheet_id,
        "input_columns": ["note"],
        "output_names": {"extracted": "phone"},
        "params": {
            "input_columns": ["note"],
            "pattern": r"\d{3}-\d{3}-\d{4}",
        },
    }
