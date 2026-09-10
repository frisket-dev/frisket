"""HTTP/OpenAPI coverage for the three F1a pre-auth instance/runtime ops.

Live-app assertions pin the transport (key order via ``response.json()``,
which preserves wire order); stub-level wire-byte assertions against the
exact pre-contract JSONResponse rendering pin serialization including the
edition-additive omission behavior. The local core app must keep serving 404
for ``/api/instance``; hosted-only ``tier``/``posture`` must validate when
present and never be required.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from frisket.contracts.http.instance_runtime import (
    HealthResponse,
    InstanceInfoResponse,
    RuntimeConfigResponse,
)
from frisket.server.app import create_app
from tests.team_setup_helpers import claim_server


CONFIG_KEYS = [
    "cache_mode",
    "live_calls_possible",
    "cache_mode_editable",
    "cost_preapproval_usd",
    "cost_preapproval_editable",
    "in_container",
    "recipe_fence_posture",
    "email_from_address",
    "email_from_name",
    "auth_methods",
    "plugins_available",
    "plugin_management_available",
    "product_telemetry_available",
]

FULL_HEALTH_PAYLOAD: dict[str, Any] = {
    "ok": True,
    "queue": {
        "schema_version": "frisket.queue_health.v1",
        "model_pull_enabled": False,
        "model_pull_workers": 0,
        "workers": {
            "live": 1,
            "count": 2,
            "last_heartbeat_age_seconds": 4.2,
            "liveness_window_seconds": 90.0,
        },
        "jobs": {"queued": 3, "running": 1, "succeeded": 40, "quarantined": 0},
        "handler_authorities": {
            "unheard": 0,
            "oldest_unheard_claim_age_seconds": None,
        },
        "queued": {
            "count": 3,
            "oldest_age_seconds": 12.5,
            "no_live_worker": False,
            "stale": False,
            "timed_out": False,
            "timeout_seconds": None,
        },
    },
}


def _wire_bytes(payload: dict[str, Any]) -> bytes:
    """The exact bytes the pre-contract bare-dict routes put on the wire."""

    return json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        indent=None,
        separators=(",", ":"),
    ).encode("utf-8")


def _serialized(model: Any) -> bytes:
    return _wire_bytes(model.model_dump(exclude_unset=True))


def _local_client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(tmp_path / "workspace"))


def _team_app(tmp_path: Path) -> Any:
    from frisket.team.app import TeamConfig, create_team_app

    config = TeamConfig(
        database_url=f"sqlite:///{tmp_path / 'control.db'}",
        run_queue_database_url=f"sqlite:///{tmp_path / 'run-queue.db'}",
        data_dir=tmp_path / "data",
        base_url="http://testserver",
        organization_name="Investigations Desk",
        admin_emails={"owner@example.com"},
    )
    app = create_team_app(config)
    claim_server(app, workspace_name="Investigations Desk")
    return app


def test_local_runtime_config_preserves_transport_shape(tmp_path) -> None:
    client = _local_client(tmp_path)
    response = client.get("/api/config")
    assert response.status_code == 200, response.text
    body = response.json()
    assert list(body.keys()) == CONFIG_KEYS
    assert isinstance(body["cache_mode"], str)
    assert isinstance(body["live_calls_possible"], bool)
    assert isinstance(body["cost_preapproval_usd"], str)
    assert body["cost_preapproval_editable"] is True
    assert isinstance(body["in_container"], bool)
    assert body["recipe_fence_posture"] in {"enforced", "partial", "none", "unknown"}
    assert body["email_from_address"] is None
    assert body["email_from_name"] is None
    assert body["auth_methods"] == {
        "password": False,
        "magic_link": False,
        "oidc": [],
    }
    assert body["plugins_available"] is True
    assert body["plugin_management_available"] is True
    assert body["product_telemetry_available"] is False
    # The typed path must put the same bytes on the wire the bare dict did.
    assert response.content == _serialized(RuntimeConfigResponse.model_validate(body))


def test_local_health_preserves_transport_shape_and_queue_subtree(tmp_path) -> None:
    client = _local_client(tmp_path)
    response = client.get("/api/health")
    assert response.status_code == 200, response.text
    body = response.json()
    assert list(body.keys()) == ["ok", "queue"]
    assert body["ok"] is True
    queue = body["queue"]
    assert queue["schema_version"] == "frisket.queue_health.v1"
    assert all(isinstance(count, int) for count in queue["jobs"].values())
    assert response.content == _serialized(HealthResponse.model_validate(body))


def test_health_wire_bytes_and_edition_additivity() -> None:
    # Full open-producer payload round-trips byte-for-byte, dynamic job
    # status keys included.
    assert _serialized(
        HealthResponse.model_validate(FULL_HEALTH_PAYLOAD)
    ) == _wire_bytes(FULL_HEALTH_PAYLOAD)

    # Queue-less producer output omits the key entirely (never null).
    assert _serialized(HealthResponse.model_validate({"ok": True})) == _wire_bytes(
        {"ok": True}
    )

    # Hosted-only additive fields validate when present and round-trip.
    hosted = {
        "ok": True,
        "tier": "hosted",
        "posture": {
            "default_secrets_master_key": True,
            "open_signup": False,
            "admin_configured": True,
        },
    }
    assert _serialized(HealthResponse.model_validate(hosted)) == _wire_bytes(hosted)

    # Anything OUTSIDE the declared additive fields still fails loudly.
    with pytest.raises(ValidationError):
        HealthResponse.model_validate({"ok": True, "surprise": True})


def test_team_instance_bytes_and_local_404(tmp_path) -> None:
    team_client = TestClient(_team_app(tmp_path))
    response = team_client.get("/api/instance")
    assert response.status_code == 200, response.text
    expected = {"display_name": "Investigations Desk", "support_contact": None}
    assert response.content == _wire_bytes(expected)
    assert response.content == _serialized(
        InstanceInfoResponse.model_validate(expected)
    )

    local_client = _local_client(tmp_path)
    assert local_client.get("/api/instance").status_code == 404


def test_openapi_declares_typed_operations(tmp_path) -> None:
    local = _local_client(tmp_path)
    document = local.app.openapi()
    config = document["paths"]["/api/config"]["get"]
    health = document["paths"]["/api/health"]["get"]
    for operation, ref in (
        (config, "RuntimeConfigResponse"),
        (health, "HealthResponse"),
    ):
        schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
        assert schema == {"$ref": f"#/components/schemas/{ref}"}
        assert set(operation["responses"]) >= {"200", "500"}

    team = TestClient(_team_app(tmp_path))
    team_document = team.app.openapi()
    instance = team_document["paths"]["/api/instance"]["get"]
    instance_schema = instance["responses"]["200"]["content"]["application/json"][
        "schema"
    ]
    assert instance_schema == {"$ref": "#/components/schemas/InstanceInfoResponse"}
