from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import subprocess
import sys
from types import SimpleNamespace
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
import sqlalchemy as sa
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from frisket.contracts.http.models import ProjectList
from frisket.team.app import TeamConfig, create_team_app
from frisket.team.bootstrap import PreSplitSchemaError
from frisket.team.bootstrap import schema_marker
from frisket.team.config import team_config_from_env
from frisket.team.policy import TeamOuterPolicyError, install_outer_policy
from frisket.team.schema import (
    api_tokens,
    audit_log,
    client_errors,
    memberships,
    oidc_identities,
    org_env_vars,
    org_keys,
    orgs,
    project_creation_intents,
    project_invites,
    project_roles,
    projects,
    users,
)
from tests.team_setup_helpers import (
    claim_server,
    seed_member_invite,
    sign_in_with_magic_link,
)


async def _mail(_email: str, _link: str) -> bool:
    return True


async def _oidc(_provider: str, _code: str, _redirect: str, nonce: str) -> dict:
    return {
        "email": "oidc@example.com",
        "email_verified": True,
        "subject": "subject-1",
        "nonce": nonce,
    }


def _config(tmp_path: Path, **overrides) -> TeamConfig:
    values = dict(
        database_url=f"sqlite:///{tmp_path / 'control.db'}",
        # create_team_app now requires this unconditionally (deferred
        # follow-up from the queue-composition audit, "general run-queue mismatch",
        # completed). Tests that specifically pin the absent-locator
        # behavior override this back to None explicitly.
        run_queue_database_url=f"sqlite:///{tmp_path / 'run-queue.db'}",
        data_dir=tmp_path / "data",
        base_url="http://testserver",
        organization_name="Desk",
        admin_emails={"owner@example.com"},
        oidc_providers={
            "work": {
                "issuer": "https://id.test",
                "client_id": "client",
                "client_secret": "secret",
                "authorization_endpoint": "https://id.test/authorize",
                "token_endpoint": "https://id.test/token",
                "jwks_uri": "https://id.test/jwks",
            }
        },
    )
    values.update(overrides)
    return TeamConfig(**values)


def _app(tmp_path: Path, **overrides):
    config = _config(tmp_path, **overrides)
    app = create_team_app(config, send_magic_email=_mail, oidc_exchange=_oidc)
    claim_server(app, origin=config.base_url)
    for email in (
        "alice@example.com",
        "bob@example.com",
        "member@example.com",
        "oidc@example.com",
    ):
        seed_member_invite(app, email)
    return app


def _login(client: TestClient, app, email: str) -> None:
    sign_in_with_magic_link(app, client, email)


def test_default_auth_channels_fail_startup_without_real_transport(
    tmp_path: Path,
) -> None:
    assert create_team_app(_config(tmp_path, oidc_providers={})).state.setup_claim_token
    assert create_team_app(
        _config(tmp_path, magic_link_enabled=False, oidc_providers={})
    ).state.setup_claim_token
    config = team_config_from_env(
        {
            "FRISKET_MAGIC_LINK_ENABLED": "false",
            "FRISKET_TEAM_DATABASE_URL": f"sqlite:///{tmp_path / 'env.db'}",
            "FRISKET_DATA_DIR": str(tmp_path / "env-data"),
            "FRISKET_BASE_URL": "https://team.example.test",
            "FRISKET_ORGANIZATION_NAME": "Newsroom",
        }
    )
    assert config.magic_link_enabled is False


def test_oidc_state_is_server_side_one_time_and_https_cookie_is_secure(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path, base_url="https://team.example.test")
    client = TestClient(app, base_url="https://team.example.test")
    start = client.get("/auth/oidc/work", follow_redirects=False)
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    assert client.get(f"/auth/callback?token={state}").status_code == 400
    first = client.get(
        f"/auth/oidc/work/callback?code=ok&state={state}", follow_redirects=False
    )
    assert first.status_code == 302
    assert "Secure" in first.headers["set-cookie"]
    replay = client.get(
        f"/auth/oidc/work/callback?code=ok&state={state}", follow_redirects=False
    )
    assert replay.status_code == 400


