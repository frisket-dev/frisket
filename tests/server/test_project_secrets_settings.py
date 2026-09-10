from __future__ import annotations

from fastapi.testclient import TestClient

from frisket.team.security.secrets import decrypt_secret
from frisket.server.app import create_app


def test_project_secrets_are_write_only_redacted_and_deletable(tmp_path) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = client.post("/api/projects", json={"name": "Secrets"}).json()["id"]

    saved = client.post(
        f"/api/projects/{pid}/secrets",
        json={"name": "weather_api_key", "value": "weather-secret"},
    )

    assert saved.status_code == 200, saved.text
    assert "weather-secret" not in saved.text
    assert saved.json()["secrets"][0]["name"] == "WEATHER_API_KEY"
    assert saved.json()["secrets"][0]["hint"] == "...cret"

    project = client.app.state.workspace.get(pid)
    row = project.db.execute(
        "SELECT encrypted FROM project_secrets WHERE name='WEATHER_API_KEY'"
    ).fetchone()
    assert row is not None
    assert decrypt_secret(str(row["encrypted"])) == "weather-secret"

    deleted = client.delete(f"/api/projects/{pid}/secrets/WEATHER_API_KEY")
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True
    assert client.get(f"/api/projects/{pid}/secrets").json()["secrets"] == []
