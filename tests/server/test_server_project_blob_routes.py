from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from frisket.server.app import create_app


def test_project_blob_missing_digest_keeps_plain_404_detail(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path / "ws"))
    project_id = client.post("/api/projects", json={"name": "Blob misses"}).json()["id"]

    response = client.get(f"/api/projects/{project_id}/blobs/missing-digest")

    assert response.status_code == 404
    assert response.json() == {"detail": "no such blob"}
