from __future__ import annotations

from fastapi.testclient import TestClient

from frisket.ai.llm import ModelRouter
from frisket.server.app import create_app


def test_sheet_data_preserves_logical_datetime_metadata_and_exact_value(
    tmp_path,
) -> None:
    client = TestClient(
        create_app(
            tmp_path / "workspace", router=ModelRouter(cache=None, cache_mode="off")
        )
    )
    project_id = client.post("/api/projects", json={"name": "Dates"}).json()["id"]
    project = client.app.state.workspace.get(project_id)
    sheet_id = project.add_sheet("Dates")
    column_id = project.add_column(sheet_id, "Received", type="datetime")
    value = "2026-10-18T12:30:45.123456-05:30"
    project.add_rows(sheet_id, [{"Received": value}], {"Received": column_id})

    response = client.get(f"/api/projects/{project_id}/sheets/{sheet_id}/data")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["columns"][0]["type"] == "datetime"
    assert payload["rows"][0]["cells"][str(column_id)] == value

    sheets = client.get(f"/api/projects/{project_id}/sheets").json()
    assert sheets[0]["columns"][0]["type"] == "datetime"
