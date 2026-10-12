from fastapi.testclient import TestClient

from frisket.server.app import create_app


def test_project_action_data_routes_preserve_http_contract(tmp_path) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = client.post("/api/projects", json={"name": "Action Utilities"}).json()["id"]
    project = client.app.state.workspace.get(pid)
    sheet_id = project.add_sheet("people")
    columns = {"name": project.add_column(sheet_id, "name", type="text")}
    project.add_rows(sheet_id, [{"name": "Ada"}], columns)

    sheets = client.get(f"/api/projects/{pid}/actions/sheets")
    assert sheets.status_code == 200, sheets.text
    sheets_body = sheets.json()
    assert sheets_body["action"] == "list_sheets"
    assert sheets_body["sheets"][0]["id"] == sheet_id

    page = client.post(
        f"/api/projects/{pid}/actions/read-range",
        json={"sheet_id": sheet_id, "columns": ["name"], "limit": 1},
    )
    assert page.status_code == 200, page.text
    assert page.json()["rows"] == [
        {"row_id": 1, "row_index": 1, "cells": {"name": "Ada"}}
    ]

    missing = client.post(
        f"/api/projects/{pid}/actions/read-range",
        json={"sheet_id": 9999, "limit": 1},
    )
    assert missing.status_code == 404