def test_oidc_binding_uses_issuer_subject_not_mutable_email(tmp_path: Path) -> None:
    async def exchange(_provider: str, code: str, _redirect: str, nonce: str) -> dict:
        subject = "stable-subject" if code != "new-subject" else "other-subject"
        email = "changed@example.com" if code == "changed" else "first@example.com"
        return {
            "email": email,
            "email_verified": True,
            "subject": subject,
            "nonce": nonce,
        }

    app = create_team_app(
        _config(tmp_path), send_magic_email=_mail, oidc_exchange=exchange
    )
    claim_server(app)
    seed_member_invite(app, "first@example.com")
    client = TestClient(app)

    def callback(code: str) -> int:
        start = client.get("/auth/oidc/work", follow_redirects=False)
        state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
        return client.get(
            f"/auth/oidc/work/callback?code={code}&state={state}",
            follow_redirects=False,
        ).status_code

    assert callback("first") == 302
    assert callback("changed") == 302
    with app.state.control_engine.connect() as cx:
        assert cx.execute(
            sa.select(users.c.email).where(users.c.email != "owner@example.com")
        ).scalars().all() == ["first@example.com"]
        assert (
            cx.execute(
                sa.select(sa.func.count()).select_from(oidc_identities)
            ).scalar_one()
            == 1
        )
    assert callback("new-subject") == 409


def test_concurrent_oidc_callbacks_consume_state_once(tmp_path: Path) -> None:
    app = _app(tmp_path)
    initiating = TestClient(app)
    start = initiating.get("/auth/oidc/work", follow_redirects=False)
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    cookie = initiating.cookies.get("frisket_oidc_work")

    def invoke() -> int:
        client = TestClient(app)
        client.cookies.set("frisket_oidc_work", cookie)
        return client.get(
            f"/auth/oidc/work/callback?code=ok&state={state}",
            follow_redirects=False,
        ).status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        statuses = sorted(pool.map(lambda _index: invoke(), range(2)))
    assert statuses == [302, 400]


def test_member_project_visibility_and_pat_inventory_are_actor_scoped(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path)
    owner, alice, bob = TestClient(app), TestClient(app), TestClient(app)
    _login(owner, app, "owner@example.com")
    _login(alice, app, "alice@example.com")
    _login(bob, app, "bob@example.com")
    owner_project = owner.post("/api/projects", json={"name": "Owner only"}).json()[
        "id"
    ]
    alice_project = alice.post("/api/projects", json={"name": "Alice only"}).json()[
        "id"
    ]
    listed = ProjectList.model_validate(alice.get("/api/projects").json()).root
    assert {row.id: row.role for row in listed} == {alice_project: "owner"}
    assert bob.get(f"/api/projects/{owner_project}").status_code == 403
    alice.post("/api/org/tokens", json={"name": "alice"})
    bob.post("/api/org/tokens", json={"name": "bob"})
    assert [row["name"] for row in alice.get("/api/org/tokens").json()] == ["alice"]
    assert [row["name"] for row in bob.get("/api/org/tokens").json()] == ["bob"]
    with app.state.control_engine.connect() as cx:
        assert (
            cx.execute(sa.select(sa.func.count()).select_from(api_tokens)).scalar_one()
            == 2
        )


