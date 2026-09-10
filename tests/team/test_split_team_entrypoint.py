"""Frozen Stage-2 contract for the open, single-organization team server.

This is intentionally an entrypoint test, not a source-layout test.  It boots the
real ASGI composition against a fresh SQLite control plane and real project
directory.  Email delivery and the provider's OIDC code exchange are the only
outbound boundaries replaced by test doubles.

Public composition API frozen here:

* ``frisket.team.app.TeamConfig`` carries deployment settings.
* ``frisket.team.app.create_team_app(config, send_magic_email=...,
  oidc_exchange=...)`` returns the protected FastAPI application.
* OIDC providers are configuration data, keyed by an operator-chosen provider
  id; routes are ``/auth/oidc/{provider}`` and
  ``/auth/oidc/{provider}/callback``.  This deliberately cannot be satisfied by
  renaming the existing Google-only route.

Run only through the manifest command for ``split-2-team-entrypoint-v1``.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import importlib
import json
import os
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
import sqlalchemy as sa
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from frisket.team.bootstrap import PreSplitSchemaError
from frisket.team import db as team_db
from frisket.team.schema import (
    api_tokens,
    audit_log,
    client_errors,
    memberships,
    org_env_vars,
    org_keys,
    orgs,
    project_roles,
    users,
)
from tests.team_setup_helpers import (
    claim_server,
    seed_member_invite,
    sign_in_with_magic_link,
)


pytestmark = pytest.mark.gap


def _team_api() -> tuple[type[Any], Callable[..., Any]]:
    spec = importlib.util.find_spec("frisket.team.app")
    assert spec is not None, (
        "the team application entrypoint is absent: frisket.team.app does not exist"
    )
    module = importlib.import_module("frisket.team.app")
    try:
        return module.TeamConfig, module.create_team_app
    except AttributeError as exc:
        pytest.fail(f"incomplete public team composition API: {exc}", pytrace=False)


def _config(tmp_path: Path, **overrides: Any) -> Any:
    TeamConfig, _ = _team_api()
    values: dict[str, Any] = {
        "database_url": f"sqlite:///{tmp_path / 'control.db'}",
        # create_team_app now requires a run-queue locator unconditionally
        # (deferred follow-up from the queue-composition audit, "general run-queue
        # mismatch", completed).
        "run_queue_database_url": f"sqlite:///{tmp_path / 'run-queue.db'}",
        "data_dir": tmp_path / "data",
        "base_url": "http://testserver",
        "organization_name": "Investigations Desk",
        "admin_emails": {"owner@example.com"},
        "oidc_providers": {
            "newsroom": {
                "issuer": "https://id.example.test",
                "client_id": "frisket-team",
                "client_secret": "test-client-secret",
                "authorization_endpoint": "https://id.example.test/authorize",
                "token_endpoint": "https://id.example.test/token",
                "jwks_uri": "https://id.example.test/.well-known/jwks.json",
                "userinfo_endpoint": "https://id.example.test/userinfo",
                "scopes": ["openid", "email", "profile"],
            }
        },
    }
    values.update(overrides)
    return TeamConfig(**values)


def _build_app(
    tmp_path: Path,
    *,
    oidc_email: str = "oidc@example.com",
    oidc_subject: str = "subject-1",
) -> Any:
    _, create_team_app = _team_api()

    async def send_magic_email(_email: str, _link: str) -> bool:
        return True

    async def oidc_exchange(
        provider: str,
        code: str,
        redirect_uri: str,
        expected_nonce: str = "",
    ) -> dict[str, Any]:
        assert provider == "newsroom"
        assert code == "valid-code"
        assert redirect_uri.endswith("/auth/oidc/newsroom/callback")
        assert expected_nonce, "the browser-bound OIDC nonce must reach the verifier"
        return {
            "email": oidc_email,
            "email_verified": True,
            "subject": oidc_subject,
            "nonce": expected_nonce,
        }

    return create_team_app(
        _config(tmp_path),
        send_magic_email=send_magic_email,
        oidc_exchange=oidc_exchange,
    )


def _session_for_email(client: TestClient, app: Any, email: str) -> str:
    claim_server(app, workspace_name="Investigations Desk")
    if email != "owner@example.com":
        seed_member_invite(app, email)
    sign_in_with_magic_link(app, client, email)
    cookie = client.cookies.get("frisket_session")
    assert cookie
    return cookie


def _routes(app: Any) -> set[tuple[str, str]]:
    return {
        (method, route.path)
        for route in app.routes
        if isinstance(route, APIRoute)
        for method in route.methods or set()
    }


def _oidc_sign_in(client: TestClient) -> Any:
    start = client.get("/auth/oidc/newsroom", follow_redirects=False)
    assert start.status_code == 302, start.text
    query = parse_qs(urlparse(start.headers["location"]).query)
    assert query["client_id"] == ["frisket-team"]
    assert "openid" in query["scope"][0]
    assert query["nonce"][0]
    state = query["state"][0]
    return client.get(
        f"/auth/oidc/newsroom/callback?code=valid-code&state={state}",
        follow_redirects=False,
    )


def test_magic_link_and_generic_oidc_users_join_the_same_single_org(
    tmp_path: Path,
) -> None:
    app = _build_app(tmp_path)
    magic = TestClient(app)
    _session_for_email(magic, app, "owner@example.com")
    magic_me = magic.get("/api/me")
    assert magic_me.status_code == 200, magic_me.text

    oidc = TestClient(app)
    seed_member_invite(app, "oidc@example.com")
    callback = _oidc_sign_in(oidc)
    assert callback.status_code == 302, callback.text
    oidc_me = oidc.get("/api/me")
    assert oidc_me.status_code == 200, oidc_me.text

    with app.state.control_engine.connect() as cx:
        assert (
            cx.execute(sa.select(sa.func.count()).select_from(orgs)).scalar_one() == 1
        )
        joined = cx.execute(
            sa.select(users.c.email, memberships.c.org_id)
            .select_from(users.join(memberships, users.c.id == memberships.c.user_id))
            .order_by(users.c.email)
        ).all()
    assert [row.email for row in joined] == ["oidc@example.com", "owner@example.com"]
    assert len({row.org_id for row in joined}) == 1
    assert magic_me.json()["instance"]["display_name"] == "Investigations Desk"
    assert oidc_me.json()["instance"]["display_name"] == "Investigations Desk"


def test_generic_oidc_rejects_a_new_uninvited_identity(tmp_path: Path) -> None:
    app = _build_app(tmp_path)
    claim_server(app, workspace_name="Investigations Desk")

    callback = _oidc_sign_in(TestClient(app))

    assert callback.status_code == 403
    assert callback.json()["detail"] == "account access is not available"


def test_oidc_identity_is_durably_bound_to_issuer_and_subject_not_email(
    tmp_path: Path,
) -> None:
    first_app = _build_app(
        tmp_path, oidc_email="first@example.com", oidc_subject="durable-subject"
    )
    claim_server(first_app, workspace_name="Investigations Desk")
    seed_member_invite(first_app, "first@example.com")
    first = TestClient(first_app)
    assert _oidc_sign_in(first).status_code == 302
    with first_app.state.control_engine.connect() as cx:
        first_user_id = cx.execute(
            sa.text(
                "SELECT user_id FROM oidc_identities "
                "WHERE issuer=:issuer AND subject=:subject"
            ),
            {"issuer": "https://id.example.test", "subject": "durable-subject"},
        ).scalar_one()

    renamed_app = _build_app(
        tmp_path, oidc_email="renamed@example.com", oidc_subject="durable-subject"
    )
    renamed = TestClient(renamed_app)
    renamed_callback = _oidc_sign_in(renamed)
    assert renamed_callback.status_code == 302, renamed_callback.text
    with renamed_app.state.control_engine.connect() as cx:
        assert (
            cx.execute(
                sa.text(
                    "SELECT user_id FROM oidc_identities "
                    "WHERE issuer=:issuer AND subject=:subject"
                ),
                {
                    "issuer": "https://id.example.test",
                    "subject": "durable-subject",
                },
            ).scalar_one()
            == first_user_id
        )
        assert (
            cx.execute(sa.select(sa.func.count()).select_from(users)).scalar_one() == 2
        )

    impostor_app = _build_app(
        tmp_path, oidc_email="first@example.com", oidc_subject="different-subject"
    )
    impostor = TestClient(impostor_app)
    rejected = _oidc_sign_in(impostor)
    assert rejected.status_code == 409, rejected.text
    with impostor_app.state.control_engine.connect() as cx:
        assert (
            cx.execute(sa.select(sa.func.count()).select_from(users)).scalar_one() == 2
        )
        assert (
            cx.execute(sa.text("SELECT count(*) FROM oidc_identities")).scalar_one()
            == 1
        )


def test_pat_and_project_role_ladder_protect_real_core_routes(tmp_path: Path) -> None:
    app = _build_app(tmp_path)
    owner = TestClient(app)
    member = TestClient(app)
    _session_for_email(owner, app, "owner@example.com")
    _session_for_email(member, app, "member@example.com")

    created = owner.post("/api/projects", json={"name": "Shared Evidence"})
    assert created.status_code == 200, created.text
    project_id = created.json()["id"]
    granted = owner.post(
        f"/api/projects/{project_id}/members",
        json={"email": "member@example.com", "role": "viewer"},
    )
    assert granted.status_code == 200, granted.text

    made = member.post("/api/org/tokens", json={"name": "reader"})
    assert made.status_code == 200, made.text
    token = made.json()["token"]
    member.cookies.clear()
    headers = {"Authorization": f"Bearer {token}"}

    assert member.get(f"/api/projects/{project_id}", headers=headers).status_code == 200
    denied = member.patch(
        f"/api/projects/{project_id}",
        headers=headers,
        json={"name": "Viewer must not edit"},
    )
    assert denied.status_code == 403, denied.text
    assert (
        member.post(
            "/api/org/tokens", headers=headers, json={"name": "nested"}
        ).status_code
        == 403
    )

    with app.state.control_engine.connect() as cx:
        roles = cx.execute(
            sa.select(users.c.email, project_roles.c.role)
            .select_from(
                project_roles.join(users, users.c.id == project_roles.c.user_id)
            )
            .where(project_roles.c.slug == project_id)
            .order_by(users.c.email)
        ).all()
        assert [(row.email, row.role) for row in roles] == [
            ("member@example.com", "viewer"),
            ("owner@example.com", "owner"),
        ]
        assert (
            cx.execute(sa.select(sa.func.count()).select_from(api_tokens)).scalar_one()
            == 1
        )


def test_open_admin_diagnostics_and_org_secret_management_work_over_http(
    tmp_path: Path,
) -> None:
    app = _build_app(tmp_path)
    admin = TestClient(app)
    _session_for_email(admin, app, "owner@example.com")

    key = admin.post(
        "/api/org/keys", json={"provider": "openai", "key": "sk-team-secret"}
    )
    assert key.status_code == 200, key.text
    env = admin.post("/api/org/env", json={"name": "REPORT_FROM", "value": "desk"})
    assert env.status_code == 200, env.text
    listed_keys = admin.get("/api/org/keys")
    assert listed_keys.status_code == 200, listed_keys.text
    assert listed_keys.json() == [{"provider": "openai", "hint": "...cret"}]
    assert admin.get("/api/org/env").status_code == 200

    error = admin.post(
        "/api/client-errors",
        json={"name": "RenderError", "message": "panel failed", "source": "browser"},
    )
    assert error.status_code in {200, 201, 202}, error.text
    for path in (
        "/api/admin/users",
        "/api/admin/jobs",
        "/api/admin/audit",
        "/api/admin/errors",
    ):
        response = admin.get(path)
        assert response.status_code == 200, f"{path}: {response.text}"

    with app.state.control_engine.connect() as cx:
        assert (
            cx.execute(sa.select(sa.func.count()).select_from(org_keys)).scalar_one()
            == 1
        )
        assert (
            cx.execute(
                sa.select(sa.func.count()).select_from(org_env_vars)
            ).scalar_one()
            == 1
        )
        assert (
            cx.execute(
                sa.select(sa.func.count()).select_from(client_errors)
            ).scalar_one()
            == 1
        )
        actions = set(cx.execute(sa.select(audit_log.c.action)).scalars())
    assert "login" in actions


def test_team_init_rejects_an_unstamped_legacy_schema(tmp_path: Path) -> None:
    db = tmp_path / "legacy.db"
    legacy = sa.create_engine(f"sqlite:///{db}")
    with legacy.begin() as cx:
        cx.execute(
            sa.text(
                "CREATE TABLE orgs (id INTEGER PRIMARY KEY, name TEXT, "
                "credits_micro INTEGER, grace_micro INTEGER, quota_micro INTEGER)"
            )
        )

    _, create_team_app = _team_api()
    with pytest.raises(PreSplitSchemaError, match="clean cut|no migration|drop|reseed"):
        create_team_app(_config(tmp_path, database_url=f"sqlite:///{db}"))


def _raw_client_error_request(
    app: Any, *, chunks: list[bytes], content_length: str | None
) -> tuple[int, int]:
    """Drive ASGI receive directly so the test can count body reads.

    TestClient/httpx may coalesce iterables before ASGI sees them.  This helper
    keeps the transport boundary real and proves the cap middleware can reject
    after the first over-limit chunk without asking the JSON parser to consume
    the remainder.
    """

    async def invoke() -> tuple[int, int]:
        pending = list(chunks)
        body_reads = 0
        status = 0

        async def receive() -> dict[str, Any]:
            nonlocal body_reads
            if not pending:
                return {"type": "http.disconnect"}
            body_reads += 1
            body = pending.pop(0)
            return {
                "type": "http.request",
                "body": body,
                "more_body": bool(pending),
            }

        async def send(message: dict[str, Any]) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])

        headers = [(b"content-type", b"application/json")]
        if content_length is None:
            headers.append((b"transfer-encoding", b"chunked"))
        else:
            headers.append((b"content-length", content_length.encode()))
        await app(
            {
                "type": "http",
                "asgi": {"version": "3.0", "spec_version": "2.3"},
                "http_version": "1.1",
                "method": "POST",
                "scheme": "http",
                "path": "/api/client-errors",
                "raw_path": b"/api/client-errors",
                "query_string": b"",
                "root_path": "",
                "headers": headers,
                "client": ("127.0.0.1", 12345),
                "server": ("testserver", 80),
                "state": {},
            },
            receive,
            send,
        )
        return status, body_reads

    return asyncio.run(invoke())


@pytest.mark.parametrize("content_length", [None, "2"])
def test_client_error_cap_counts_actual_chunked_body_before_json_buffering(
    tmp_path: Path, content_length: str | None
) -> None:
    app = _build_app(tmp_path)
    claim_server(app)
    # The first chunk alone crosses the 64 KiB cap.  The second makes the body
    # valid JSON, so a 202/422 proves the request reached buffering/parsing.
    chunks = [b'{"message":"' + (b"x" * 65_536), b'","source":"browser"}']
    status, body_reads = _raw_client_error_request(
        app, chunks=chunks, content_length=content_length
    )
    assert status == 413
    assert body_reads == 1, "over-limit body must be rejected before full buffering"
    with app.state.control_engine.connect() as cx:
        assert (
            cx.execute(
                sa.select(sa.func.count()).select_from(client_errors)
            ).scalar_one()
            == 0
        )


def test_org_key_candidate_validation_matches_ui_contract_and_binds_receipt(
    tmp_path: Path,
) -> None:
    from frisket.server.provider_config import validation_token_is_valid

    validated: list[tuple[str, str]] = []

    async def validator(provider: str, key: str) -> bool:
        validated.append((provider, key))
        return True

    async def mail(_email: str, _link: str) -> bool:
        return True

    _, create_team_app = _team_api()
    app = create_team_app(
        _config(tmp_path),
        send_magic_email=mail,
        oidc_exchange=lambda *_args: {},
        validate_provider_key=validator,
    )
    client = TestClient(app)
    _session_for_email(client, app, "owner@example.com")
    candidate = "sk-unsaved-candidate"
    response = client.post(
        "/api/org/keys/validate", json={"provider": "openai", "key": candidate}
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert set(result) == {
        "provider",
        "ok",
        "reachable",
        "status",
        "detail",
        "validation_token",
    }
    assert result | {"validation_token": "<receipt>"} == {
        "provider": "openai",
        "ok": True,
        "reachable": True,
        "status": 200,
        "detail": None,
        "validation_token": "<receipt>",
    }
    receipt = result["validation_token"]
    assert validation_token_is_valid("openai", candidate, receipt)
    assert not validation_token_is_valid("openai", "sk-other", receipt)

    saved = client.post(
        "/api/org/keys",
        json={
            "provider": "openai",
            "key": candidate,
            "validation_token": receipt,
        },
    )
    assert saved.status_code == 200, saved.text
    stored = client.post("/api/org/keys/validate", json={"provider": "openai"})
    assert stored.status_code == 200, stored.text
    assert stored.json()["ok"] is True
    assert validated == [("openai", candidate), ("openai", candidate)]


class _FakePostgresTransaction:
    def __init__(self) -> None:
        self.committed = False
        self.rolled_back = False

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True


class _FakePostgresConnection:
    def __init__(self) -> None:
        from sqlalchemy.dialects import postgresql

        self.dialect = postgresql.dialect()
        self.transaction = _FakePostgresTransaction()
        self.executed: list[tuple[Any, Any]] = []
        self.closed = False

    def begin(self) -> _FakePostgresTransaction:
        return self.transaction

    def execute(self, statement: Any, params: Any = None) -> Any:
        self.executed.append((statement, params))
        return SimpleNamespace(rowcount=1)

    def close(self) -> None:
        self.closed = True


class _FakePostgresEngine:
    def __init__(self) -> None:
        self.connection = _FakePostgresConnection()

    def connect(self) -> _FakePostgresConnection:
        return self.connection


def test_postgres_lock_is_scoped_and_atomic_upsert_is_dialect_safe() -> None:
    scopes = [("org-membership", 7), ("project-membership", 7, "case-123")]
    lock_params: list[Any] = []
    for scope in scopes:
        engine = _FakePostgresEngine()
        with team_db.locked_transaction(engine, lock_scope=scope):
            pass
        lock_statements = [
            (statement, params)
            for statement, params in engine.connection.executed
            if "pg_advisory_xact_lock" in str(statement)
        ]
        assert len(lock_statements) == 1
        lock_params.append(lock_statements[0][1])
        assert engine.connection.transaction.committed
    assert lock_params[0] != lock_params[1], (
        "lock identity must include org/project scope"
    )

    atomic_upsert = getattr(team_db, "atomic_upsert", None)
    assert callable(atomic_upsert), (
        "Postgres first-insert paths need one atomic upsert seam"
    )
    cx = _FakePostgresConnection()
    atomic_upsert(
        cx,
        table=memberships,
        values={"user_id": 9, "org_id": 7, "role": "member"},
        conflict_columns=("user_id", "org_id"),
        update_values={"role": "member"},
    )
    sql = " ".join(str(cx.executed[-1][0]).upper().split())
    assert "INSERT INTO MEMBERSHIPS" in sql
    assert "ON CONFLICT" in sql and "DO UPDATE" in sql


def test_org_and_project_owner_mutations_request_scoped_database_locks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import frisket.team.admin_browser_service as admin_browser_service
    import frisket.team.app as team_app

    org_scopes: list[Any] = []
    project_scopes: list[Any] = []
    original_org_lock = admin_browser_service.locked_transaction
    original_project_lock = team_app.locked_transaction

    @contextmanager
    def record_org_lock(engine: Any, *, lock_scope: Any = None):
        org_scopes.append(lock_scope)
        with original_org_lock(engine, lock_scope=lock_scope) as cx:
            yield cx

    @contextmanager
    def record_project_lock(engine: Any, *, lock_scope: Any = None):
        project_scopes.append(lock_scope)
        with original_project_lock(engine, lock_scope=lock_scope) as cx:
            yield cx

    monkeypatch.setattr(admin_browser_service, "locked_transaction", record_org_lock)
    monkeypatch.setattr(team_app, "locked_transaction", record_project_lock)
    app = _build_app(tmp_path)
    owner, member = TestClient(app), TestClient(app)
    _session_for_email(owner, app, "owner@example.com")
    _session_for_email(member, app, "member@example.com")
    with app.state.control_engine.connect() as cx:
        member_id = cx.execute(
            sa.select(users.c.id).where(users.c.email == "member@example.com")
        ).scalar_one()
    assert (
        owner.patch(
            f"/api/admin/users/{member_id}/role", json={"role": "owner"}
        ).status_code
        == 200
    )
    assert (
        owner.patch(
            f"/api/admin/browser/users/{member_id}/role",
            json={"org_id": 1, "role": "owner"},
        ).status_code
        == 200
    )
    pid = owner.post("/api/projects", json={"name": "Lock scope"}).json()["id"]
    assert (
        owner.post(
            f"/api/projects/{pid}/members",
            json={"email": "member@example.com", "role": "owner"},
        ).status_code
        == 200
    )
    assert org_scopes == [("org-membership", 1), ("org-membership", 1)]
    assert any(
        scope and "project" in repr(scope).lower() and pid in repr(scope)
        for scope in project_scopes
    )


def test_partial_project_bundle_recovery_is_complete_and_concurrency_safe(
    tmp_path: Path,
) -> None:
    app = _build_app(tmp_path)
    owner = TestClient(app)
    _session_for_email(owner, app, "owner@example.com")
    slug = "partial-recovery-123"
    with app.state.control_engine.begin() as cx:
        user_id = cx.execute(
            sa.select(users.c.id).where(users.c.email == "owner@example.com")
        ).scalar_one()
        cx.execute(
            sa.text(
                "INSERT INTO project_creation_intents "
                "(slug, org_id, creator_user_id, name) "
                "VALUES (:slug, 1, :user_id, 'Recovered')"
            ),
            {"slug": slug, "user_id": user_id},
        )
    partial = app.state.workspace.root / f"{slug}.frisket"
    partial.mkdir(parents=True)
    (partial / "manifest.json").write_text(
        json.dumps(
            {"format": "frisket-bundle", "project_id": slug, "name": "Recovered"}
        )
    )

    def recover() -> Any:
        return _build_app(tmp_path)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(recover) for _ in range(2)]
        recovered = [future.result() for future in futures]
    assert len(recovered) == 2
    assert (partial / "project.db").is_file()
    with recovered[0].state.control_engine.connect() as cx:
        assert (
            cx.execute(
                sa.text("SELECT count(*) FROM projects WHERE slug=:slug"),
                {"slug": slug},
            ).scalar_one()
            == 1
        )
        assert (
            cx.execute(
                sa.text(
                    "SELECT count(*) FROM project_roles WHERE slug=:slug AND role='owner'"
                ),
                {"slug": slug},
            ).scalar_one()
            == 1
        )
        assert (
            cx.execute(
                sa.text(
                    "SELECT count(*) FROM project_creation_intents WHERE slug=:slug"
                ),
                {"slug": slug},
            ).scalar_one()
            == 0
        )
        assert (
            cx.execute(
                sa.text(
                    "SELECT count(*) FROM audit_log WHERE action='project_created' AND detail=:slug"
                ),
                {"slug": slug},
            ).scalar_one()
            == 1
        )


def test_non_loopback_smtp_requires_verified_transport_tls(tmp_path: Path) -> None:
    _, create_team_app = _team_api()
    with pytest.raises(ValueError, match="SMTP|TLS|STARTTLS|SSL"):
        create_team_app(
            _config(
                tmp_path,
                base_url="https://team.example.test",
                oidc_providers={},
                smtp_host="mail.example.test",
                smtp_from="frisket@example.test",
                smtp_starttls=False,
                smtp_ssl=False,
                smtp_username=None,
                smtp_password=None,
            )
        )


def test_oidc_scopes_and_authorization_endpoint_are_structurally_validated(
    tmp_path: Path,
) -> None:
    _, create_team_app = _team_api()
    provider = _config(tmp_path).oidc_providers["newsroom"]
    base = {
        "issuer": provider.issuer,
        "client_id": provider.client_id,
        "client_secret": provider.client_secret,
        "authorization_endpoint": provider.authorization_endpoint,
        "token_endpoint": provider.token_endpoint,
        "jwks_uri": provider.jwks_uri,
    }
    for bad_scopes in ("openid email", ["email", "profile"]):
        with pytest.raises(ValueError, match="scope|openid|sequence"):
            create_team_app(
                _config(
                    tmp_path / repr(bad_scopes),
                    oidc_providers={"newsroom": {**base, "scopes": bad_scopes}},
                ),
                send_magic_email=lambda *_args: True,
                oidc_exchange=lambda *_args: {},
            )

    queried = {
        **base,
        "authorization_endpoint": f"{provider.authorization_endpoint}?prompt=login",
    }
    try:
        app = create_team_app(
            _config(tmp_path / "query", oidc_providers={"newsroom": queried}),
            send_magic_email=lambda *_args: True,
            oidc_exchange=lambda *_args: {},
        )
    except ValueError:
        return
    start = TestClient(app).get("/auth/oidc/newsroom", follow_redirects=False)
    location = start.headers["location"]
    assert location.count("?") == 1
    query = parse_qs(urlparse(location).query)
    assert query["prompt"] == ["login"]
    assert query["client_id"] == ["frisket-team"]
    assert "openid" in query["scope"][0].split()


_CROSS_PROCESS_KEY_SCRIPT = r"""
import json
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient

