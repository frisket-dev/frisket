from __future__ import annotations
from frisket.execution.purchases import LOCAL_PURCHASE_POLICY

import json

import pytest
from fastapi.testclient import TestClient

from frisket.ai.llm import ModelRouter
from frisket.engine.store import Project
from frisket.local_runtime_config import (
    InvalidLocalRuntimeConfigError,
    resolve_local_allowance_usd,
)
from frisket.server.app import create_app
from frisket.server.runtime_settings import (
    InvalidRuntimeSettingsError,
    resolve_workspace_allowance_usd,
    runtime_settings_path,
    save_workspace_allowance_usd,
    save_workspace_cache_mode,
)


def test_local_preferences_change_persists_and_updates_new_routers(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("FRISKET_CACHE_MODE", "replay")
    root = tmp_path / "workspace"
    app = create_app(root)
    client = TestClient(app)

    response = client.patch(
        "/api/config",
        json={"cache_mode": "replay_strict", "confirmed": False},
    )

    assert response.status_code == 200
    assert response.json()["cache_mode"] == "replay_strict"
    assert response.json()["live_calls_possible"] is False
    assert response.json()["cache_mode_editable"] is True
    assert json.loads(runtime_settings_path(root).read_text()) == {
        "cache_mode": "replay_strict"
    }
    project = Project.create(root / "sample.frisket", name="sample")
    try:
        assert app.state.workspace.router_for(project).cache_mode == "replay_strict"
    finally:
        project.close()

    # The explicit UI choice is the persisted authority after restart, rather
    # than the environment silently undoing what Preferences reported saved.
    monkeypatch.setenv("FRISKET_CACHE_MODE", "off")
    restarted = TestClient(create_app(root))
    assert restarted.get("/api/config").json()["cache_mode"] == "replay_strict"


def test_local_allowance_persists_without_auth(tmp_path, monkeypatch) -> None:
    root = tmp_path / "workspace"
    monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "1.50")
    client = TestClient(create_app(root))

    initial = client.get("/api/config").json()
    assert initial["allowance_usd"] == "1.5"
    assert initial["allowance_editable"] is True

    response = client.patch("/api/config", json={"allowance_usd": "3.25"})
    assert response.status_code == 200, response.text
    assert response.json()["allowance_usd"] == "3.25"
    assert json.loads(runtime_settings_path(root).read_text()) == {
        "allowance_usd": "3.25"
    }
    assert (
        TestClient(create_app(root)).get("/api/config").json()["allowance_usd"]
        == "3.25"
    )

    for amount in ("NaN", "1000000000000", "0.0000001"):
        invalid = client.patch("/api/config", json={"allowance_usd": amount})
        assert invalid.status_code == 422


def test_local_allowance_settings_migrates_legacy_key_safely(tmp_path) -> None:
    root = tmp_path / "workspace"
    settings_file = runtime_settings_path(root)
    settings_file.parent.mkdir(parents=True, exist_ok=True)

    # Reading a pre-cutover workspace with only the legacy key preserves the value.
    settings_file.write_text(
        json.dumps({"cost_preapproval_usd": "4.50"}) + "\n", encoding="utf-8"
    )
    assert resolve_local_allowance_usd(root) == "4.5"
    assert resolve_workspace_allowance_usd(root) == "4.5"
    assert TestClient(create_app(root)).get("/api/config").json()["allowance_usd"] == (
        "4.5"
    )

    # Saving another runtime setting migrates the file to the canonical key.
    save_workspace_cache_mode(root, "replay_strict")
    assert json.loads(settings_file.read_text(encoding="utf-8")) == {
        "cache_mode": "replay_strict",
        "allowance_usd": "4.5",
    }

    # Reading the canonical key returns its normalized value.
    assert resolve_local_allowance_usd(root) == "4.5"
    assert resolve_workspace_allowance_usd(root) == "4.5"

    # Both keys with equivalent normalized values are accepted and canonicalized on write.
    settings_file.write_text(
        json.dumps({"allowance_usd": "2.00", "cost_preapproval_usd": "2"}) + "\n",
        encoding="utf-8",
    )
    assert resolve_local_allowance_usd(root) == "2"
    assert resolve_workspace_allowance_usd(root) == "2"
    assert save_workspace_allowance_usd(root, "7.00") == "7"
    assert json.loads(settings_file.read_text(encoding="utf-8")) == {
        "allowance_usd": "7"
    }

    # Conflicting values fail loudly rather than silently choosing one.
    settings_file.write_text(
        json.dumps({"allowance_usd": "2", "cost_preapproval_usd": "5"}) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(InvalidLocalRuntimeConfigError, match="conflicting"):
        resolve_local_allowance_usd(root)
    with pytest.raises(InvalidRuntimeSettingsError, match="conflicting"):
        resolve_workspace_allowance_usd(root)


def test_enabling_live_calls_requires_confirmation(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FRISKET_CACHE_MODE", "replay_strict")
    client = TestClient(create_app(tmp_path / "workspace"))

    refused = client.patch(
        "/api/config",
        json={"cache_mode": "fresh", "confirmed": False},
    )
    assert refused.status_code == 409
    assert "confirm" in refused.json()["detail"].lower()
    assert client.get("/api/config").json()["cache_mode"] == "replay_strict"

    accepted = client.patch(
        "/api/config",
        json={"cache_mode": "fresh", "confirmed": True},
    )
    assert accepted.status_code == 200
    assert accepted.json()["cache_mode"] == "fresh"
    assert accepted.json()["live_calls_possible"] is True

    # Moving between live-capable postures still changes future spend
    # behavior, so selecting the new posture requires acknowledgement.
    replay = client.patch(
        "/api/config",
        json={"cache_mode": "replay", "confirmed": True},
    )
    assert replay.status_code == 200
    replay_to_fresh = client.patch(
        "/api/config",
        json={"cache_mode": "fresh", "confirmed": False},
    )
    assert replay_to_fresh.status_code == 409


def test_deployment_owned_composition_has_no_runtime_mutation(tmp_path) -> None:
    client = TestClient(
        create_app(tmp_path / "workspace", enable_provider_config=False)
    )

    assert client.get("/api/config").json()["cache_mode_editable"] is False
    assert (
        client.patch(
            "/api/config",
            json={"cache_mode": "off", "confirmed": True},
        ).status_code
        == 405
    )

    injected = TestClient(
        create_app(
            tmp_path / "injected-workspace",
            router=ModelRouter(
                model_call_policy=LOCAL_PURCHASE_POLICY, cache_mode="off"
            ),
        )
    )
    assert injected.get("/api/config").json()["cache_mode_editable"] is False
    assert (
        injected.patch(
            "/api/config",
            json={"cache_mode": "replay", "confirmed": True},
        ).status_code
        == 405
    )


def test_runtime_update_rejects_unknown_modes(client) -> None:
    response = client.patch(
        "/api/config",
        json={"cache_mode": "surprise", "confirmed": True},
    )
    assert response.status_code == 422
