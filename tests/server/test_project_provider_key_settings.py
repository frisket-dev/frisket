from __future__ import annotations

from fastapi.testclient import TestClient

from frisket.team.security.secrets import decrypt_secret
from frisket.server import provider_config
from frisket.server.app import create_app


def test_project_provider_keys_are_redacted_and_used_by_project_router(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        provider_config,
        "probe_provider",
        lambda provider, key, **k: {
            "provider": provider,
            "ok": True,
            "reachable": True,
            "status": 200,
            "detail": None,
        },
    )
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = client.post("/api/projects", json={"name": "Provider Keys"}).json()["id"]

    initial = client.get(f"/api/projects/{pid}/provider-keys")
    assert initial.status_code == 200, initial.text
    assert {row["id"] for row in initial.json()["providers"]} >= {
        "anthropic",
        "openai",
    }

    validated = client.post(
        f"/api/projects/{pid}/provider-keys/validate",
        json={"provider": "openai", "key": "sk-project-openai"},
    )
    assert validated.status_code == 200, validated.text
    saved = client.post(
        f"/api/projects/{pid}/provider-keys",
        json={
            "provider": "openai",
            "key": "sk-project-openai",
            "spend_cap_usd": 7,
            "validation_token": validated.json()["validation_token"],
        },
    )
    assert saved.status_code == 200, saved.text
    rendered = saved.text
    assert "sk-project-openai" not in rendered
    openai = next(row for row in saved.json()["providers"] if row["id"] == "openai")
    assert openai["configured"] is True
    assert openai["hint"] == "...enai"
    assert openai["spend_cap_usd"] == 7

    project = client.app.state.workspace.get(pid)
    row = project.db.execute(
        "SELECT encrypted FROM project_provider_keys WHERE provider='openai'"
    ).fetchone()
    assert row is not None
    assert decrypt_secret(str(row["encrypted"])) == "sk-project-openai"
    assert (
        client.app.state.workspace.router_for(project).adapter_for("openai").api_key
        == "sk-project-openai"
    )


def test_project_provider_keys_reject_unknown_provider(tmp_path) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = client.post("/api/projects", json={"name": "Provider Keys"}).json()["id"]

    response = client.post(
        f"/api/projects/{pid}/provider-keys",
        json={"provider": "unknown", "key": "sk-nope"},
    )

    assert response.status_code == 400
    assert "sk-nope" not in response.text
