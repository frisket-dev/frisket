import concurrent.futures
import hashlib
import re
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import sqlalchemy as sa
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastapi.routing import APIRoute
from starlette.routing import Mount

from frisket.team.app import TeamConfig, create_team_app
from frisket.team.local_auth import SetupUnavailable, claim_first_owner
from frisket.team.auth_challenges import MAGIC_LINK
from frisket.team.schema import auth_challenges, memberships, pending_invites, users
from frisket.team.setup_gate import SetupGate


def _app(tmp_path: Path, **overrides: Any):
    values = dict(
        database_url=f"sqlite:///{tmp_path / 'control.db'}",
        run_queue_database_url=f"sqlite:///{tmp_path / 'queue.db'}",
        data_dir=tmp_path / "data",
        base_url="http://testserver",
        organization_name="Initial name",
        magic_link_enabled=False,
    )
    values.update(overrides)
    return create_team_app(TeamConfig(**values))


def _claim(client: TestClient, app) -> int:
    return client.post(
        "/setup",
        data={
            "claim_token": app.state.setup_claim_token,
            "workspace_name": "Investigations",
            "owner_name": "Owner",
            "email": "owner@example.test",
            "password": "a sufficiently long password",
            "password_confirmation": "a sufficiently long password",
        },
        headers={"Origin": "http://testserver"},
        follow_redirects=False,
    ).status_code


def test_team_outer_lifespan_joins_mounted_core_preview_workers(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path)
    registry = app.state.action_preview_job_registry
    started = threading.Event()
    stopped = threading.Event()

    def run(_progress_cb, cancel_event):
        started.set()
        cancel_event.wait()
        stopped.set()
        return None

    with TestClient(app):
        job = registry.start("team-project-lifespan", 1, run)
        assert started.wait(2.0)

    # Starlette does not run mounted sub-app lifespans; this can pass only
    # because create_team_app wires the core registry into the outer lifespan.
    assert stopped.is_set()
    assert job.status == "cancelled"
    assert all(not thread.is_alive() for thread in registry._threads.values())


def test_team_base_url_rejects_a_path_because_browser_origin_omits_it(
    tmp_path: Path,
):
    with pytest.raises(ValueError, match=r"absolute credential-free http\(s\) origin"):
        _app(tmp_path, base_url="https://frisket.example.test/subpath")


def test_empty_server_is_gated_then_claimed_and_password_login_works(tmp_path: Path):
    app = _app(tmp_path)
    client = TestClient(app)
    assert client.get("/setup").status_code == 200
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/me").status_code == 503
    assert _claim(client, app) == 303
    assert client.get("/setup").status_code == 404
    client.cookies.clear()
    assert (
        client.post(
            "/auth/password-login",
            json={
                "email": "owner@example.test",
                "password": "a sufficiently long password",
            },
            headers={"Origin": "http://testserver"},
            follow_redirects=False,
        ).status_code
        == 200
    )


