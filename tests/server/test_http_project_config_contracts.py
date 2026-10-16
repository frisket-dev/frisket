"""Public project-configuration route contracts and redaction invariants."""

from __future__ import annotations

from contextlib import contextmanager
import json

from fastapi.testclient import TestClient
from pydantic import ValidationError
import pytest

from frisket.contracts.http.project_config import ProjectProviderKeyValidationResponse
from frisket.engine.store import Project
from frisket.retrieval.media import AudioWindowRecipe
from frisket.retrieval.preparation_policy import (
    DEFAULT_PREPARATION_POLICY,
    PREPARATION_POLICY_META_KEY,
    PreparationPolicy,
    preparation_policy_json,
    write_preparation_policy,
)
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


def test_project_settings_round_trip_typed_retrieval_policy(tmp_path) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = client.post("/api/projects", json={"name": "Retrieval settings"}).json()["id"]

    initial = client.get(f"/api/projects/{pid}/settings")
    assert initial.status_code == 200
    assert initial.json()["retrieval"] == {
        "text_enabled": True,
        "transcript_enabled": True,
        "image_enabled": True,
        "pdf_pages_enabled": True,
        "video": {
            "strategy": "shot_clips",
            "interval_seconds": None,
            "duration_seconds": 10.0,
            "stride_seconds": None,
            "sample_fps": 1.0,
        },
        "audio_enabled": False,
    }

    updated = client.patch(
        f"/api/projects/{pid}/settings",
        json={
            "retrieval": {
                "text_enabled": False,
                "audio_enabled": True,
                "video": {
                    "strategy": "fixed_clips",
                    "duration_seconds": 6,
                    "stride_seconds": 3,
                    "sample_fps": 2,
                },
            }
        },
    )
    assert updated.status_code == 200, updated.text
    retrieval = updated.json()["retrieval"]
    assert retrieval["text_enabled"] is False
    assert retrieval["transcript_enabled"] is True
    assert retrieval["audio_enabled"] is True
    assert retrieval["video"] == {
        "strategy": "fixed_clips",
        "interval_seconds": None,
        "duration_seconds": 6.0,
        "stride_seconds": 3.0,
        "sample_fps": 2.0,
    }

    project = client.app.state.workspace.get(pid)
    assert project.get_meta("retrieval_preparation_policy_v1") is not None
    assert "retrieval" not in json.loads(project.get_meta("project_settings_v1"))


def test_project_settings_reject_invalid_video_knobs(tmp_path) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = client.post("/api/projects", json={"name": "Retrieval settings"}).json()["id"]

    wrong_strategy_field = client.patch(
        f"/api/projects/{pid}/settings",
        json={"retrieval": {"video": {"strategy": "frames", "duration_seconds": 5}}},
    )
    assert wrong_strategy_field.status_code == 422

    unknown_retrieval_field = client.patch(
        f"/api/projects/{pid}/settings",
        json={"retrieval": {"future_paid_provider": True}},
    )
    assert unknown_retrieval_field.status_code == 422

    impossible_clip = client.patch(
        f"/api/projects/{pid}/settings",
        json={
            "retrieval": {
                "video": {
                    "strategy": "fixed_clips",
                    "duration_seconds": 4,
                    "stride_seconds": 5,
                }
            }
        },
    )
    assert impossible_clip.status_code == 400


def test_concurrent_partial_retrieval_patches_merge_after_writer_lock(
    tmp_path, monkeypatch
) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = client.post("/api/projects", json={"name": "Concurrent settings"}).json()[
        "id"
    ]
    project = client.app.state.workspace.get(pid)
    concurrent = Project(project.path)
    original_transaction_scope = Project.transaction_scope
    injected = False

    @contextmanager
    def transaction_after_concurrent_patch(self):
        nonlocal injected
        if self is project and not injected:
            injected = True
            write_preparation_policy(
                concurrent,
                PreparationPolicy(audio_recipe=AudioWindowRecipe()),
            )
        with original_transaction_scope(self):
            yield

    try:
        monkeypatch.setattr(
            Project, "transaction_scope", transaction_after_concurrent_patch
        )

        response = client.patch(
            f"/api/projects/{pid}/settings",
            json={"retrieval": {"text_enabled": False}},
        )

        assert response.status_code == 200, response.text
        assert injected
        assert response.json()["retrieval"]["text_enabled"] is False
        assert response.json()["retrieval"]["audio_enabled"] is True
    finally:
        concurrent.close()


@pytest.mark.parametrize(
    ("stored_video", "reported_strategy"),
    [
        (
            {
                "strategy": "future_strategy_v2",
                "future_key": "preserve-me",
            },
            "off",
        ),
        (
            {
                **json.loads(preparation_policy_json(DEFAULT_PREPARATION_POLICY))[
                    "video_recipe"
                ],
                "future_key": "preserve-me",
            },
            "shot_clips",
        ),
    ],
)
def test_project_settings_preserve_untouched_future_policy_fields(
    tmp_path, stored_video: dict, reported_strategy: str
) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = client.post("/api/projects", json={"name": "Future policy"}).json()["id"]
    project = client.app.state.workspace.get(pid)
    stored = json.loads(preparation_policy_json(DEFAULT_PREPARATION_POLICY))
    stored["future_top_level"] = {"remote_provider": "paid"}
    stored["video_recipe"] = stored_video
    project.set_meta(PREPARATION_POLICY_META_KEY, json.dumps(stored))

    readable = client.get(f"/api/projects/{pid}/settings")
    assert readable.status_code == 200
    assert readable.json()["retrieval"]["video"]["strategy"] == reported_strategy

    unrelated = client.patch(
        f"/api/projects/{pid}/settings",
        json={"retrieval": {"text_enabled": False}},
    )
    assert unrelated.status_code == 200, unrelated.text
    persisted = json.loads(project.get_meta(PREPARATION_POLICY_META_KEY))
    assert persisted["text_recipe"] is None
    assert persisted["video_recipe"] == stored_video
    assert persisted["future_top_level"] == {"remote_provider": "paid"}

    converged = client.patch(
        f"/api/projects/{pid}/settings",
        json={"retrieval": {"video": {"strategy": "off"}}},
    )
    assert converged.status_code == 200, converged.text
    persisted = json.loads(project.get_meta(PREPARATION_POLICY_META_KEY))
    assert persisted["video_recipe"] is None
    assert persisted["future_top_level"] == {"remote_provider": "paid"}
    assert persisted["text_recipe"] is None


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
