from __future__ import annotations

from fastapi.testclient import TestClient
import pytest

from frisket.team.security.secrets import decrypt_secret
from frisket.project_settings import (
    ProjectSettings,
    read_project_settings,
    write_project_settings,
)
from frisket.server import provider_config
from frisket.server.app import create_app
from frisket.server.services.projects import ProjectLifecycleService


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


def test_key_save_commits_route_pin_and_key_together(tmp_path, monkeypatch) -> None:
    app = create_app(tmp_path / "workspace")
    project_id = app.state.workspace.create("Atomic provider key")["id"]
    project = app.state.workspace.get(project_id)
    write_project_settings(project, ProjectSettings())
    service = ProjectLifecycleService(app.state.workspace)

    class Routes:
        def pin_ready_routes(self, pinned_project_id, *, commit=True):
            assert pinned_project_id == project_id
            assert commit is False
            assert project.db.in_transaction
            settings = read_project_settings(project).model_copy(
                update={"model_execution_routes": {"openai/gpt-5-mini": "own"}}
            )
            return write_project_settings(project, settings, commit=False)

    service._model_routes = Routes()
    monkeypatch.setattr(provider_config, "require_validation_token", lambda *_a: None)

    service.set_project_provider_key(
        project_id,
        provider="openai",
        key="sk-atomic",
    )

    assert read_project_settings(project).model_execution_routes == {
        "openai/gpt-5-mini": "own"
    }
    assert project.provider_model_keys() == {"openai": "sk-atomic"}


def test_key_save_failure_rolls_back_route_pin_and_key(tmp_path, monkeypatch) -> None:
    app = create_app(tmp_path / "workspace")
    project_id = app.state.workspace.create("Failed provider key")["id"]
    project = app.state.workspace.get(project_id)
    write_project_settings(project, ProjectSettings())
    service = ProjectLifecycleService(app.state.workspace)

    class Routes:
        def pin_ready_routes(self, _project_id, *, commit=True):
            assert commit is False
            settings = read_project_settings(project).model_copy(
                update={"model_execution_routes": {"openai/gpt-5-mini": "own"}}
            )
            return write_project_settings(project, settings, commit=False)

    service._model_routes = Routes()
    original_set = project.set_provider_key

    def write_then_fail(**kwargs):
        original_set(**kwargs)
        raise RuntimeError("simulated key write failure")

    monkeypatch.setattr(project, "set_provider_key", write_then_fail)
    monkeypatch.setattr(provider_config, "require_validation_token", lambda *_a: None)

    with pytest.raises(RuntimeError, match="simulated key write failure"):
        service.set_project_provider_key(
            project_id,
            provider="openai",
            key="sk-rollback",
        )

    assert read_project_settings(project).model_execution_routes is None
    assert project.provider_model_keys() == {}
