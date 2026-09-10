"""Frozen corrective Stage-2 contract for open-team route parity.

This is deliberately an edition boundary test, not a source-layout ledger.  It
uses the real team ASGI application and its mounted core app, derives the full
open/private identity sets from the allowlist plus endpoint catalogs, and then
exercises the formerly omitted open-team workflows.  A future implementation
must make these assertions true without importing any private edition module.
"""

from __future__ import annotations

import importlib
import inspect
import json
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from itertools import count, islice
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
import sqlalchemy as sa
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.routing import BaseRoute, Mount

from frisket.team.schema import (
    DEFAULT_DIAGNOSTIC_REPORT_RETENTION_DAYS,
    DEFAULT_DIAGNOSTIC_REPORT_RETENTION_MAX_ROWS,
    client_errors,
    memberships,
    orgs,
    oauth_connections,
    pending_invites,
    project_invites,
    project_roles,
    users,
)
from tests.team_setup_helpers import (
    claim_server,
    seed_member_invite,
    sign_in_with_magic_link,
)


pytestmark = pytest.mark.gap

ROOT = Path(__file__).resolve().parents[2]
COMMERCE_WORDS = (
    "credit",
    "funding",
    "stripe",
    "balance",
    "reservation",
    "ledger",
    "allowance",
    "billing",
    "quota",
)

OPEN_AUTH_ROUTES = {
    ("GET", "/auth/project-invites/{token}/accept"),
    ("POST", "/auth/project-invites/{token}/accept"),
}


def _fail(message: str) -> None:
    pytest.fail(message, pytrace=False)


def _team_module() -> ModuleType:
    spec = importlib.util.find_spec("frisket.team.app")
    if spec is None:
        _fail("open team app is absent: frisket.team.app cannot be found")
    try:
        return importlib.import_module("frisket.team.app")
    except (
        Exception
    ) as exc:  # guarded so admission reports an assertion, not collection
        _fail(f"open team app cannot be imported: {type(exc).__name__}: {exc}")
    raise AssertionError("unreachable")


def _team_api() -> tuple[type[Any], Callable[..., Any]]:
    module = _team_module()
    config = getattr(module, "TeamConfig", None)
    factory = getattr(module, "create_team_app", None)
    assert config is not None and callable(factory), (
        "team composition must export TeamConfig and create_team_app"
    )
    return config, factory


def _config(tmp_path: Path) -> Any:
    TeamConfig, _ = _team_api()
    return TeamConfig(
        database_url=f"sqlite:///{tmp_path / 'control.db'}",
        # create_team_app now requires a run-queue locator unconditionally
        # (deferred follow-up from the queue-composition audit, "general run-queue
        # mismatch", completed).
        run_queue_database_url=f"sqlite:///{tmp_path / 'run-queue.db'}",
        data_dir=tmp_path / "data",
        base_url="http://testserver",
        organization_name="Investigations Desk",
        admin_emails={"owner@example.com"},
        oidc_providers={
            "newsroom": {
                "issuer": "https://id.example.test",
                "client_id": "team-client",
                "client_secret": "oidc-secret",
                "authorization_endpoint": "https://id.example.test/authorize",
                "token_endpoint": "https://id.example.test/token",
                "jwks_uri": "https://id.example.test/jwks",
                "userinfo_endpoint": "https://id.example.test/userinfo",
                "scopes": ["openid", "email", "profile"],
            }
        },
    )


def _app(tmp_path: Path, *, deliveries: list[tuple[str, str]] | None = None) -> Any:
    _, create_team_app = _team_api()

    async def send_magic_email(email: str, link: str) -> bool:
        if deliveries is not None:
            deliveries.append((email, link))
        return True

    # The existing OIDC injection is unrelated to connected Google accounts,
    # but keeps ordinary startup wholly local and deterministic.
    async def oidc_exchange(
        _provider: str, _code: str, _redirect_uri: str, nonce: str = ""
    ) -> dict[str, Any]:
        return {
            "email": "oidc@example.com",
            "email_verified": True,
            "subject": "subject-1",
            "nonce": nonce,
        }

    try:
        app = create_team_app(
            _config(tmp_path),
            send_magic_email=send_magic_email,
            oidc_exchange=oidc_exchange,
        )
        claim_server(app, workspace_name="Investigations Desk")
        return app
    except Exception as exc:
        _fail(f"team app startup failed: {type(exc).__name__}: {exc}")
    raise AssertionError("unreachable")