def test_empty_workspace_name_defaults_to_workspace_instead_of_blocking(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path)
    client = TestClient(app)
    response = client.post(
        "/setup",
        data={
            "claim_token": app.state.setup_claim_token,
            "workspace_name": "   ",
            "owner_name": "Owner",
            "email": "owner@example.test",
            "password": "a sufficiently long password",
            "password_confirmation": "a sufficiently long password",
        },
        headers={"Origin": "http://testserver"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with app.state.control_engine.connect() as connection:
        name = connection.execute(
            sa.text("select name from orgs where id = :org_id"),
            {"org_id": app.state.team_org_id},
        ).scalar_one()
    assert name == "workspace"


def test_setup_code_is_single_use_and_origin_is_required(tmp_path: Path):
    app = _app(tmp_path)
    client = TestClient(app)
    token = app.state.setup_claim_token
    assert client.post("/setup", data={"claim_token": token}).status_code == 403
    for index in range(8):
        assert (
            client.post(
                "/setup",
                data={"claim_token": f"wrong-code-{index}"},
                headers={"Origin": "http://testserver"},
            ).status_code
            == 403
        )
    assert _claim(client, app) == 303
    assert (
        client.post(
            "/setup",
            data={"claim_token": token},
            headers={"Origin": "http://testserver"},
        ).status_code
        == 404
    )


def test_setup_code_is_emitted_only_to_startup_and_claim_survives_restart(
    tmp_path: Path, caplog, capsys
):
    app = _app(tmp_path)
    token = app.state.setup_claim_token
    emitted = capsys.readouterr().err
    assert f"FRISKET_SETUP_CODE {token} /setup" in emitted
    assert token not in caplog.text

    client = TestClient(app)
    for path in ("/setup", "/api/health", "/api/ready"):
        response = client.get(path)
        assert token not in response.text
    ready = client.get("/api/ready").json()
    assert ready["identity"]["schema_version"] == "frisket.server_identity.v1"
    assert set(ready["identity"]) == {
        "schema_version",
        "package_version",
        "code_version",
    }
    assert client.get("/setup").headers["cache-control"] == "no-store"
    assert _claim(client, app) == 303

    restarted = _app(tmp_path)
    assert restarted.state.setup_claim_token is None
    closed = TestClient(restarted).get("/setup")
    assert closed.status_code == 404
    assert closed.headers["cache-control"] == "no-store"


def test_setup_page_explains_the_claim_lifecycle_without_leaking_code(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path)
    client = TestClient(app)
    token = app.state.setup_claim_token

    setup = client.get("/setup")
    assert setup.status_code == 200
    assert setup.headers["cache-control"] == "no-store"
    assert '<html lang="en">' in setup.text
    assert '<main class="shell">' in setup.text
    assert 'aria-labelledby="page-title"' in setup.text
    assert "FRISKET_SETUP_CODE" in setup.text
    assert "current startup logs" in setup.text
    assert "older pre-claim code is stale" in setup.text
    assert "restart the Frisket app service—not the database" in setup.text
    assert "12–1,024 characters" in setup.text
    assert 'autocomplete="one-time-code"' in setup.text
    assert 'autocomplete="new-password"' in setup.text
    assert token not in setup.text
    # The field hints "workspace" as a placeholder (not "Investigations") and
    # carries no `required` attribute — an empty submission defaults
    # server-side instead of being blocked client-side.
    assert 'placeholder="workspace"' in setup.text
    assert (
        'name="workspace_name" autocomplete="organization" maxlength="200"\n            placeholder="workspace">'
        in setup.text
    )


def test_competing_claims_create_exactly_one_owner_and_close_every_process(
    tmp_path: Path,
):
    app = _app(tmp_path)
    token = app.state.setup_claim_token

    def claim(index: int) -> str:
        try:
            claim_first_owner(
                app.state.control_engine,
                org_id=app.state.team_org_id,
                claim_token=token,
                expected_claim_token=token,
                workspace_name="Investigations",
                owner_name=f"Owner {index}",
                email=f"owner{index}@example.test",
                password="a sufficiently long password",
            )
            return "claimed"
        except SetupUnavailable:
            return "closed"

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = sorted(pool.map(claim, range(2)))
    assert outcomes == ["claimed", "closed"]
    with app.state.control_engine.connect() as connection:
        assert (
            connection.execute(
                sa.select(sa.func.count())
                .select_from(memberships)
                .where(memberships.c.role == "owner")
            ).scalar_one()
            == 1
        )
    assert TestClient(app).get("/setup").status_code == 404


def test_unknown_and_wrong_password_have_one_no_store_response(tmp_path: Path):
    app = _app(tmp_path)
    client = TestClient(app)
    assert _claim(client, app) == 303
    client.cookies.clear()
    unknown = client.post(
        "/auth/password-login",
        json={"email": "unknown@example.test", "password": "incorrect password"},
        headers={"Origin": "http://testserver"},
    )
    wrong = client.post(
        "/auth/password-login",
        json={"email": "owner@example.test", "password": "incorrect password"},
        headers={"Origin": "http://testserver"},
    )
    assert (unknown.status_code, unknown.json()) == (wrong.status_code, wrong.json())
    assert unknown.status_code == 401
    assert unknown.headers["cache-control"] == "no-store"
    assert wrong.headers["cache-control"] == "no-store"


def test_password_login_rejects_cross_origin_and_does_not_share_proxy_limit(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path)
    client = TestClient(app, client=("proxy", 50000))
    assert _claim(client, app) == 303
    client.cookies.clear()

    rejected = client.post(
        "/auth/password-login",
        json={
            "email": "owner@example.test",
            "password": "a sufficiently long password",
        },
        headers={"Origin": "https://attacker.example"},
        follow_redirects=False,
    )
    assert rejected.status_code == 403
    assert rejected.headers["cache-control"] == "no-store"

    # All requests have the same apparent proxy address. Rotating unknown
    # identities must not consume the real owner's limiter bucket.
    for index in range(20):
        failed = client.post(
            "/auth/password-login",
            json={
                "email": f"unknown-{index}@example.test",
                "password": "incorrect password",
            },
            headers={"Origin": "http://testserver"},
        )
        assert failed.status_code == 401
    assert (
        client.post(
            "/auth/password-login",
            json={
                "email": "owner@example.test",
                "password": "a sufficiently long password",
            },
            headers={"Origin": "http://testserver"},
            follow_redirects=False,
        ).status_code
        == 200
    )


def test_logout_revokes_the_server_side_session(tmp_path: Path):
    app = _app(tmp_path)
    client = TestClient(app)
    assert _claim(client, app) == 303
    token = client.cookies.get("frisket_session")
    assert token
    response = client.post("/auth/logout")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    client.cookies.set("frisket_session", token)
    assert client.get("/api/me").status_code == 401


def test_optional_magic_link_rejects_uninvited_identity_cleanly(tmp_path: Path):
    links: list[str] = []

    async def mail(_email: str, link: str) -> bool:
        links.append(link)
        return True

    config = TeamConfig(
        database_url=f"sqlite:///{tmp_path / 'control.db'}",
        run_queue_database_url=f"sqlite:///{tmp_path / 'queue.db'}",
        data_dir=tmp_path / "data",
        base_url="http://testserver",
        organization_name="Initial name",
        magic_link_enabled=True,
    )
    app = create_team_app(config, send_magic_email=mail)
    owner = TestClient(app)
    assert _claim(owner, app) == 303
    visitor = TestClient(app)
    assert (
        visitor.post(
            "/auth/request-link", json={"email": "visitor@example.test"}
        ).status_code
        == 200
    )
    token = parse_qs(urlparse(links[-1]).query)["token"][0]
    denied = visitor.get(f"/auth/callback?token={token}")
    assert denied.status_code == 403
    assert denied.headers["cache-control"] == "no-store"


def _magic_app(tmp_path: Path):
    links: list[str] = []

    async def mail(_email: str, link: str) -> bool:
        links.append(link)
        return True

    config = TeamConfig(
        database_url=f"sqlite:///{tmp_path / 'control.db'}",
        run_queue_database_url=f"sqlite:///{tmp_path / 'queue.db'}",
        data_dir=tmp_path / "data",
        base_url="http://testserver",
        organization_name="Initial name",
        magic_link_enabled=True,
    )
    app = create_team_app(config, send_magic_email=mail)
    app.state.test_magic_links = links
    return app


def _magic_token(client: TestClient, app, email: str) -> str:
    assert client.post("/auth/request-link", json={"email": email}).status_code == 200
    return parse_qs(urlparse(app.state.test_magic_links[-1]).query)["token"][0]


def _token_used(app, token: str) -> bool:
    secret_hash = hashlib.sha256(token.encode()).hexdigest()
    with app.state.control_engine.connect() as connection:
        return (
            connection.execute(
                sa.select(auth_challenges.c.consumed_at).where(
                    auth_challenges.c.secret_hash == secret_hash,
                    auth_challenges.c.kind == MAGIC_LINK,
                )
            ).scalar_one()
            is not None
        )


def _complete_new_magic_identity(client: TestClient, token: str) -> None:
    start = client.get(f"/auth/callback?token={token}", follow_redirects=False)
    assert start.status_code == 302
    assert "auth_kind=magic-link" in start.headers["location"]
    completed = client.post(
        f"/auth/callback?token={token}",
        json={"password": "a sufficiently long password"},
    )
    assert completed.status_code == 200, completed.text


def test_callback_refuses_to_switch_a_signed_in_owner_to_a_different_invitee(
    tmp_path: Path,
):
    app = _magic_app(tmp_path)
    owner = TestClient(app)
    assert _claim(owner, app) == 303

    # An invited (redeemable) second identity, so the link stays genuinely
    # usable by the real invitee after the owner's click is refused.
    with app.state.control_engine.connect() as connection:
        org_id = connection.execute(sa.select(memberships.c.org_id)).scalar_one()
    with app.state.control_engine.begin() as connection:
        connection.execute(
            pending_invites.insert().values(
                email="invitee@example.test",
                org_id=org_id,
                role="member",
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )
    token = _magic_token(owner, app, "invitee@example.test")

    refused = owner.get(f"/auth/callback?token={token}", follow_redirects=False)
    assert refused.status_code == 409
    # The one-time token was NOT consumed by the refusal.
    assert _token_used(app, token) is False
    # The owner's own session is untouched — still signed in as the owner.
    me = owner.get("/api/me")
    assert me.status_code == 200
    assert me.json()["email"] == "owner@example.test"

    # The real invitee (a fresh, unauthenticated browser) can still redeem it.
    invitee = TestClient(app)
    _complete_new_magic_identity(invitee, token)
    assert invitee.get("/api/me").json()["email"] == "invitee@example.test"
    assert _token_used(app, token) is True


def test_callback_reinvites_a_removed_existing_identity_and_claims_before_grant(
    tmp_path: Path,
) -> None:
    app = _magic_app(tmp_path)
    owner = TestClient(app)
    assert _claim(owner, app) == 303
    email = "returning@example.test"
    with app.state.control_engine.connect() as connection:
        org_id = int(connection.execute(sa.select(memberships.c.org_id)).scalar_one())
    with app.state.control_engine.begin() as connection:
        connection.execute(
            pending_invites.insert().values(
                email=email,
                org_id=org_id,
                role="member",
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )
    returning = TestClient(app)
    first_token = _magic_token(returning, app, email)
    _complete_new_magic_identity(returning, first_token)
    with app.state.control_engine.begin() as connection:
        user_id = int(
            connection.execute(
                sa.select(users.c.id).where(users.c.email == email)
            ).scalar_one()
        )
        connection.execute(
            memberships.delete().where(
                memberships.c.user_id == user_id,
                memberships.c.org_id == org_id,
            )
        )
        connection.execute(
            auth_challenges.delete().where(
                auth_challenges.c.kind == MAGIC_LINK,
                auth_challenges.c.subject_email == email,
            )
        )
        connection.execute(
            pending_invites.insert().values(
                email=email,
                org_id=org_id,
                role="member",
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )
    returning.cookies.clear()
    reissued_token = _magic_token(returning, app, email)
    accepted = returning.get(
        f"/auth/callback?token={reissued_token}", follow_redirects=False
    )

    assert accepted.status_code == 302, accepted.text
    assert _token_used(app, reissued_token) is True
    with app.state.control_engine.connect() as connection:
        assert (
            connection.execute(
                sa.select(memberships.c.role).where(
                    memberships.c.user_id == user_id,
                    memberships.c.org_id == org_id,
                )
            ).scalar_one()
            == "member"
        )
        assert (
            connection.execute(
                sa.select(pending_invites.c.email).where(
                    pending_invites.c.email == email
                )
            ).scalar_one_or_none()
            is None
        )


@pytest.mark.parametrize("state", ["revoked", "expired"])
def test_callback_does_not_grant_membership_from_a_nonlive_invite(
    tmp_path: Path, state: str
) -> None:
    app = _magic_app(tmp_path)
    owner = TestClient(app)
    assert _claim(owner, app) == 303
    email = f"{state}@example.test"
    visitor = TestClient(app)
    token = _magic_token(visitor, app, email)
    with app.state.control_engine.connect() as connection:
        org_id = int(connection.execute(sa.select(memberships.c.org_id)).scalar_one())
    with app.state.control_engine.begin() as connection:
        connection.execute(
            pending_invites.insert().values(
                email=email,
                org_id=org_id,
                role="member",
                expires_at=datetime.now(UTC)
                + (timedelta(hours=1) if state == "revoked" else -timedelta(hours=1)),
            )
        )
        if state == "revoked":
            connection.execute(
                pending_invites.delete().where(pending_invites.c.email == email)
            )

    denied = visitor.get(f"/auth/callback?token={token}", follow_redirects=False)

    assert denied.status_code == 403, denied.text
    with app.state.control_engine.connect() as connection:
        assert (
            connection.execute(
                sa.select(sa.func.count())
                .select_from(memberships)
                .join(users, users.c.id == memberships.c.user_id)
                .where(users.c.email == email)
            ).scalar_one()
            == 0
        )


def test_callback_for_the_signed_in_users_own_email_is_a_noop(tmp_path: Path):
    app = _magic_app(tmp_path)
    owner = TestClient(app)
    assert _claim(owner, app) == 303
    session_before = owner.cookies.get("frisket_session")

    token = _magic_token(owner, app, "owner@example.test")
    response = owner.get(f"/auth/callback?token={token}", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/"
    # No cookie was reissued and the token was not consumed.
    assert "set-cookie" not in response.headers
    assert _token_used(app, token) is False
    assert owner.cookies.get("frisket_session") == session_before
    assert owner.get("/api/me").json()["email"] == "owner@example.test"


def test_callback_signs_in_an_unauthenticated_visitor_as_the_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    app = _magic_app(tmp_path)
    owner = TestClient(app)
    assert _claim(owner, app) == 303

    # Fresh browser with no session redeems a link for the existing owner.
    visitor = TestClient(app)
    monkeypatch.setattr(
        "frisket.team.auth_challenges.secrets.token_urlsafe",
        lambda _bytes: "oidc_magic-link-spelling",
    )
    token = _magic_token(visitor, app, "owner@example.test")
    assert token == "oidc_magic-link-spelling"
    response = visitor.get(f"/auth/callback?token={token}", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/"
    assert visitor.cookies.get("frisket_session")
    assert _token_used(app, token) is True
    assert visitor.get("/api/me").json()["email"] == "owner@example.test"


def _http_routes(routes, prefix: str = ""):
    for route in routes:
        if isinstance(route, APIRoute):
            yield prefix + route.path, route.methods or set()
        elif isinstance(route, Mount):
            child_prefix = prefix + ("" if route.path == "/" else route.path)
            yield from _http_routes(route.routes, child_prefix)


def test_unclaimed_gate_denies_the_registered_route_inventory(tmp_path: Path):
    app = _app(tmp_path)
    client = TestClient(app)
    allowed = {
        ("GET", "/setup"),
        ("POST", "/setup"),
        ("GET", "/api/health"),
        ("GET", "/api/ready"),
    }
    checked: set[tuple[str, str]] = set()
    for template, methods in _http_routes(app.routes):
        path = re.sub(r"\{[^}]+\}", "1", template)
        for method in methods - {"HEAD", "OPTIONS"}:
            if (method, template) in allowed:
                continue
            response = client.request(method, path)
            assert response.status_code == 503, (method, template, response.text)
            checked.add((method, template))
    for path in ("/", "/assets/app.js", "/does-not-exist"):
        assert client.get(path).status_code == 503
    assert len(checked) > 40


def test_health_and_readiness_do_not_depend_on_setup_claim_lookup() -> None:
    inner = FastAPI()

    @inner.get("/api/health")
    def health():
        return {"ok": True}

    @inner.get("/api/ready")
    def ready():
        return {"ok": False}

    def unavailable_claim_state() -> bool:
        raise RuntimeError("control database is unavailable")

    client = TestClient(SetupGate(inner, claimed=unavailable_claim_state))
    assert client.get("/api/health").json() == {"ok": True}
    assert client.get("/api/ready").json() == {"ok": False}
