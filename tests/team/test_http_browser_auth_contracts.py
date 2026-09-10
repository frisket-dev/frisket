"""Browser-auth routes retain their typed JSON and cookie wire contract."""

from __future__ import annotations

from pathlib import Path
from typing import get_type_hints

from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from frisket.contracts.http.browser_auth import (
    BrowserAuthLogoutResponse,
    BrowserAuthPasswordLoginRequest,
    BrowserAuthPasswordLoginResponse,
    BrowserAuthRequestLinkRequest,
    BrowserAuthRequestLinkResponse,
)
from frisket.team.app import TeamConfig, create_team_app


def _app(tmp_path: Path, *, magic_link_enabled: bool):
    async def mail(_email: str, _link: str) -> bool:
        return True

    return create_team_app(
        TeamConfig(
            database_url=f"sqlite:///{tmp_path / 'control.db'}",
            run_queue_database_url=f"sqlite:///{tmp_path / 'queue.db'}",
            data_dir=tmp_path / "data",
            base_url="http://testserver",
            organization_name="Browser auth contracts",
            magic_link_enabled=magic_link_enabled,
        ),
        send_magic_email=mail,
        product_telemetry_destination=None,
    )


def _route(app, path: str) -> APIRoute:
    return next(
        route
        for route in app.routes
        if isinstance(route, APIRoute) and route.path == path
    )


def _claim(client: TestClient, app) -> None:
    response = client.post(
        "/setup",
        data={
            "claim_token": app.state.setup_claim_token,
            "workspace_name": "Browser auth contracts",
            "owner_name": "Owner",
            "email": "owner@example.test",
            "password": "a sufficiently long password",
            "password_confirmation": "a sufficiently long password",
        },
        headers={"Origin": "http://testserver"},
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_browser_auth_routes_declare_typed_models_and_http_errors(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path, magic_link_enabled=True)
    request_link = _route(app, "/auth/request-link")
    password_login = _route(app, "/auth/password-login")
    logout = _route(app, "/auth/logout")

    assert request_link.response_model is BrowserAuthRequestLinkResponse
    assert request_link.body_field is not None
    assert (
        get_type_hints(request_link.endpoint)["body"] is BrowserAuthRequestLinkRequest
    )
    assert set(request_link.responses) == {400, 404, 422, 500, 502}
    assert password_login.response_model is BrowserAuthPasswordLoginResponse
    assert password_login.body_field is not None
    assert (
        get_type_hints(password_login.endpoint)["body"]
        is BrowserAuthPasswordLoginRequest
    )
    assert set(password_login.responses) == {401, 403, 422, 429, 500}
    assert logout.response_model is BrowserAuthLogoutResponse
    assert set(logout.responses) == {500}

    client = TestClient(app)
    _claim(client, app)
    client.cookies.clear()
    login = client.post(
        "/auth/password-login",
        json={
            "email": "owner@example.test",
            "password": "a sufficiently long password",
        },
        headers={"Origin": "http://testserver"},
    )
    assert login.status_code == 200
    assert login.content == b'{"ok":true}'
    sent = client.post("/auth/request-link", json={"email": " person@example.test "})
    assert sent.status_code == 200
    assert sent.content == b'{"sent":true}'

    invalid = client.post("/auth/request-link", json={})
    assert invalid.status_code == 400
    assert invalid.content == b'{"detail":"valid email required"}'

    logged_out = client.post("/auth/logout")
    assert logged_out.status_code == 200
    assert logged_out.content == b'{"ok":true}'
    assert logged_out.headers["cache-control"] == "no-store"
    assert 'frisket_session=""' in logged_out.headers["set-cookie"]
    assert "Max-Age=0" in logged_out.headers["set-cookie"]


def test_request_link_disabled_error_remains_the_http_error_envelope(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path, magic_link_enabled=False)
    client = TestClient(app)
    _claim(client, app)
    response = client.post("/auth/request-link", json={"email": "person@example.test"})

    assert response.status_code == 404
    assert response.content == b'{"detail":"magic-link auth is disabled"}'
