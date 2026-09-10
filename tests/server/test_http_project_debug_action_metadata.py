from __future__ import annotations

from fastapi.testclient import TestClient

from frisket.contracts.action import ActionResult
from frisket.server.app import create_app
from action_test_helpers import typed_map_request
from http_test_helpers import drain_queue


CSV = "note\nCall 212-555-0123\nNo phone\n"


def _assert_no_recipe_keys(value: object) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            assert key not in {"recipe", "recipe_version"}
            _assert_no_recipe_keys(child)
    elif isinstance(value, list):
        for child in value:
            _assert_no_recipe_keys(child)


def test_project_debug_runs_and_lineage_use_action_metadata(tmp_path) -> None:
    client = TestClient(create_app(tmp_path / "ws"))
    project_id = client.post("/api/projects", json={"name": "Debug v1"}).json()["id"]
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
            idempotency_key="project-debug-v1-regex@sha256:stable",
        ),
    )
    assert run.status_code == 200, run.text
    result = ActionResult.model_validate(run.json())
    assert result.status == "queued"
    assert result.run_id is not None
    assert result.job_id is not None
    drain_queue(client)

    response = client.get(f"/api/projects/{project_id}/debug")
    assert response.status_code == 200, response.text
    body = response.json()

    debug_run = next(r for r in body["runs"] if r["run_id"] == result.run_id)
    _assert_no_recipe_keys(debug_run)
    assert debug_run["action_kind"] == "map.regex_extract"
    assert debug_run["action_kind"] == "map.regex_extract"
    assert debug_run["action_name"] == "Extract text with regex"
    assert debug_run["action_version"] == "1"
    assert debug_run["status"] == "completed"

    sheet = next(s for s in body["lineage"] if s["sheet_id"] == sheet_id)
    phone_column = next(c for c in sheet["columns"] if c["name"] == "phone")
    derived_from = phone_column["derived_from"]
    _assert_no_recipe_keys(derived_from)
    assert derived_from["run_id"] == result.run_id
    assert derived_from["action_kind"] == "map.regex_extract"
    assert derived_from["action_kind"] == "map.regex_extract"
    assert derived_from["action_name"] == "Extract text with regex"
    assert derived_from["action_version"] == "1"
    assert derived_from["input_columns"] == ["note"]