def test_out_of_scope_pat_is_unauthorized_before_team_route_dispatch(
    tmp_path: Path,
) -> None:
    app = create_team_app(
        _config(tmp_path), send_magic_email=_mail, oidc_exchange=_oidc
    )
    project_id = "org-fence-proof"

    # A second org cannot be created through the singleton bootstrap API, so
    # the cross-org credential is possible only through this direct fixture.
    token = "frisket_pat_cross_org_fixture"
    with app.state.control_engine.begin() as cx:
        owner_id = cx.execute(
            users.insert().values(
                email="owner@example.com",
                default_org_id=1,
            )
        ).inserted_primary_key[0]
        cx.execute(
            memberships.insert().values(user_id=owner_id, org_id=1, role="owner")
        )
        cx.execute(
            projects.insert().values(
                org_id=1,
                storage_org_id=1,
                slug=project_id,
                name="Org fence proof",
                sensitive=False,
                network="inherit",
            )
        )
        cx.execute(orgs.insert().values(id=2, name="Other org"))
        user_id = cx.execute(
            users.insert().values(
                email="other-org@example.com",
                default_org_id=2,
            )
        ).inserted_primary_key[0]
        # This membership is essential: resolve_token already joins through it.
        cx.execute(memberships.insert().values(user_id=user_id, org_id=2, role="owner"))
        cx.execute(
            api_tokens.insert().values(
                org_id=2,
                user_id=user_id,
                name="cross-org",
                token_hash=hashlib.sha256(token.encode()).hexdigest(),
                prefix="frisket_pat_cross",
            )
        )
        before = {
            "invites": cx.execute(
                sa.select(sa.func.count()).select_from(project_invites)
            ).scalar_one(),
            "intents": cx.execute(
                sa.select(sa.func.count()).select_from(project_creation_intents)
            ).scalar_one(),
            "projects": cx.execute(
                sa.select(sa.func.count()).select_from(projects)
            ).scalar_one(),
            "deletions": cx.execute(
                sa.select(sa.func.count())
                .select_from(audit_log)
                .where(audit_log.c.action == "project_deleted")
            ).scalar_one(),
            "audit": cx.execute(
                sa.select(sa.func.count()).select_from(audit_log)
            ).scalar_one(),
        }

    def endpoint(path: str, method: str):
        return next(
            route.endpoint
            for route in app.routes
            if getattr(route, "path", None) == path
            and method in getattr(route, "methods", set())
        )

    def request(method: str, path: str, *, bearer: str = token) -> Request:
        return Request(
            {
                "type": "http",
                "method": method,
                "path": path,
                "headers": [(b"authorization", f"Bearer {bearer}".encode())],
            }
        )

    calls = (
        lambda: endpoint("/api/projects", "POST")(
            request("POST", "/api/projects"),
            SimpleNamespace(name="Must not exist", sensitive=False),
        ),
        lambda: endpoint("/api/projects/{pid}/invites", "GET")(
            project_id, request("GET", f"/api/projects/{project_id}/invites")
        ),
        lambda: asyncio.run(
            endpoint("/api/projects/{pid}/invites", "POST")(
                project_id,
                request("POST", f"/api/projects/{project_id}/invites"),
                SimpleNamespace(email="invitee@example.com", role="viewer"),
            )
        ),
        lambda: endpoint("/api/projects/{pid}/invites/{invite_id}", "DELETE")(
            project_id,
            999,
            request("DELETE", f"/api/projects/{project_id}/invites/999"),
        ),
        lambda: endpoint("/api/projects/{pid}", "DELETE")(
            project_id,
            request("DELETE", f"/api/projects/{project_id}"),
            SimpleNamespace(confirm_name="Org fence proof"),
        ),
    )
    auth_errors = []
    for call in calls:
        with pytest.raises(HTTPException) as raised:
            call()
        assert raised.value.status_code == 401
        auth_errors.append(raised.value)

    with pytest.raises(HTTPException) as bad_token:
        endpoint("/api/projects", "POST")(
            request(
                "POST",
                "/api/projects",
                bearer="frisket_pat_invalid_fixture",
            ),
            SimpleNamespace(name="Must not exist", sensitive=False),
        )
    assert (auth_errors[0].status_code, auth_errors[0].detail) == (
        bad_token.value.status_code,
        bad_token.value.detail,
    )

    with app.state.control_engine.connect() as cx:
        after = {
            "invites": cx.execute(
                sa.select(sa.func.count()).select_from(project_invites)
            ).scalar_one(),
            "intents": cx.execute(
                sa.select(sa.func.count()).select_from(project_creation_intents)
            ).scalar_one(),
            "projects": cx.execute(
                sa.select(sa.func.count()).select_from(projects)
            ).scalar_one(),
            "deletions": cx.execute(
                sa.select(sa.func.count())
                .select_from(audit_log)
                .where(audit_log.c.action == "project_deleted")
            ).scalar_one(),
            "audit": cx.execute(
                sa.select(sa.func.count()).select_from(audit_log)
            ).scalar_one(),
        }
    assert after == before

    in_scope_token = "frisket_pat_in_scope_fixture"
    with app.state.control_engine.begin() as cx:
        cx.execute(
            api_tokens.insert().values(
                org_id=1,
                user_id=owner_id,
                name="in-scope",
                token_hash=hashlib.sha256(in_scope_token.encode()).hexdigest(),
                prefix="frisket_pat_in",
            )
        )
    assert endpoint("/api/projects/{pid}/invites", "GET")(
        project_id,
        request(
            "GET",
            f"/api/projects/{project_id}/invites",
            bearer=in_scope_token,
        ),
    ) == {"invites": []}


