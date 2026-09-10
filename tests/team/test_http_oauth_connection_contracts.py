"""Public HTTP contract for Team browser OAuth-connection inventory."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.routing import APIRoute
from pydantic import ValidationError

from frisket.contracts.http.models import HttpError, WireModel
from frisket.contracts.http.oauth_connections import (
    OAuthConnection,
    OAuthConnectionList,
)
from frisket.team.app import TeamConfig, create_team_app
from frisket.team.oauth_connections import TeamOAuthConnectionService
from frisket.team.secret_box import TeamSecretBox
from tests.team_setup_helpers import claim_server


async def _mail(_email: str, _link: str) -> bool:
    return True


def _config(tmp_path: Path) -> TeamConfig:
    return TeamConfig(
        database_url=f"sqlite:///{tmp_path / 'control.db'}",
        run_queue_database_url=f"sqlite:///{tmp_path / 'queue.db'}",
        data_dir=tmp_path / "data",
        base_url="http://testserver",
        organization_name="OAuth contract desk",
        admin_emails={"owner@example.com"},
    )


def _app(tmp_path: Path) -> tuple[Any, TeamConfig]:
    config = _config(tmp_path)
    return (
        create_team_app(
            config, send_magic_email=_mail, product_telemetry_destination=None
        ),
        config,
    )


def test_oauth_connection_list_route_has_a_closed_public_wire_contract(
    tmp_path: Path,
) -> None:
    app, _config = _app(tmp_path)
    routes = {
        route.name: route
        for route in app.routes
        if isinstance(route, APIRoute) and route.name == "list_oauth_connections"
    }
    assert set(routes) == {"list_oauth_connections"}
    route = routes["list_oauth_connections"]
    assert route.path == "/api/org/oauth/connections"
    assert route.methods == {"GET"}
    assert route.response_model is OAuthConnectionList
    assert route.response_model_exclude_unset is True
    assert set(route.responses) == {401, 403, 500}
    assert all(value == {"model": HttpError} for value in route.responses.values())
    assert [parameter.name for parameter in route.dependant.query_params] == [
        "provider"
    ]
    assert route.dependant.body_params == []

    document = app.openapi()
    operation = document["paths"]["/api/org/oauth/connections"]["get"]
    assert "requestBody" not in operation
    assert operation["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/OAuthConnectionList"
    }
    assert operation["parameters"][0]["name"] == "provider"
    assert operation["parameters"][0]["in"] == "query"
    assert operation["parameters"][0]["required"] is False
    assert operation["parameters"][0]["schema"]["anyOf"] == [
        {"type": "string"},
        {"type": "null"},
    ]
    schemas = document["components"]["schemas"]
    assert schemas["OAuthConnection"]["additionalProperties"] is False
    assert schemas["OAuthConnectionList"]["additionalProperties"] is False
    assert {"refresh_token", "ciphertext", "encrypted_refresh_token"}.isdisjoint(
        schemas["OAuthConnection"]["properties"]
    )
    for field in ("token_type", "refresh_token_hint"):
        assert field not in schemas["OAuthConnection"]["required"]
        assert schemas["OAuthConnection"]["properties"][field]["anyOf"] == [
            {"type": "string"},
            {"type": "null"},
        ]


def test_oauth_connection_public_omissions_and_private_compatible_fields_are_exact() -> (
    None
):
    legacy = {
        "id": "google_0123",
        "connection_id": "google_0123",
        "provider": "google",
        "external_subject": "subject-1",
        "external_email": "person@example.com",
        "scopes": ["openid"],
        "created_at": "2026-08-12T00:00:00+00:00",
        "updated_at": "2026-08-12T00:00:00+00:00",
        "revoked_at": None,
    }
    assert OAuthConnectionList.model_validate({"connections": [legacy]}).model_dump(
        exclude_unset=True
    ) == {"connections": [legacy]}

    compatible = {
        **legacy,
        "token_type": "Bearer",
        "refresh_token_hint": "…abcd",
    }
    compatible_dump = OAuthConnection.model_validate(compatible).model_dump(
        exclude_unset=True
    )
    assert compatible_dump == compatible
    assert list(compatible_dump) == [
        "id",
        "connection_id",
        "provider",
        "external_subject",
        "external_email",
        "scopes",
        "token_type",
        "refresh_token_hint",
        "created_at",
        "updated_at",
        "revoked_at",
    ]

    for forbidden in (
        "refresh_token",
        "ciphertext",
        "encrypted_refresh_token",
        "unknown",
    ):
        with pytest.raises(ValidationError):
            OAuthConnection.model_validate({**legacy, forbidden: "secret"})
    assert issubclass(OAuthConnection, WireModel)
    assert issubclass(OAuthConnectionList, WireModel)


def test_oauth_connection_list_keeps_org_member_bytes_and_browser_only_auth(
    tmp_path: Path,
) -> None:
    app, config = _app(tmp_path)
    owner = claim_server(app, workspace_name="OAuth contract desk")
    service = TeamOAuthConnectionService(
        app.state.control_engine,
        secret_box=TeamSecretBox(config),
    )
    service.connect_google_account(
        org_id=app.state.team_org_id,
        user_id=1,
        external_subject="subject-1",
        refresh_token="refresh-secret-never-public",
        external_email="person@example.com",
        scopes=["openid"],
    )
    expected = owner.get("/api/org/oauth/connections")
    assert expected.status_code == 200, expected.text
    payload = expected.json()
    connection = payload["connections"][0]
    producer_order = [
        "id",
        "connection_id",
        "provider",
        "external_subject",
        "external_email",
        "scopes",
        "created_at",
        "updated_at",
        "revoked_at",
    ]
    assert list(connection) == producer_order
    assert expected.content == json.dumps(
        {"connections": [{key: connection[key] for key in producer_order}]},
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    assert connection["provider"] == "google"
    assert "token_type" not in connection
    assert "refresh_token_hint" not in connection
    assert "refresh-secret-never-public" not in expected.text
    # Empty is a valid optional query value and retains the legacy all-provider
    # behavior; special characters are accepted as ordinary query bytes.
    assert owner.get("/api/org/oauth/connections?provider=").json() == expected.json()
    assert owner.get("/api/org/oauth/connections?provider=google%2Ftest").json() == {
        "connections": []
    }

    pat = owner.post("/api/org/tokens", json={"name": "oauth probe"}).json()["token"]
    owner.cookies.clear()
    denied = owner.get(
        "/api/org/oauth/connections",
        headers={"Authorization": f"Bearer {pat}"},
    )
    assert denied.status_code == 403
    assert denied.json() == {"detail": "browser session required"}