def _sign_in(client: TestClient, app: Any, email: str) -> None:
    claim_server(
        app,
        client=client,
        workspace_name="Investigations Desk",
    )
    if email != "owner@example.com":
        seed_member_invite(app, email)
    sign_in_with_magic_link(app, client, email)


def _create_project(client: TestClient, name: str = "Parity project") -> str:
    response = client.post("/api/projects", json={"name": name})
    assert response.status_code == 200, response.text
    project_id = response.json().get("id")
    assert isinstance(project_id, str) and project_id
    return project_id


def _pat(client: TestClient) -> str:
    response = client.post("/api/org/tokens", json={"name": "parity"})
    assert response.status_code == 200, response.text
    token = response.json().get("token")
    assert isinstance(token, str) and token.startswith("frisket_pat_")
    return token


def _delivered_link(deliveries: list[tuple[str, str]], email: str) -> str:
    matches = [link for target, link in deliveries if target == email]
    assert matches, f"open mail transport did not receive a link for {email}"
    return matches[-1]


def _join(prefix: str, path: str) -> str:
    return (prefix.rstrip("/") + "/" + path.lstrip("/")).rstrip("/") or "/"


def _flatten(
    routes: Iterable[BaseRoute], *, prefix: str = "", owner: str = "outer"
) -> list[tuple[str, str, str, str]]:
    """Return effective route records in Starlette's first-match order.

    A mount is recursed at the point it appears.  Later outer/core routes with
    the same method and path cannot win, mirroring Starlette dispatch rather
    than treating a mounted app as an unordered route set.
    """
    flattened: list[tuple[str, str, str, str]] = []
    for route in routes:
        if isinstance(route, Mount):
            child_owner = "tenant" if owner == "outer" else owner
            flattened.extend(
                _flatten(
                    route.routes, prefix=_join(prefix, route.path), owner=child_owner
                )
            )
            continue
        if not isinstance(route, APIRoute):
            continue
        path = _join(prefix, route.path)
        for method in sorted(route.methods or ()):
            # HEAD/OPTIONS are framework behavior; catalog identities are explicit.
            if method in {"HEAD", "OPTIONS"}:
                continue
            flattened.append((owner, route.name, method, path))
    return flattened


def _effective_routes(app: Any) -> dict[tuple[str, str], tuple[str, str, str, str]]:
    effective: dict[tuple[str, str], tuple[str, str, str, str]] = {}
    for record in _flatten(app.routes):
        effective.setdefault((record[2], record[3]), record)
    return effective