def test_dual_member_cannot_use_org_two_pat_on_org_one_routes(tmp_path: Path) -> None:
    app = create_team_app(
        _config(tmp_path), send_magic_email=_mail, oidc_exchange=_oidc
    )
    target_project = "dual-member-target"
    app.state.workspace.create("Dual-member invite target", project_id=target_project)
    token = "frisket_pat_dual_member_org_two"
    with app.state.control_engine.begin() as cx:
        cx.execute(orgs.insert().values(id=2, name="Other org"))
        user_id = cx.execute(
            users.insert().values(
                email="dual-member@example.com",
                default_org_id=2,
            )
        ).inserted_primary_key[0]
        cx.execute(
            memberships.insert(),
            (
                {"user_id": user_id, "org_id": 1, "role": "member"},
                {"user_id": user_id, "org_id": 2, "role": "member"},
            ),
        )
        cx.execute(
            api_tokens.insert().values(
                org_id=2,
                user_id=user_id,
                name="dual-member-org-two",
                token_hash=hashlib.sha256(token.encode()).hexdigest(),
                prefix="frisket_pat_dual",
            )
        )
        cx.execute(
            projects.insert().values(
                org_id=1,
                storage_org_id=1,
                slug=target_project,
                name="Dual-member invite target",
                sensitive=False,
                network="inherit",
            )
        )
        cx.execute(
            project_roles.insert().values(
                org_id=1,
                slug=target_project,
                user_id=user_id,
                role="editor",
            )
        )

    def endpoint(path: str, method: str):
        return next(
            route.endpoint
            for route in app.routes
            if getattr(route, "path", None) == path
            and method in getattr(route, "methods", set())
        )

    def request(method: str, path: str) -> Request:
        return Request(
            {
                "type": "http",
                "method": method,
                "path": path,
                "headers": [(b"authorization", f"Bearer {token}".encode())],
            }
        )

    def status(call) -> int:
        try:
            call()
        except HTTPException as exc:
            return exc.status_code
        return 200

    project_status = status(
        lambda: endpoint("/api/projects", "POST")(
            request("POST", "/api/projects"),
            SimpleNamespace(name="Dual-member escalation", sensitive=False),
        )
    )
    invite_status = status(
        lambda: asyncio.run(
            endpoint("/api/projects/{pid}/invites", "POST")(
                target_project,
                request("POST", f"/api/projects/{target_project}/invites"),
                SimpleNamespace(email="escalated-invite@example.com", role="viewer"),
            )
        )
    )

    with app.state.control_engine.connect() as cx:
        created_projects = cx.execute(
            sa.select(sa.func.count())
            .select_from(projects)
            .where(projects.c.name == "Dual-member escalation")
        ).scalar_one()
        created_invites = cx.execute(
            sa.select(sa.func.count())
            .select_from(project_invites)
            .where(project_invites.c.email == "escalated-invite@example.com")
        ).scalar_one()
    assert (
        project_status,
        invite_status,
        created_projects,
        created_invites,
    ) == (401, 401, 0, 0)


def test_secret_upsert_delete_validation_and_diagnostic_redaction(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path)
    client = TestClient(app)
    _login(client, app, "owner@example.com")
    assert (
        client.post("/api/org/keys", json={"provider": "bogus", "key": "x"}).status_code
        == 400
    )
    client.post("/api/org/keys", json={"provider": "openai", "key": "sk-first-secret"})
    client.post("/api/org/keys", json={"provider": "openai", "key": "sk-second-secret"})
    assert client.get("/api/org/keys").json() == [
        {"provider": "openai", "hint": "...cret"}
    ]
    assert (
        client.post("/api/org/env", json={"name": "1BAD", "value": "x"}).status_code
        == 400
    )
    client.post("/api/org/env", json={"name": "REPORT_FROM", "value": "one"})
    client.post("/api/org/env", json={"name": "REPORT_FROM", "value": "two"})
    assert client.delete("/api/org/env/REPORT_FROM").json() == {"deleted": True}
    client.post(
        "/api/client-errors",
        json={
            "name": "Oops",
            "message": "api_key=sk-super-secret-value",
            "source": "browser",
        },
    )
    feed = client.get("/api/admin/errors").json()
    assert "sk-super" not in feed["errors"][0]["message"]
    assert feed["schema_version"] == "frisket.admin_errors.v1"
    with app.state.control_engine.connect() as cx:
        assert (
            cx.execute(sa.select(sa.func.count()).select_from(org_keys)).scalar_one()
            == 1
        )
        assert (
            cx.execute(
                sa.select(sa.func.count()).select_from(org_env_vars)
            ).scalar_one()
            == 0
        )
        assert (
            cx.execute(
                sa.select(sa.func.count()).select_from(client_errors)
            ).scalar_one()
            == 1
        )
    assert set(client.get("/api/admin/jobs").json()) == {
        "schema_version",
        "summary",
        "workers",
        "jobs",
    }