from frisket.team.app import TeamConfig, create_team_app

mode, database_url, data_dir, candidate, receipt, shared_secret = sys.argv[1:]
links = []

async def mail(_email, link):
    links.append(link)
    return True

async def validator(_provider, _key):
    return True

app = create_team_app(
    TeamConfig(
        database_url=database_url,
        run_queue_database_url=database_url,
        data_dir=Path(data_dir),
        base_url="http://testserver",
        organization_name="Process Desk",
        admin_emails={"owner@example.com"},
        oidc_providers={},
        secrets_master_key=shared_secret,
    ),
    send_magic_email=mail,
    validate_provider_key=validator,
)
client = TestClient(app)
if app.state.setup_claim_token is not None:
    setup = client.post(
        "/setup",
        data={
            "claim_token": app.state.setup_claim_token,
            "workspace_name": "Process Desk",
            "owner_name": "Owner",
            "email": "owner@example.com",
            "password": "a sufficiently long password",
            "password_confirmation": "a sufficiently long password",
        },
        headers={"Origin": "http://testserver"},
        follow_redirects=False,
    )
    assert setup.status_code == 303, setup.text
assert client.post("/auth/request-link", json={"email": "owner@example.com"}).status_code == 200
token = parse_qs(urlparse(links[-1]).query)["token"][0]
assert client.get(f"/auth/callback?token={token}", follow_redirects=False).status_code == 302
if mode == "validate":
    response = client.post(
        "/api/org/keys/validate",
        json={"provider": "openai", "key": candidate},
    )
