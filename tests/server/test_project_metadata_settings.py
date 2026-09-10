from __future__ import annotations

import json

from fastapi.testclient import TestClient

from frisket.server.app import create_app


def test_project_metadata_settings_patch_updates_manifest(tmp_path) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    created = client.post("/api/projects", json={"name": "Metadata"}).json()
    pid = created["id"]

    response = client.patch(
        f"/api/projects/{pid}",
        json={"name": "Metadata renamed", "description": "Tracked in settings"},
    )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "id": pid,
        "name": "Metadata renamed",
        "description": "Tracked in settings",
        "sensitive": False,
        # Shell/Home flags (workbench-ia-shell-v1): default False, unchanged here.
        "starred": False,
        "archived": False,
    }
    manifest = json.loads(
        (tmp_path / "workspace" / f"{pid}.frisket" / "manifest.json").read_text()
    )
    assert manifest["name"] == "Metadata renamed"
    assert manifest["description"] == "Tracked in settings"


def test_project_metadata_settings_reject_sensitive_flag_without_mutating(
    tmp_path,
) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    created = client.post("/api/projects", json={"name": "Sensitive"}).json()
    pid = created["id"]
    manifest_path = tmp_path / "workspace" / f"{pid}.frisket" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["sensitive"] = True
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    response = client.patch(
        f"/api/projects/{pid}",
        json={"name": "Sensitive renamed", "sensitive": False},
    )

    assert response.status_code == 422, response.text
    metadata = client.get(f"/api/projects/{pid}").json()
    assert metadata["name"] == "Sensitive"
    assert metadata["sensitive"] is True
    assert json.loads(manifest_path.read_text())["sensitive"] is True
