"""Shared helpers for tests which need a ready server fixture."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi.testclient import TestClient

from frisket.team.identity_service import IdentityAuthService
from frisket.team.schema import pending_invites
from frisket.team.uow import TeamUnitOfWork


def claim_server(
    app: Any,
    *,
    client: TestClient | None = None,
    email: str = "owner@example.com",
    password: str = "a sufficiently long password",
    origin: str = "http://testserver",
    workspace_name: str = "Test Desk",
) -> TestClient:
    """Claim a fresh test server through its public setup endpoint."""
    browser = client or TestClient(app)
    if app.state.setup_claim_token is None:
        return browser
    response = browser.post(
        "/setup",
        data={
            "claim_token": app.state.setup_claim_token,
            "workspace_name": workspace_name,
            "owner_name": "Owner",
            "email": email,
            "password": password,
            "password_confirmation": password,
        },
        headers={"Origin": origin},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    return browser


def seed_member_invite(app: Any, email: str) -> None:
    """Make a magic-link login for ``email`` join the claimed test server."""
    with app.state.control_engine.begin() as connection:
        connection.execute(
            pending_invites.delete().where(pending_invites.c.email == email.lower())
        )
        connection.execute(
            pending_invites.insert().values(
                email=email.lower(),
                org_id=app.state.team_org_id,
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )


def sign_in_with_magic_link(
    app: Any,
    client: TestClient,
    email: str,
    *,
    password: str = "a sufficiently long password",
) -> None:
    """Complete either the returning-user or first-use magic-link path."""

    auth = IdentityAuthService(
        lambda: TeamUnitOfWork(app.state.control_engine, org_id=app.state.team_org_id)
    )
    token = auth.create_magic_link(email)
    callback = client.get(f"/auth/callback?token={token}", follow_redirects=False)
    assert callback.status_code == 302, callback.text
    if client.cookies.get("frisket_session") is None:
        completed = client.post(
            f"/auth/callback?token={token}", json={"password": password}
        )
        assert completed.status_code == 200, completed.text
    assert client.cookies.get("frisket_session")