def test_membership_admin_preserves_last_owner(tmp_path: Path) -> None:
    app = _app(tmp_path)
    owner, member = TestClient(app), TestClient(app)
    _login(owner, app, "owner@example.com")
    _login(member, app, "member@example.com")
    with app.state.control_engine.connect() as cx:
        rows = dict(cx.exec_driver_sql("SELECT email, id FROM users").all())
    owner_id, member_id = rows["owner@example.com"], rows["member@example.com"]
    assert (
        owner.patch(
            f"/api/admin/users/{owner_id}/role", json={"role": "member"}
        ).status_code
        == 409
    )
    assert owner.delete(f"/api/admin/users/{owner_id}").status_code == 409
    assert (
        owner.patch(
            f"/api/admin/users/{member_id}/role", json={"role": "owner"}
        ).status_code
        == 200
    )
    assert (
        owner.patch(
            f"/api/admin/users/{owner_id}/role", json={"role": "member"}
        ).status_code
        == 200
    )


def test_admin_operational_routes_refuse_anonymous_and_non_admin_callers(
    tmp_path: Path,
) -> None:
    """Nobody but an admin reaches the admin operational surface.

    Coverage here was positive-only — an owner gets 200, and nothing
    asserted that anyone else does not. This adds the two negative tiers:
    anonymous -> 401, authenticated NON-admin member -> 403, on a job
    remediation route and on the audit log.

    **What actually guards these routes** (measured by mutation, because
    the obvious answer is wrong). They are TWO independent gates, and
    disabling either one alone changes nothing observable:

    1. ``team/policy.py``'s ``_PolicyMiddleware``, which matches the request
       against the compiled outer policy and, for a route whose mode is
       ``admin``, calls ``require_user(..., browser=True, admin=True)``
       BEFORE the handler runs. This is the primary gate.
    2. Each handler's own ``require_user(request, browser=True, admin=True)``
       — defence in depth, and unreachable while gate 1 stands.

    This test goes red only when BOTH are gone, which is the honest thing
    for it to assert: it pins the PROPERTY (non-admins are refused), not
    either mechanism. Do not read a green here as proof that a particular
    ``require_user`` call is still present. When the retry/recover doors were
    removed, ``_remediate``'s own call was dropped by
    accident and this property held throughout, because gate 1 never
    stopped refusing. Ruff's F821 on the now-dangling ``actor`` is what
    caught that, and a defence-in-depth layer going missing silently is
    exactly the thing no assertion here can see.

    A fresh ``TestClient`` is mandatory: ``_app`` claims the server through
    ``claim_server``, whose own client ends up logged in as the owner —
    reusing it would make this probe pass no matter what.
    """
    app = _app(tmp_path)
    routes = (
        ("post", "/api/admin/jobs/1/cancel"),
        ("get", "/api/admin/audit"),
    )

    anonymous = TestClient(app)  # never logged in
    for method, path in routes:
        response = getattr(anonymous, method)(path)
        assert response.status_code == 401, f"{path} answered {response.status_code}"

    member = TestClient(app)
    _login(member, app, "member@example.com")
    for method, path in routes:
        response = getattr(member, method)(path)
        assert response.status_code == 403, f"{path} answered {response.status_code}"

    # Positive control: the same calls really are reachable when authorized,
    # so the refusals above are the gate and not a broken request shape.
    owner = TestClient(app)
    _login(owner, app, "owner@example.com")
    for method, path in routes:
        response = getattr(owner, method)(path)
        assert response.status_code == 200, response.text


