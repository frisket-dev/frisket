"""Public project-configuration route contracts and redaction invariants."""

from __future__ import annotations

from fastapi.testclient import TestClient
from pydantic import ValidationError
import pytest

from frisket.contracts.http.project_config import ProjectProviderKeyValidationResponse
from frisket.server import provider_config
from frisket.server.app import create_app
from frisket.team.app import TeamConfig, create_team_app
from tests.team_setup_helpers import claim_server


def test_project_config_routes_have_typed_successes_and_preserve_wire_rules(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        provider_config,
        "probe_provider",
        lambda provider, key, **_kwargs: {
            "provider": provider,
            "ok": True,
            "reachable": True,
            "status": 200,
            "detail": None,
        },
    )
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = client.post("/api/projects", json={"name": "Config"}).json()["id"]
    spec = client.app.openapi()

    expected = {
        ("/api/projects/{pid}/retention", "get"),
        ("/api/projects/{pid}/retention", "patch"),
        ("/api/projects/{pid}/network", "get"),
        ("/api/projects/{pid}/network", "patch"),
        ("/api/projects/{pid}/settings", "get"),
        ("/api/projects/{pid}/settings", "patch"),
        ("/api/projects/{pid}/compact", "post"),
        ("/api/projects/{pid}/provider-keys", "get"),
        ("/api/projects/{pid}/provider-keys", "post"),
        ("/api/projects/{pid}/provider-keys/validate", "post"),
        ("/api/projects/{pid}/provider-keys/{provider}", "delete"),
        ("/api/projects/{pid}/secrets", "get"),
        ("/api/projects/{pid}/secrets", "post"),
        ("/api/projects/{pid}/secrets/{name}", "delete"),
    }
    assert all(
        "200" in spec["paths"][path][method]["responses"] for path, method in expected
    )
    assert all(
        "409" not in spec["paths"][path][method]["responses"]
        for path, method in expected
    )

    # Settings alone rejects unknown fields. The older config endpoints retain
    # their coercive/default-extra-ignore behavior at the public boundary.
    assert (
        client.patch(f"/api/projects/{pid}/settings", json={"typo": True}).status_code
        == 422
    )
    retained = client.patch(
        f"/api/projects/{pid}/retention",
        json={"no_compact": "true", "ignored": "value"},
    )
    assert retained.status_code == 200
    assert retained.json()["no_compact"] is True

    validated = client.post(
        f"/api/projects/{pid}/provider-keys/validate",
        json={"provider": "openai", "key": "secret-provider-key"},
    )
    assert validated.status_code == 200
    assert validated.json()["validation_token"]
    saved = client.post(
        f"/api/projects/{pid}/provider-keys",
        json={
            "provider": "openai",
            "key": "secret-provider-key",
            "spend_cap_usd": 7,
            "validation_token": validated.json()["validation_token"],
        },
    )
    assert saved.status_code == 200
    assert "secret-provider-key" not in saved.text
    provider = next(row for row in saved.json()["providers"] if row["id"] == "openai")
    assert isinstance(provider["spent_usd"], (int, float))
    assert isinstance(provider["spend_cap_usd"], float)

    secret = client.post(
        f"/api/projects/{pid}/secrets",
        json={"name": "example_key", "value": "secret-value"},
    )
    assert secret.status_code == 200
    assert "secret-value" not in secret.text
    assert secret.json()["schemaVersion"] == "frisket.project_secrets.v1"


def test_network_and_secret_conflict_responses_preserve_existing_wire_bytes(
    tmp_path,
) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = client.post("/api/projects", json={"name": "Config"}).json()["id"]
    project = client.app.state.workspace.get(pid)
    project.db.execute(
        "INSERT INTO project_secret_migration_conflicts "
        "(plugin_id, name, hint, status, created_at) VALUES (?, ?, ?, ?, ?)",
        ("weather-plugin", "WEATHER_API_KEY", None, "needs_resolution", "2026-01-02"),
    )
    project.db.commit()

    # Local GET/PATCH retain the materialized (possibly null) org default.
    initial = client.get(f"/api/projects/{pid}/network")
    assert initial.status_code == 200, initial.text
    assert initial.json()["org_default"] is None
    updated = client.patch(f"/api/projects/{pid}/network", json={"mode": "off"})
    assert updated.status_code == 200, updated.text
    assert updated.json()["org_default"] is None

    # This comes directly from the existing SQLite select and the Settings UI
    # already reads `plugin_id`; project-config must not silently camel-case it.
    conflicts = client.get(f"/api/projects/{pid}/secrets").json()["conflicts"]
    assert conflicts == [
        {
            "plugin_id": "weather-plugin",
            "name": "WEATHER_API_KEY",
            "hint": None,
            "status": "needs_resolution",
            "created_at": "2026-01-02",
        }
    ]


def test_team_network_twin_and_validate_optionality(tmp_path) -> None:
    app = create_team_app(
        TeamConfig(
            database_url=f"sqlite:///{tmp_path / 'control.db'}",
            run_queue_database_url=f"sqlite:///{tmp_path / 'queue.db'}",
            data_dir=tmp_path / "team-data",
            base_url="http://testserver",
            organization_name="Project Config",
            admin_emails={"owner@example.com"},
        )
    )
    browser = claim_server(app)
    pid = browser.post("/api/projects", json={"name": "Team Config"}).json()["id"]
    browser.patch("/api/org/network", json={"network_default": "off"})
    response = browser.patch(f"/api/projects/{pid}/network", json={"mode": "inherit"})
    assert response.status_code == 200, response.text
    assert response.json() == {
        "schemaVersion": "frisket.project_network_policy.v1",
        "mode": "inherit",
        "org_default": "off",
        "effective": "off",
    }

    nullable = ProjectProviderKeyValidationResponse.model_validate(
        {
            "provider": "openai",
            "ok": False,
            "reachable": False,
            "status": None,
            "detail": None,
        }
    )
    assert nullable.model_dump(exclude_unset=True)["status"] is None
    assert "validation_token" not in nullable.model_dump(exclude_unset=True)
    with pytest.raises(ValidationError):
        ProjectProviderKeyValidationResponse.model_validate(
            {
                "provider": "openai",
                "ok": True,
                "reachable": True,
                "status": 200,
                "detail": None,
                "validation_token": None,
            }
        )
