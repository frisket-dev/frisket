"""Selector reads are part of the actual app, including incomplete action drafts."""

from fastapi.testclient import TestClient

from frisket.server.app import create_app


def test_application_serves_project_selector_for_an_incomplete_draft(tmp_path):
    app = create_app(tmp_path / "workspace", serve_spa=False)
    project = app.state.workspace.create("Choices", project_id="choices")
    with TestClient(app) as client:
        response = client.post(
            f"/api/projects/{project['id']}/selector-choices",
            json={
                "schema_version": "frisket.selector_choices_query.v1",
                "subject": {
                    "kind": "action",
                    "action_id": "map.translate",
                    "field": "engine",
                    "params": {},
                },
            },
        )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["schema_version"] == "frisket.selector_choices.v1"
    assert payload["project_id"] == project["id"]
    assert payload["groups"]