def test_create_team_app_fails_fast_when_pull_enabled_without_run_queue_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wave-3 review, Medium "absent run-queue fallback mismatch": with
    FRISKET_RUN_QUEUE_DATABASE_URL unset, the team app used to silently fall
    back to the control database as its run queue, while a bare `frisket
    worker` falls back to a DIFFERENT workspace-local `.queue.db` file --  a
    silent non-topology. An enabled pull surface (FRISKET_ENABLE_MODEL_PULL)
    with no configured run_queue_database_url fails fast at startup instead
    of composing that mismatch silently -- now via the general unconditional
    check (the queue-composition audit, completed), not a pull-flag-specific one."""
    monkeypatch.setenv("FRISKET_ENABLE_MODEL_PULL", "1")
    config = _config(tmp_path, run_queue_database_url=None)
    assert config.run_queue_database_url is None
    with pytest.raises(ValueError, match="FRISKET_RUN_QUEUE_DATABASE_URL"):
        create_team_app(config, send_magic_email=_mail, oidc_exchange=_oidc)


def test_create_team_app_requires_run_queue_database_unconditionally(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deferred follow-up from the queue-composition audit ("general run-queue
    mismatch"), now completed: the direct create_team_app(config)
    constructor used to tolerate an absent run-queue locator whenever the
    pull surface stayed off -- warning loudly but still falling back to the
    control database, while a bare `frisket worker` falls back to a
    DIFFERENT workspace-local queue file. An app whose queue no worker
    drains is a broken topology for every queued job kind, not just model
    pulls, so the constructor now requires the locator unconditionally, the
    same as create_team_app_from_env (the production entrypoint) already
    did."""
    monkeypatch.delenv("FRISKET_ENABLE_MODEL_PULL", raising=False)
    config = _config(tmp_path, run_queue_database_url=None)
    assert config.run_queue_database_url is None
    with pytest.raises(ValueError, match="FRISKET_RUN_QUEUE_DATABASE_URL"):
        create_team_app(config, send_magic_email=_mail, oidc_exchange=_oidc)


