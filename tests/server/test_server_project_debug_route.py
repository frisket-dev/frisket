from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from frisket.server.app import create_app


def test_project_debug_does_not_create_response_cache_sidecar(tmp_path: Path) -> None:
    app = create_app(tmp_path / "ws")
    client = TestClient(app)
    project_id = client.post("/api/projects", json={"name": "Debug sidecar"}).json()[
        "id"
    ]
    project = app.state.workspace.get(project_id)
    cache_file = project.path / "project.cache.db"
    assert not cache_file.exists()

    response = client.get(f"/api/projects/{project_id}/debug")

    assert response.status_code == 200, response.text
    assert response.json()["cache"] == {
        "mode": "replay",
        "enabled": False,
        "entries": 0,
    }
    assert not cache_file.exists()