def _commerce_free(value: Any) -> bool:
    if isinstance(value, dict):
        return all(
            not any(word in str(key).lower() for word in COMMERCE_WORDS)
            and _commerce_free(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return all(_commerce_free(item) for item in value)
    return True


def test_session_instance_profile_and_logout_are_open_identity_not_commerce(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path)
    client = TestClient(app)
    instance = client.get("/api/instance")
    assert instance.status_code == 200, instance.text
    assert instance.json()["display_name"] == "Investigations Desk"
    assert _commerce_free(instance.json())

    _sign_in(client, app, "owner@example.com")
    before = client.get("/api/me")
    assert before.status_code == 200, before.text
    changed = client.patch("/api/me/profile", json={"display_name": "Owner Name"})
    assert changed.status_code == 200, changed.text
    assert changed.json()["display_name"] == "Owner Name"
    assert _commerce_free(changed.json())

    logout = client.post("/auth/logout")
    assert logout.status_code == 200, logout.text
    assert logout.json() == {"ok": True}
    assert client.get("/api/me").status_code == 401


def test_admin_health_overview_and_invite_revocation_require_browser_admin_and_are_credit_free(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path)
    owner = TestClient(app)
    member = TestClient(app)
    _sign_in(owner, app, "owner@example.com")
    _sign_in(member, app, "member@example.com")
    pat = _pat(owner)

    for path in ("/api/admin/health", "/api/admin/overview"):
        assert TestClient(app).get(path).status_code == 401
        assert member.get(path).status_code == 403
        assert (
            owner.get(path, headers={"Authorization": f"Bearer {pat}"}).status_code
            == 403
        )
        response = owner.get(path)
        assert response.status_code == 200, response.text
        payload = response.json()
        assert _commerce_free(payload), f"{path} leaks commerce state: {payload}"
        if path.endswith("/health"):
            assert payload.get("ok") is True
            assert payload.get("db", {}).get("ok") is True
            assert payload.get("db", {}).get("dialect") == "sqlite"
            assert payload.get("blob_store", {}).get("ok") is True
            assert payload.get("active_runs") == 0
        else:
            totals = payload.get("totals", payload.get("summary"))
            assert isinstance(totals, dict), (
                "overview must expose named substantive totals"
            )
            assert totals.get("orgs") == 1
            assert totals.get("users") == 2
            assert totals.get("projects") == 0
            assert totals.get("pending_invites") == 0
    invited = owner.post(
        "/api/admin/users/invite", json={"email": "remove@example.com"}
    )
    assert invited.status_code == 200, invited.text
    with app.state.control_engine.connect() as cx:
        assert (
            cx.execute(
                sa.select(pending_invites.c.email).where(
                    pending_invites.c.email == "remove@example.com"
                )
            ).scalar_one_or_none()
            == "remove@example.com"
        )
    revoke = owner.delete("/api/admin/users/invites/remove@example.com")
    assert revoke.status_code == 200, revoke.text
    with app.state.control_engine.connect() as cx:
        org_count = cx.execute(
            sa.select(sa.func.count()).select_from(orgs)
        ).scalar_one()
        removed = cx.execute(
            sa.select(pending_invites.c.email).where(
                pending_invites.c.email == "remove@example.com"
            )
        ).scalar_one_or_none()
    assert org_count == 1, "admin route must not create a second organization"
    assert removed is None, "admin revoke must remove the pending invite row"


def test_admin_invite_delivers_usable_magic_link_into_the_single_org(
    tmp_path: Path,
) -> None:
    deliveries: list[tuple[str, str]] = []
    app = _app(tmp_path, deliveries=deliveries)
    owner = TestClient(app)
    _sign_in(owner, app, "owner@example.com")

    org_invite = owner.post(
        "/api/admin/users/invite", json={"email": "member@example.com"}
    )
    assert org_invite.status_code == 200, org_invite.text
    link = _delivered_link(deliveries, "member@example.com")
    parsed = urlparse(link)
    assert parsed.path == "/auth/callback"
    token = parse_qs(parsed.query).get("token", [""])[0]
    assert token, "admin invite mail link lacks a token"
    member = TestClient(app)
    callback = member.get(f"{parsed.path}?{parsed.query}", follow_redirects=False)
    assert callback.status_code == 302, callback.text
    assert member.cookies.get("frisket_session") is None
    redirect = urlparse(callback.headers["location"])
    assert parse_qs(redirect.query).get("auth_kind") == ["magic-link"]
    assert parse_qs(redirect.query).get("auth_token") == [token]
    completed = member.post(
        f"/auth/callback?token={token}",
        json={"password": "a sufficiently long password"},
    )
    assert completed.status_code == 200, completed.text
    assert completed.json() == {"ok": True}
    assert member.cookies.get("frisket_session")
    with app.state.control_engine.connect() as cx:
        org_count = cx.execute(
            sa.select(sa.func.count()).select_from(orgs)
        ).scalar_one()
        member_count = cx.execute(
            sa.select(sa.func.count())
            .select_from(memberships.join(users, memberships.c.user_id == users.c.id))
            .where(users.c.email == "member@example.com")
        ).scalar_one()
    assert org_count == 1
    assert member_count == 1


def test_project_invite_acceptance_is_open_single_use_and_project_scoped(
    tmp_path: Path,
) -> None:
    deliveries: list[tuple[str, str]] = []
    app = _app(tmp_path, deliveries=deliveries)
    owner = TestClient(app)
    _sign_in(owner, app, "owner@example.com")
    project_id = _create_project(owner)

    created = owner.post(
        f"/api/projects/{project_id}/invites",
        json={"email": "collab@example.com", "role": "viewer"},
    )
    assert created.status_code == 200, created.text
    body = created.json()
    invite = body.get("invite", body)
    assert "token" not in json.dumps(invite).lower(), "raw project invite token leaked"
    link = _delivered_link(deliveries, "collab@example.com")
    parsed = urlparse(link)
    assert parsed.path.startswith("/auth/project-invites/")
    assert parsed.path.endswith("/accept")
    token = parsed.path.removeprefix("/auth/project-invites/").removesuffix("/accept")
    assert token and token not in json.dumps(invite), (
        "raw invite token leaked outside mail"
    )
    listed = owner.get(f"/api/projects/{project_id}/invites")
    assert listed.status_code == 200, listed.text
    rows = listed.json().get("invites", listed.json())
    assert len(rows) == 1 and rows[0]["email"] == "collab@example.com"
    invitee = TestClient(app)
    first_accept = invitee.post(
        parsed.path, json={"password": "a sufficiently long password"}
    )
    assert first_accept.status_code == 200, first_accept.text
    assert first_accept.json() == {"ok": True}
    assert invitee.cookies.get("frisket_session"), (
        "accepted invite must establish a usable session"
    )
    assert token not in first_accept.text
    invited_projects = invitee.get("/api/projects")
    assert invited_projects.status_code == 200, invited_projects.text
    assert project_id in {project["id"] for project in invited_projects.json()}
    invited_project = invitee.get(f"/api/projects/{project_id}")
    assert invited_project.status_code == 200, invited_project.text
    forbidden_org_scope = {
        "create_project": invitee.post(
            "/api/projects", json={"name": "Project-only user must not create"}
        ).status_code,
        "org_keys": invitee.get("/api/org/keys").status_code,
        "org_env": invitee.get("/api/org/env").status_code,
        "oauth_connections": invitee.get("/api/org/oauth/connections").status_code,
        "create_pat": invitee.post(
            "/api/org/tokens", json={"name": "forbidden project-only token"}
        ).status_code,
    }
    assert forbidden_org_scope == {
        "create_project": 403,
        "org_keys": 403,
        "org_env": 403,
        "oauth_connections": 403,
        "create_pat": 403,
    }, f"project-only invite leaked organization capabilities: {forbidden_org_scope}"
    repeated = invitee.post(
        parsed.path, json={"password": "a sufficiently long password"}
    )
    assert repeated.status_code == 410, repeated.text
    with app.state.control_engine.connect() as cx:
        collab_id = cx.execute(
            sa.select(users.c.id).where(users.c.email == "collab@example.com")
        ).scalar_one()
        org_membership = cx.execute(
            sa.select(memberships.c.user_id).where(memberships.c.user_id == collab_id)
        ).scalar_one_or_none()
        project_role = cx.execute(
            sa.select(project_roles.c.role).where(
                project_roles.c.user_id == collab_id,
                project_roles.c.slug == project_id,
            )
        ).scalar_one_or_none()
        org_count = cx.execute(
            sa.select(sa.func.count()).select_from(orgs)
        ).scalar_one()
    assert org_membership is None, "project invite must not grant whole-org membership"
    assert project_role == "viewer"
    assert org_count == 1
    inspector = sa.inspect(app.state.control_engine)
    assert not any("funding" in table for table in inspector.get_table_names())

    second = owner.post(
        f"/api/projects/{project_id}/invites",
        json={"email": "revoked@example.com", "role": "viewer"},
    )
    assert second.status_code == 200, second.text
    revoked_link = _delivered_link(deliveries, "revoked@example.com")
    revoked_path = urlparse(revoked_link).path
    second_rows = owner.get(f"/api/projects/{project_id}/invites").json()["invites"]
    revoked_id = next(
        row["id"] for row in second_rows if row["email"] == "revoked@example.com"
    )
    revoked = owner.delete(f"/api/projects/{project_id}/invites/{revoked_id}")
    assert revoked.status_code == 200, revoked.text
    rejected = TestClient(app).get(revoked_path, follow_redirects=False)
    unknown = TestClient(app).get(
        "/auth/project-invites/not-a-real-token/accept", follow_redirects=False
    )
    assert rejected.status_code == unknown.status_code == 404
    assert rejected.json() == unknown.json(), "revoked token leaked its prior validity"


def test_project_invite_acceptance_rolls_back_when_session_insert_fails(
    tmp_path: Path,
) -> None:
    deliveries: list[tuple[str, str]] = []
    app = _app(tmp_path, deliveries=deliveries)
    owner = TestClient(app)
    _sign_in(owner, app, "owner@example.com")
    project_id = _create_project(owner, "Atomic invite")
    created = owner.post(
        f"/api/projects/{project_id}/invites",
        json={"email": "atomic@example.com", "role": "viewer"},
    )
    assert created.status_code == 200, created.text
    acceptance_path = urlparse(_delivered_link(deliveries, "atomic@example.com")).path

    injected = {"raised": False}

    def fail_first_session_insert(
        _connection: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        normalized = statement.lower().replace('"', "").replace("`", "")
        if not injected["raised"] and "insert into sessions" in normalized:
            injected["raised"] = True
            raise RuntimeError("injected session insert failure")

    sa.event.listen(
        app.state.control_engine, "before_cursor_execute", fail_first_session_insert
    )
    try:
        failed = TestClient(app, raise_server_exceptions=False).post(
            acceptance_path, json={"password": "a sufficiently long password"}
        )
    finally:
        sa.event.remove(
            app.state.control_engine,
            "before_cursor_execute",
            fail_first_session_insert,
        )
    assert injected["raised"], "acceptance did not attempt to persist a session"
    assert failed.status_code == 500, failed.text

    with app.state.control_engine.connect() as cx:
        accepted_at = cx.execute(
            sa.select(project_invites.c.accepted_at).where(
                project_invites.c.email == "atomic@example.com",
                project_invites.c.slug == project_id,
            )
        ).scalar_one()
        role_count = cx.execute(
            sa.select(sa.func.count())
            .select_from(
                project_roles.join(users, project_roles.c.user_id == users.c.id)
            )
            .where(
                users.c.email == "atomic@example.com",
                project_roles.c.slug == project_id,
            )
        ).scalar_one()
    assert accepted_at is None, "failed session insert consumed the invite"
    assert role_count == 0, "failed session insert left a project-role grant"

    retry = TestClient(app).post(
        acceptance_path, json={"password": "a sufficiently long password"}
    )
    assert retry.status_code == 200, retry.text
    assert retry.json() == {"ok": True}


def test_google_connection_uses_injected_exchange_and_persists_encrypted_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _TeamConfig, create_team_app = _team_api()
    signature = inspect.signature(create_team_app)
    assert "google_connection_exchange" in signature.parameters, (
        "create_team_app must accept google_connection_exchange= so the open "
        "Google connection callback has a deterministic no-network seam"
    )
    exchanges: list[tuple[str, str]] = []

    async def exchange(code: str, **_kwargs: Any) -> dict[str, Any]:
        exchanges.append(("google", code))
        return {
            "refresh_token": "refresh-secret-never-in-response",
            "external_subject": "google-subject-1",
            "external_email": "person@example.com",
            "scopes": [
                "openid",
                "email",
                "https://www.googleapis.com/auth/spreadsheets",
            ],
            "token_type": "Bearer",
        }

    # Rebuild only after the explicit assertion above: current implementation
    # reports one semantic missing seam instead of a TypeError fixture error.
    _, factory = _team_api()

    async def send_magic_email(_email: str, _link: str) -> bool:
        return True

    oauth_dir = tmp_path / "oauth"
    oauth_dir.mkdir()
    kwargs: dict[str, Any] = {
        "send_magic_email": send_magic_email,
        "google_connection_exchange": exchange,
    }
    try:
        app = factory(_config(oauth_dir), **kwargs)
    except Exception as exc:
        _fail(
            f"injected Google connection app startup failed: {type(exc).__name__}: {exc}"
        )

    # A direct transport call would make this test fail immediately; the route
    # must use the injected exchange rather than contact Google.
    import httpx

    class NoNetworkClient:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("Google connected-account route attempted network I/O")

    monkeypatch.setattr(httpx, "AsyncClient", NoNetworkClient)
    client = TestClient(app)
    _sign_in(client, app, "owner@example.com")
    start = client.get("/api/org/oauth/google/start", follow_redirects=False)
    assert start.status_code == 302, start.text
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    callback = client.get(
        f"/api/org/oauth/google/callback?code=local-code&state={state}",
        follow_redirects=False,
    )
    assert callback.status_code == 302, callback.text
    assert exchanges == [("google", "local-code")]
    listed = client.get("/api/org/oauth/connections")
    assert listed.status_code == 200, listed.text
    payload = listed.json()
    serialized = json.dumps(payload)
    assert "refresh-secret-never-in-response" not in serialized
    connection = payload["connections"][0]
    assert connection["provider"] == "google"
    assert connection["external_email"] == "person@example.com"
    assert "token" not in json.dumps(connection).lower()
    with app.state.control_engine.connect() as cx:
        stored = cx.execute(
            sa.select(
                oauth_connections.c.encrypted_refresh_token,
                oauth_connections.c.refresh_token_hint,
            ).where(
                oauth_connections.c.provider == "google",
                oauth_connections.c.connection_id == connection["id"],
            )
        ).one_or_none()
    assert stored is not None, "connected account metadata was not persisted"
    assert stored.encrypted_refresh_token
    assert stored.encrypted_refresh_token != "refresh-secret-never-in-response"
    assert "refresh-secret-never-in-response" not in stored.encrypted_refresh_token
    assert stored.refresh_token_hint
    assert "refresh-secret-never-in-response" not in stored.refresh_token_hint
    revoked = client.delete(f"/api/org/oauth/connections/google/{connection['id']}")
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["revoked"] is True


def test_diagnostic_bundle_redacts_content(tmp_path: Path) -> None:
    app = _app(tmp_path)
    owner = TestClient(app)
    _sign_in(owner, app, "owner@example.com")
    secret = "sk-secret-content-9274"
    # Every spelling here is a client-content sink, not merely a known-secret
    # name.  The open diagnostic format may retain shape/count metadata, but it
    # must never keep arbitrary values supplied under these fields.  Exercise
    # case, snake-case, kebab-case, and camelCase spellings so a deny-list of
    # the handful of examples above cannot claim the boundary.
    raw_field_names = (
        "body",
        "data",
        "file",
        "input",
        "messages",
        "output",
        "prompt",
        "promptText",
        "rawCells",
        "cellValue",
        "requestBody",
        "responseBody",
        "rows",
        "value",
        "values",
    )
    raw_context: dict[str, str] = {}
    raw_context_values: list[str] = []
    for index, field in enumerate(raw_field_names):
        words: list[str] = []
        current = ""
        for character in field:
            if character.isupper() and current:
                words.append(current)
                current = character.lower()
            else:
                current += character.lower()
        words.append(current)
        spellings = {
            field,
            field.upper(),
            "_".join(words),
            "-".join(words),
        }
        for spelling in spellings:
            value = f"unexportable-diagnostic-{index}-{len(raw_context_values)}-9917"
            raw_context[spelling] = value
            raw_context_values.append(value)
    secret_key_values = {
        "clientSecret": "client-secret-material-29481",
        "refreshToken": "refresh-token-material-29482",
        "auth-header": "auth-header-material-29483",
        "CREDENTIALS": "credential-material-29484",
        "privateKey": (
            # Credential-shaped test sentinel; keep separate for gitleaks.
            "private-key-camel-material-29485"
        ),
        "private-key": (
            # Credential-shaped test sentinel; keep separate for gitleaks.
            "private-key-kebab-material-29486"
        ),
        "private_key": (
            # Credential-shaped test sentinel; keep separate for gitleaks.
            "private-key-snake-material-29487"
        ),
    }
    raw_context.update(secret_key_values)
    # These are explicitly useful structural telemetry and prove the sanitizer
    # does not satisfy this contract by replacing the entire context.
    raw_context.update(
        {
            "trace_id": "trace-7f2e",
            "browser": "Firefox",
            "row_count": 41,
            "columnCount": 9,
        }
    )
    diagnostic = owner.post(
        "/api/diagnostic-bundle",
        json={
            "message": f"api_key={secret}",
            "include_raw_values": True,
            "context": {"authorization": f"Bearer {secret}", **raw_context},
        },
    )
    assert diagnostic.status_code == 200, diagnostic.text
    payload = diagnostic.json()
    # Cross-producer envelope fact: both editions now assert the same
    # additive `ok` on a persisted-and-audited bundle (the private twin of
    # this file runs against this same public app).
    assert payload.get("ok") is True
    assert isinstance(payload.get("report_id"), int) and payload["report_id"] > 0
    assert isinstance(payload.get("bundle"), dict) and payload["bundle"]
    assert payload["bundle"].get("include_raw_values") is False
    assert secret not in diagnostic.text
    assert all(value not in diagnostic.text for value in raw_context_values), (
        "diagnostic response exposed arbitrary raw context despite the honest false flag"
    )
    assert all(value not in diagnostic.text for value in secret_key_values.values())
    response_bundle = payload["bundle"]
    response_context = response_bundle.get("context", {})
    assert response_context.get("trace_id") == "trace-7f2e"
    assert response_context.get("browser") == "Firefox"
    assert response_context.get("row_count") == 41
    assert response_context.get("columnCount") == 9
    with app.state.control_engine.connect() as cx:
        row = cx.execute(
            sa.select(
                client_errors.c.source,
                client_errors.c.message,
                client_errors.c.bundle_json,
            ).where(client_errors.c.id == payload["report_id"])
        ).one_or_none()
    assert row is not None, (
        "diagnostic bundle must persist a report rather than return a transient object"
    )
    assert row.source == "diagnostic_report"
    assert row.bundle_json, (
        "persisted diagnostic report must retain its sanitized bundle"
    )
    persisted = f"{row.message}\n{row.bundle_json}"
    assert secret not in persisted, "diagnostic report persisted raw secret content"
    assert all(value not in persisted for value in raw_context_values), (
        "diagnostic report persisted arbitrary raw context values"
    )
    assert all(value not in persisted for value in secret_key_values.values())
    persisted_bundle = json.loads(row.bundle_json)
    persisted_context = persisted_bundle.get("context", {})
    assert persisted_context.get("trace_id") == "trace-7f2e"
    assert persisted_context.get("browser") == "Firefox"
    assert persisted_context.get("row_count") == 41
    assert persisted_context.get("columnCount") == 9
    assert "[redacted]" in persisted or "[redacted-key]" in persisted


def test_diagnostic_bundle_rejects_oversize_before_persistence(tmp_path: Path) -> None:
    app = _app(tmp_path)
    owner = TestClient(app)
    _sign_in(owner, app, "owner@example.com")
    with app.state.control_engine.connect() as cx:
        before = cx.execute(
            sa.select(sa.func.count())
            .select_from(client_errors)
            .where(client_errors.c.source == "diagnostic_report")
        ).scalar_one()
    oversized = owner.post(
        "/api/diagnostic-bundle",
        json={"message": "x" * (70 * 1024)},
    )
    with app.state.control_engine.connect() as cx:
        after = cx.execute(
            sa.select(sa.func.count())
            .select_from(client_errors)
            .where(client_errors.c.source == "diagnostic_report")
        ).scalar_one()
    assert oversized.status_code == 413, oversized.text
    assert after == before, "oversize diagnostic request persisted a report"


def test_diagnostic_bundle_authenticated_burst_hits_named_rate_limit(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path)
    owner = TestClient(app)
    _sign_in(owner, app, "owner@example.com")
    limited = None
    statuses: list[int] = []
    for index in range(30):
        response = owner.post(
            "/api/diagnostic-bundle", json={"message": f"burst-{index}"}
        )
        statuses.append(response.status_code)
        if response.status_code == 429:
            limited = response
            break
        assert response.status_code == 200, response.text
    assert limited is not None, (
        f"30 authenticated diagnostic reports were unbounded: {statuses}"
    )
    detail = str(limited.json().get("detail", "")).lower()
    assert "diagnostic" in detail and "rate" in detail, (
        f"diagnostic rate limit returned an unnamed 429: {limited.text}"
    )


def test_diagnostic_report_retention_enforces_age_and_row_bounds(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path)
    owner = TestClient(app)
    _sign_in(owner, app, "owner@example.com")
    now = datetime.now(UTC)
    recent_rows = [
        {
            "org_id": app.state.team_org_id,
            "user_id": None,
            "source": "diagnostic_report",
            "severity": "info",
            "message": f"preseed-recent-{index}",
            "context_json": "{}",
            "bundle_json": "{}",
            "created_at": now - timedelta(seconds=index),
        }
        for index in islice(count(), DEFAULT_DIAGNOSTIC_REPORT_RETENTION_MAX_ROWS + 5)
    ]
    expired_rows = [
        {
            "org_id": app.state.team_org_id,
            "user_id": None,
            "source": "diagnostic_report",
            "severity": "info",
            "message": f"preseed-expired-{index}",
            "context_json": "{}",
            "bundle_json": "{}",
            "created_at": now
            - timedelta(days=DEFAULT_DIAGNOSTIC_REPORT_RETENTION_DAYS + 1),
        }
        for index in range(3)
    ]
    with app.state.control_engine.begin() as cx:
        cx.execute(client_errors.insert(), [*recent_rows, *expired_rows])

    created = owner.post(
        "/api/diagnostic-bundle", json={"message": "retention-trigger"}
    )
    assert created.status_code == 200, created.text
    report_id = created.json().get("report_id")
    with app.state.control_engine.connect() as cx:
        total = cx.execute(
            sa.select(sa.func.count())
            .select_from(client_errors)
            .where(client_errors.c.source == "diagnostic_report")
        ).scalar_one()
        expired = cx.execute(
            sa.select(sa.func.count())
            .select_from(client_errors)
            .where(
                client_errors.c.source == "diagnostic_report",
                client_errors.c.message.like("preseed-expired-%"),
            )
        ).scalar_one()
        trigger_persisted = cx.execute(
            sa.select(client_errors.c.id).where(client_errors.c.id == report_id)
        ).scalar_one_or_none()
    assert expired == 0, "diagnostic reports older than the default age survived"
    assert total <= DEFAULT_DIAGNOSTIC_REPORT_RETENTION_MAX_ROWS, (
        f"diagnostic report retention kept {total} rows"
    )
    assert trigger_persisted == report_id, "retention deleted the triggering report"


def test_mounted_project_blob_keeps_session_pat_rbac_and_range(tmp_path: Path) -> None:
    app = _app(tmp_path)
    owner = TestClient(app)
    _sign_in(owner, app, "owner@example.com")
    project_id = _create_project(owner, "Blob parity")
    project = app.state.workspace.get(project_id)
    digest = project.add_blob(b"abcdefghij", filename="proof.txt", mime="text/plain")
    session = owner.get(f"/api/projects/{project_id}/blobs/{digest}")
    assert session.status_code == 200 and session.content == b"abcdefghij", session.text
    ranged = owner.get(
        f"/api/projects/{project_id}/blobs/{digest}", headers={"Range": "bytes=2-5"}
    )
    assert ranged.status_code == 206 and ranged.content == b"cdef", ranged.text
    token = _pat(owner)
    owner.cookies.clear()
    pat = owner.get(
        f"/api/projects/{project_id}/blobs/{digest}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert pat.status_code == 200 and pat.content == b"abcdefghij", pat.text
    stranger = TestClient(app)
    _sign_in(stranger, app, "stranger@example.com")
    assert stranger.get(f"/api/projects/{project_id}/blobs/{digest}").status_code == 403
