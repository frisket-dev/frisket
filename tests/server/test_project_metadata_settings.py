from __future__ import annotations

import json


def test_project_metadata_settings_patch_updates_manifest(client) -> None:
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
        "usage_data_sharing": "inherit",
        "allowed_services": "all_services",
        "cover_seed": None,
        "starred": False,
        "archived": False,
    }
    manifest = json.loads(
        (
            client.app.state.workspace.root / f"{pid}.frisket" / "manifest.json"
        ).read_text()
    )
    assert manifest["name"] == "Metadata renamed"
    assert manifest["description"] == "Tracked in settings"


def test_project_metadata_settings_reject_sensitive_flag_without_mutating(
    client,
) -> None:
    created = client.post(
        "/api/projects", json={"name": "Sensitive", "usage_data_sharing": "off"}
    ).json()
    pid = created["id"]
    manifest_path = client.app.state.workspace.root / f"{pid}.frisket" / "manifest.json"

    response = client.patch(
        f"/api/projects/{pid}",
        json={"name": "Sensitive renamed", "sensitive": False},
    )

    assert response.status_code == 422, response.text
    metadata = client.get(f"/api/projects/{pid}").json()
    assert metadata["name"] == "Sensitive"
    assert metadata["sensitive"] is True
    assert json.loads(manifest_path.read_text())["sensitive"] is True
