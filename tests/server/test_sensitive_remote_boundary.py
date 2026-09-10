from __future__ import annotations

import json

from fastapi.testclient import TestClient

from frisket.server.app import create_app


def test_sensitive_project_flag_is_read_only_from_general_settings(tmp_path) -> None:
    # INVARIANT: the sensitive flag can never be changed remotely via
    # PATCH /api/projects/{pid}. The update wire model now declares
    # `model_config = ConfigDict(extra="forbid")`, so a `sensitive` key in the
    # PATCH body is rejected outright (422) rather than silently dropped — a
    # strictly stronger enforcement of the same boundary this test pins.
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = client.post("/api/projects", json={"name": "Sensitive"}).json()["id"]
    manifest_path = tmp_path / "workspace" / f"{pid}.frisket" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["sensitive"] = True
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    response = client.patch(
        f"/api/projects/{pid}",
        json={"name": "Sensitive renamed", "sensitive": False},
    )

    assert response.status_code == 422, response.text
    assert json.loads(manifest_path.read_text())["sensitive"] is True