def test_create_team_app_warns_when_team_and_worker_database_urls_diverge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Wave-3 review, High "one authoritative control-plane locator":
    FRISKET_TEAM_DATABASE_URL (read into TeamConfig.database_url) and
    FRISKET_DATABASE_URL (read separately by the worker's audit writer) used
    to carry INDEPENDENT literal defaults in docker-compose.yml, so a
    credential rotation that only touched FRISKET_DATABASE_URL silently left
    the team app on the old password. A deliberate split (both set to
    different values) is legal, but must not be SILENT -- create_team_app
    logs a loud warning naming both variables whenever both are set and
    genuinely differ."""
    monkeypatch.setenv(
        "FRISKET_TEAM_DATABASE_URL",
        "postgresql+psycopg://frisket:team-secret@team-db:5432/frisket",
    )
    monkeypatch.setenv(
        "FRISKET_DATABASE_URL",
        "postgresql+psycopg://frisket:worker-secret@worker-db:5432/frisket",
    )
    with caplog.at_level("WARNING", logger="frisket.team.app"):
        create_team_app(_config(tmp_path), send_magic_email=_mail, oidc_exchange=_oidc)
    messages = [record.getMessage() for record in caplog.records]
    assert any(
        "FRISKET_TEAM_DATABASE_URL" in message and "FRISKET_DATABASE_URL" in message
        for message in messages
    ), f"no divergence warning logged: {messages}"
    # Credentials must never be logged, even in the warning.
    assert "team-secret" not in caplog.text
    assert "worker-secret" not in caplog.text


def test_create_team_app_does_not_warn_when_database_urls_match_or_only_one_is_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    same_url = "postgresql+psycopg://frisket:frisket@db:5432/frisket"
    monkeypatch.setenv("FRISKET_TEAM_DATABASE_URL", same_url)
    monkeypatch.setenv("FRISKET_DATABASE_URL", same_url)
    with caplog.at_level("WARNING", logger="frisket.team.app"):
        create_team_app(
            _config(tmp_path / "same"), send_magic_email=_mail, oidc_exchange=_oidc
        )
    # Scoped to the DIVERGENCE warning: the absent-run-queue
    # warning may legitimately fire in this harness (no run-queue URL).
    assert not [
        r.getMessage()
        for r in caplog.records
        if "DIFFERENT databases" in r.getMessage()
    ], caplog.text

    caplog.clear()
    monkeypatch.delenv("FRISKET_DATABASE_URL", raising=False)
    with caplog.at_level("WARNING", logger="frisket.team.app"):
        create_team_app(
            _config(tmp_path / "only-team"),
            send_magic_email=_mail,
            oidc_exchange=_oidc,
        )
    # Scoped to the DIVERGENCE warning: the absent-run-queue
    # warning may legitimately fire in this harness (no run-queue URL).
    assert not [
        r.getMessage()
        for r in caplog.records
        if "DIFFERENT databases" in r.getMessage()
    ], caplog.text


def test_outer_policy_refuses_an_unclassified_route() -> None:
    app = FastAPI()

    @app.get("/api/new")
    def new_route():
        return {}

    with pytest.raises(TeamOuterPolicyError, match="neutral base declaration"):
        install_outer_policy(
            app, auth_declarations={}, require_user=lambda *_args, **_kwargs: {}
        )


def test_concurrent_cold_boot_converges_to_one_org(tmp_path: Path) -> None:
    config = _config(tmp_path)

    def boot():
        return create_team_app(config, send_magic_email=_mail, oidc_exchange=_oidc)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        apps = list(pool.map(lambda _index: boot(), range(2)))
    with apps[0].state.control_engine.connect() as cx:
        assert (
            cx.execute(sa.select(sa.func.count()).select_from(orgs)).scalar_one() == 1
        )


def test_independent_process_boots_converge_to_one_org(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'process.db'}"
    code = (
        "import sys; from pathlib import Path; "
        "from frisket.team.app import TeamConfig, create_team_app; "
        "create_team_app(TeamConfig(database_url=sys.argv[1], data_dir=Path(sys.argv[2]), "
        "base_url='http://127.0.0.1:8000', organization_name='Desk', "
        "admin_emails={'owner@example.com'}, smtp_host='mail.example.test', "
        "smtp_from='frisket@example.test', run_queue_database_url=sys.argv[1]))"
    )
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", code, url, str(tmp_path / f"data-{index}")],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for index in range(2)
    ]
    results = [process.communicate(timeout=30) for process in processes]
    assert [process.returncode for process in processes] == [0, 0], results
    engine = sa.create_engine(url)
    with engine.connect() as cx:
        assert (
            cx.execute(sa.select(sa.func.count()).select_from(orgs)).scalar_one() == 1
        )


def test_marker_version_is_exact_and_project_intent_recovers(tmp_path: Path) -> None:
    app = _app(tmp_path)
    owner = TestClient(app)
    _login(owner, app, "owner@example.com")
    with app.state.control_engine.begin() as cx:
        user_id = cx.execute(
            sa.select(users.c.id).where(users.c.email == "owner@example.com")
        ).scalar_one()
        cx.execute(
            project_creation_intents.insert().values(
                slug="recover-me-123",
                org_id=1,
                creator_user_id=user_id,
                name="Recovered",
            )
        )
    recovered = _app(tmp_path)
    with recovered.state.control_engine.connect() as cx:
        assert (
            cx.execute(
                sa.select(projects.c.slug).where(projects.c.slug == "recover-me-123")
            ).scalar_one()
            == "recover-me-123"
        )
        assert (
            cx.execute(
                sa.select(project_roles.c.role).where(
                    project_roles.c.slug == "recover-me-123",
                    project_roles.c.user_id == user_id,
                )
            ).scalar_one()
            == "owner"
        )
        assert (
            cx.execute(
                sa.select(sa.func.count()).select_from(project_creation_intents)
            ).scalar_one()
            == 0
        )
    with recovered.state.control_engine.begin() as cx:
        cx.execute(schema_marker.update().values(schema_version=999))
    with pytest.raises(PreSplitSchemaError, match="version=2|drop and reseed"):
        _app(tmp_path)


def test_exact_catalog_paths_audits_key_files_and_safe_hints(tmp_path: Path) -> None:
    validated: list[tuple[str, str]] = []

    async def validator(provider: str, secret: str) -> bool:
        validated.append((provider, secret))
        return True

    config = _config(tmp_path)
    app = create_team_app(
        config,
        send_magic_email=_mail,
        oidc_exchange=_oidc,
        validate_provider_key=validator,
    )
    claim_server(app, origin=config.base_url)
    client = TestClient(app)
    _login(client, app, "owner@example.com")
    paths = {
        (method, route.path)
        for route in app.routes
        if hasattr(route, "methods")
        for method in (route.methods or set())
    }
    assert ("PATCH", "/api/admin/users/{user_id}/role") in paths
    assert ("DELETE", "/api/projects/{pid}/members/{email}") in paths
    assert ("POST", "/api/org/keys/validate") in paths
    client.post("/api/org/keys", json={"provider": "openai", "key": "sk-secret-value"})
    assert (
        client.post("/api/org/keys/validate", json={"provider": "openai"}).json()["ok"]
        is True
    )
    assert validated == [("openai", "sk-secret-value")]
    assert (
        client.post("/api/org/env", json={"name": "TINY", "value": "desk"}).json()[
            "hint"
        ]
        == "..."
    )
    with app.state.control_engine.connect() as cx:
        actions = set(cx.execute(sa.select(audit_log.c.action)).scalars())
    assert {"org_key_set", "org_key_validation_requested", "org_env_set"} <= actions
    other_dir = tmp_path / "other-data"
    other_db = tmp_path / "other.db"
    _app(tmp_path / "other", database_url=f"sqlite:///{other_db}", data_dir=other_dir)
    assert (
        config.secrets_key_file.read_bytes()
        != (other_dir.resolve() / "secrets" / "master.key").read_bytes()
    )


def test_concurrent_project_owner_removal_preserves_one_owner(tmp_path: Path) -> None:
    app = _app(tmp_path)
    owner, alice = TestClient(app), TestClient(app)
    _login(owner, app, "owner@example.com")
    _login(alice, app, "alice@example.com")
    pid = owner.post("/api/projects", json={"name": "Owned"}).json()["id"]
    assert (
        owner.post(
            f"/api/projects/{pid}/members",
            json={"email": "alice@example.com", "role": "owner"},
        ).status_code
        == 200
    )

    def remove(client: TestClient, email: str) -> int:
        return client.delete(f"/api/projects/{pid}/members/{email}").status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(remove, owner, "alice@example.com"),
            pool.submit(remove, alice, "owner@example.com"),
        ]
        statuses = [future.result() for future in futures]
    assert statuses.count(200) == 1
    assert set(statuses) <= {200, 403, 409}
    with app.state.control_engine.connect() as cx:
        assert (
            cx.execute(
                sa.select(sa.func.count())
                .select_from(project_roles)
                .where(project_roles.c.slug == pid, project_roles.c.role == "owner")
            ).scalar_one()
            == 1
        )


def test_diagnostics_redact_bearer_jwt_and_dsn_and_cap_body(tmp_path: Path) -> None:
    app = _app(tmp_path)
    client = TestClient(app)
    message = "Authorization: Bearer eyJabc.def.ghi postgresql://user:password@db.example.test/data sk-provider-secret"
    assert (
        client.post("/api/client-errors", json={"message": message}).status_code == 202
    )
    admin = TestClient(app)
    _login(admin, app, "owner@example.com")
    serialized = str(admin.get("/api/admin/errors").json())
    assert "eyJabc" not in serialized
    assert "user:password" not in serialized
    assert "sk-provider" not in serialized
    assert client.post("/api/client-errors", content="x" * 70_000).status_code == 413


def test_production_asgi_entrypoint_requires_run_queue_locator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """the queue-composition audit: the direct
    create_team_app constructor stays warning-only for harness ergonomics,
    but the PRODUCTION entrypoint (create_team_app_from_env, the uvicorn
    --factory target) must fail fast without an explicit run-queue locator
    -- a team app whose queue no worker drains is a broken topology for
    every queued job kind, pull flag or not."""
    from frisket.team.app import create_team_app_from_env

    monkeypatch.delenv("FRISKET_RUN_QUEUE_DATABASE_URL", raising=False)
    monkeypatch.delenv("FRISKET_ENABLE_MODEL_PULL", raising=False)
    monkeypatch.setenv("FRISKET_TEAM_DATABASE_URL", f"sqlite:///{tmp_path}/t.db")
    monkeypatch.setenv("FRISKET_DATA_DIR", str(tmp_path / "data"))
    with pytest.raises(ValueError, match="FRISKET_RUN_QUEUE_DATABASE_URL"):
        create_team_app_from_env()