else:
    response = client.post(
        "/api/org/keys",
        json={"provider": "openai", "key": candidate, "validation_token": receipt},
    )
print(json.dumps({"status_code": response.status_code, "body": response.json()}))
"""


def _last_json_line(output: str) -> dict[str, Any]:
    return json.loads([line for line in output.splitlines() if line.strip()][-1])


def test_candidate_key_receipt_survives_process_boundary(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'process-control.db'}"
    data_dir = tmp_path / "process-data"
    candidate = "arbitrary-shaped-key-Z9_7.private.material"
    shared_secret = "deployment-shared-secret-for-tests"
    env = {**os.environ, "FRISKET_SECRETS_MASTER_KEY": shared_secret}

    issued = subprocess.run(
        [
            sys.executable,
            "-c",
            _CROSS_PROCESS_KEY_SCRIPT,
            "validate",
            database_url,
            str(data_dir),
            candidate,
            "",
            shared_secret,
        ],
        text=True,
        capture_output=True,
        env=env,
        check=True,
    )
    issued_payload = _last_json_line(issued.stdout)
    assert issued_payload["status_code"] == 200
    receipt = issued_payload["body"]["validation_token"]
    assert receipt

    saved = subprocess.run(
        [
            sys.executable,
            "-c",
            _CROSS_PROCESS_KEY_SCRIPT,
            "save",
            database_url,
            str(data_dir),
            candidate,
            receipt,
            shared_secret,
        ],
        text=True,
        capture_output=True,
        env=env,
        check=True,
    )
    saved_payload = _last_json_line(saved.stdout)
    assert saved_payload["status_code"] == 200, saved_payload


def test_candidate_key_validator_exception_is_publicly_opaque(tmp_path: Path) -> None:
    candidate = "arbitrary-shaped-key-Z9_7.private.material"

    marker = "provider-private-exception-marker-4219"

    async def exploding_validator(_provider: str, key: str) -> bool:
        raise RuntimeError(f"upstream rejected {key}: {marker}")

    async def mail(_email: str, _link: str) -> bool:
        return True

    _, create_team_app = _team_api()
    app = create_team_app(
        _config(tmp_path / "opaque"),
        send_magic_email=mail,
        oidc_exchange=lambda *_args: {},
        validate_provider_key=exploding_validator,
    )
    client = TestClient(app)
    _session_for_email(client, app, "owner@example.com")
    failed = client.post(
        "/api/org/keys/validate", json={"provider": "openai", "key": candidate}
    )
    assert failed.status_code == 200
    body = failed.json()
    assert body == {
        "provider": "openai",
        "ok": False,
        "reachable": False,
        "status": None,
        "detail": "provider validation failed",
        "validation_token": None,
    }
    serialized = json.dumps(body)
    assert candidate not in serialized
    assert marker not in serialized
    assert "upstream rejected" not in serialized


def _seed_corrupt_intent(app: Any, *, slug: str, user_id: int) -> Path:
    with app.state.control_engine.begin() as cx:
        cx.execute(
            sa.text(
                "INSERT INTO project_creation_intents "
                "(slug, org_id, creator_user_id, name) "
                "VALUES (:slug, 1, :user_id, 'Recovered')"
            ),
            {"slug": slug, "user_id": user_id},
        )
    bundle = app.state.workspace.root / f"{slug}.frisket"
    bundle.mkdir(parents=True)
    (bundle / "project.db").write_bytes(b"not-a-sqlite-database")
    assert not (bundle / "manifest.json").exists()
    return bundle


def test_corrupt_project_db_is_rebuilt_before_registration(tmp_path: Path) -> None:
    app = _build_app(tmp_path)
    owner = TestClient(app)
    _session_for_email(owner, app, "owner@example.com")
    with app.state.control_engine.connect() as cx:
        user_id = cx.execute(
            sa.select(users.c.id).where(users.c.email == "owner@example.com")
        ).scalar_one()

    slug = "corrupt-recovery-123"
    bundle = _seed_corrupt_intent(app, slug=slug, user_id=user_id)
    recovered = _build_app(tmp_path)
    assert (bundle / "manifest.json").is_file()
    with sqlite3.connect(bundle / "project.db") as project_db:
        assert project_db.execute("PRAGMA quick_check").fetchone() == ("ok",)
        assert project_db.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='meta'"
        ).fetchone() == (1,)
    with recovered.state.control_engine.connect() as cx:
        assert (
            cx.execute(
                sa.text("SELECT count(*) FROM projects WHERE slug=:slug"),
                {"slug": slug},
            ).scalar_one()
            == 1
        )
        assert (
            cx.execute(
                sa.text(
                    "SELECT count(*) FROM project_creation_intents WHERE slug=:slug"
                ),
                {"slug": slug},
            ).scalar_one()
            == 0
        )


def test_failed_corrupt_project_rebuild_keeps_intent_and_skips_registration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _build_app(tmp_path)
    owner = TestClient(app)
    _session_for_email(owner, app, "owner@example.com")
    with app.state.control_engine.connect() as cx:
        user_id = cx.execute(
            sa.select(users.c.id).where(users.c.email == "owner@example.com")
        ).scalar_one()
    failed_slug = "corrupt-recovery-fails-456"
    _seed_corrupt_intent(app, slug=failed_slug, user_id=user_id)
    from frisket.server.workspace import Workspace

    original_create = Workspace.create

    def fail_rebuild(
        self: Any,
        name: str,
        *,
        project_id: str | None = None,
        sensitive: bool = False,
    ) -> Any:
        if project_id == failed_slug:
            raise RuntimeError("simulated rebuild failure")
        return original_create(self, name, project_id=project_id, sensitive=sensitive)

    monkeypatch.setattr(Workspace, "create", fail_rebuild)
    with pytest.raises(RuntimeError, match="simulated rebuild failure"):
        _build_app(tmp_path)
    with app.state.control_engine.connect() as cx:
        assert (
            cx.execute(
                sa.text(
                    "SELECT count(*) FROM project_creation_intents WHERE slug=:slug"
                ),
                {"slug": failed_slug},
            ).scalar_one()
            == 1
        )
        assert (
            cx.execute(
                sa.text("SELECT count(*) FROM projects WHERE slug=:slug"),
                {"slug": failed_slug},
            ).scalar_one()
            == 0
        )


def test_owner_authorization_is_rechecked_inside_scoped_mutation_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import frisket.team.operational_routes as operational_routes

    # Organization-owned mutation: revoke the actor's owner role only after
    # the route's optimistic outer check but inside its scoped lock.
    org_app = _build_app(tmp_path / "org")
    org_owner = TestClient(org_app)
    _session_for_email(org_owner, org_app, "owner@example.com")
    with org_app.state.control_engine.connect() as cx:
        org_owner_id = cx.execute(
            sa.select(users.c.id).where(users.c.email == "owner@example.com")
        ).scalar_one()
    original_org_lock = operational_routes.locked_transaction
    org_revoked = False

    @contextmanager
    def revoke_org_inside_lock(engine: Any, *, lock_scope: Any = None):
        nonlocal org_revoked
        with original_org_lock(engine, lock_scope=lock_scope) as cx:
            if not org_revoked and lock_scope and "org-key" in repr(lock_scope):
                cx.execute(
                    memberships.update()
                    .where(
                        memberships.c.org_id == 1,
                        memberships.c.user_id == org_owner_id,
                    )
                    .values(role="member")
                )
                org_revoked = True
            yield cx

    monkeypatch.setattr(
        operational_routes, "locked_transaction", revoke_org_inside_lock
    )
    denied_org = org_owner.post(
        "/api/org/keys", json={"provider": "openai", "key": "sk-must-not-save"}
    )
    assert denied_org.status_code == 403, denied_org.text
    with org_app.state.control_engine.connect() as cx:
        assert (
            cx.execute(sa.select(sa.func.count()).select_from(org_keys)).scalar_one()
            == 0
        )


@pytest.mark.parametrize(
    ("wire", "operation"),
    [
        ("legacy", "role"),
        ("browser", "role"),
        ("legacy", "remove"),
        ("browser", "remove"),
    ],
)
def test_membership_admin_rechecks_stale_browser_owner_inside_shared_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    wire: str,
    operation: str,
) -> None:
    """Both membership adapters must reject a browser owner revoked at the lock."""
    import frisket.team.admin_browser_service as admin_browser_service

    app = _build_app(tmp_path)
    owner, target = TestClient(app), TestClient(app)
    _session_for_email(owner, app, "owner@example.com")
    _session_for_email(target, app, "target@example.com")
    with app.state.control_engine.connect() as cx:
        ids = dict(cx.execute(sa.select(users.c.email, users.c.id)).all())

    scopes: list[Any] = []
    revoked = False
    original_lock = admin_browser_service.locked_transaction

    @contextmanager
    def revoke_owner_inside_membership_lock(engine: Any, *, lock_scope: Any = None):
        nonlocal revoked
        scopes.append(lock_scope)
        with original_lock(engine, lock_scope=lock_scope) as cx:
            if not revoked and lock_scope == ("org-membership", 1):
                cx.execute(
                    memberships.update()
                    .where(
                        memberships.c.org_id == 1,
                        memberships.c.user_id == ids["owner@example.com"],
                    )
                    .values(role="member")
                )
                revoked = True
            yield cx

    monkeypatch.setattr(
        admin_browser_service, "locked_transaction", revoke_owner_inside_membership_lock
    )
    if wire == "legacy":
        if operation == "role":
            response = owner.patch(
                f"/api/admin/users/{ids['target@example.com']}/role",
                json={"role": "owner"},
            )
        else:
            response = owner.delete(f"/api/admin/users/{ids['target@example.com']}")
    elif operation == "role":
        response = owner.patch(
            f"/api/admin/browser/users/{ids['target@example.com']}/role",
            json={"org_id": 1, "role": "owner"},
        )
    else:
        response = owner.delete(
            f"/api/admin/browser/users/{ids['target@example.com']}",
            params={"org_id": 1},
        )

    assert response.status_code == 403, response.text
    assert scopes == [("org-membership", 1)]
    with app.state.control_engine.connect() as cx:
        assert (
            cx.execute(
                sa.select(memberships.c.role).where(
                    memberships.c.org_id == 1,
                    memberships.c.user_id == ids["target@example.com"],
                )
            ).scalar_one()
            == "member"
        )


def test_project_owner_authorization_is_rechecked_inside_scoped_mutation_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import frisket.team.app as team_app

    # Project-owned mutation: revoke a member-created project's explicit owner
    # row inside the project lock. The target role must not be inserted.
    project_app = _build_app(tmp_path / "project")
    alice, bob = TestClient(project_app), TestClient(project_app)
    _session_for_email(alice, project_app, "alice@example.com")
    _session_for_email(bob, project_app, "bob@example.com")
    pid = alice.post("/api/projects", json={"name": "Linearizable"}).json()["id"]
    with project_app.state.control_engine.connect() as cx:
        ids = dict(cx.execute(sa.select(users.c.email, users.c.id)).all())
    original_project_lock = team_app.locked_transaction
    project_revoked = False

    @contextmanager
    def revoke_project_inside_lock(engine: Any, *, lock_scope: Any = None):
        nonlocal project_revoked
        with original_project_lock(engine, lock_scope=lock_scope) as cx:
            if (
                not project_revoked
                and lock_scope
                and "project-membership" in repr(lock_scope)
            ):
                cx.execute(
                    project_roles.delete().where(
                        project_roles.c.org_id == 1,
                        project_roles.c.slug == pid,
                        project_roles.c.user_id == ids["alice@example.com"],
                    )
                )
                project_revoked = True
            yield cx

    monkeypatch.setattr(team_app, "locked_transaction", revoke_project_inside_lock)
    denied_project = alice.post(
        f"/api/projects/{pid}/members",
        json={"email": "bob@example.com", "role": "viewer"},
    )
    assert denied_project.status_code == 403, denied_project.text
    with project_app.state.control_engine.connect() as cx:
        assert (
            cx.execute(
                sa.select(sa.func.count())
                .select_from(project_roles)
                .where(
                    project_roles.c.slug == pid,
                    project_roles.c.user_id == ids["bob@example.com"],
                )
            ).scalar_one()
            == 0
        )
