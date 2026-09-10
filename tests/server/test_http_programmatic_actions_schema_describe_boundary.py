from __future__ import annotations

from fastapi.testclient import TestClient

from action_test_helpers import typed_map_request
from frisket.contracts.action import ActionResult
from frisket.server.app import create_app


def _assert_no_recipe_keys(value: object) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            assert key not in {"recipe", "recipe_version"}
            _assert_no_recipe_keys(child)
    elif isinstance(value, list):
        for child in value:
            _assert_no_recipe_keys(child)


def test_programmatic_actions_schema_and_describe_use_action_metadata(
    tmp_path,
) -> None:
    client = TestClient(create_app(tmp_path / "ws"))
    project_id = client.post("/api/projects", json={"name": "Programmatic v1"}).json()[
        "id"
    ]
    imported = client.post(
        f"/api/projects/{project_id}/import/csv",
        files={"file": ("people.csv", "name\nAda\nGrace\n", "text/csv")},
    )
    assert imported.status_code == 200, imported.text
    sheet_id = imported.json()["sheet_id"]

    run = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json=typed_map_request(
            "map.template",
            sheet_id,
            params={"template": {"text": "{{name}}"}},
            output_names={"rendered": "name_copy"},
            idempotency_key="programmatic-actions-describe@sha256:stable",
        ),
    )
    assert run.status_code == 200, run.text
    result = ActionResult.model_validate(run.json())
    assert result.run_id is not None

    schema = client.get("/api/actions/schema")
    assert schema.status_code == 200, schema.text
    schema_body = schema.json()
    action_names = {action["name"] for action in schema_body["actions"]}
    assert "run_action" in action_names
    assert "run_recipe" not in action_names
    _assert_no_recipe_keys(schema_body)

    described = client.get(f"/api/projects/{project_id}/actions/describe")
    assert described.status_code == 200, described.text
    body = described.json()
    assert "run_action" in body["actions"]
    assert "run_recipe" not in body["actions"]
    run_row = next(row for row in body["runs"] if row["id"] == result.run_id)
    _assert_no_recipe_keys(run_row)
    assert run_row["action_kind"] == "map.template"
    assert run_row["action_kind"] == "map.template"
    assert run_row["action_name"] == "Template"
