"""Route contracts for the remote-administration API (frisket.team.admin_routes
plus the extended /api/admin/users list/remove surfaces): happy paths, auth
failures, validation, invite-link correctness through the real InviteService,
and the reset-link flow."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import sqlalchemy as sa
from fastapi.testclient import TestClient

from frisket.team.app import TeamConfig, create_team_app
from frisket.team.operator_service import mint_operator_token
from frisket.team.schema import (
    audit_log,
    memberships,
    org_keys,
    pending_invites,
    users,
)
from tests.team_setup_helpers import (
    claim_server,
    seed_member_invite,
    sign_in_with_magic_link,
)


def _claimed_app(tmp_path: Path, **overrides: Any) -> tuple[Any, TestClient]:
    values = dict(
        database_url=f"sqlite:///{tmp_path / 'control.db'}",
        run_queue_database_url=f"sqlite:///{tmp_path / 'queue.db'}",
        data_dir=tmp_path / "data",
        base_url="http://testserver",
        organization_name="Ops Desk",
        magic_link_enabled=False,
    )
    values.update(overrides)
    app = create_team_app(TeamConfig(**values))
    owner = TestClient(app)
    claim_server(app, client=owner, workspace_name="Ops Desk")
    return app, owner


def _operator(app) -> dict[str, str]:
    raw, _ = mint_operator_token(app.state.control_engine, org_id=1, label="laptop")
    return {"Authorization": f"Bearer {raw}"}


def _member_client(app, email: str = "member@example.com") -> TestClient:
    """Sign a member in through the public magic-link redemption path."""
    seed_member_invite(app, email)
    client = TestClient(app)
    sign_in_with_magic_link(app, client, email)
    return client


def test_ping_and_rotate_require_the_operator_channel(tmp_path: Path) -> None:
    app, owner = _claimed_app(tmp_path)
    bearer = _operator(app)
    operator_client = TestClient(app)

    assert operator_client.get("/api/admin/ping").status_code == 401
    ping = operator_client.get("/api/admin/ping", headers=bearer)
    assert ping.status_code == 200, ping.text
    payload = ping.json()
    assert payload["org"] == "Ops Desk"
    assert payload["token_label"] == "laptop"
    assert isinstance(payload["server_version"], str) and payload["server_version"]

    # A browser owner session is an admin, but ping/rotate insist on the
    # operator channel itself.
    assert owner.get("/api/admin/ping").status_code == 403
    assert owner.post("/api/admin/token/rotate").status_code == 403

    rotated = operator_client.post("/api/admin/token/rotate", headers=bearer)
    assert rotated.status_code == 200, rotated.text
    assert rotated.json()["label"] == "laptop"
    new_bearer = {"Authorization": f"Bearer {rotated.json()['token']}"}
    assert operator_client.get("/api/admin/ping", headers=bearer).status_code == 401
    assert operator_client.get("/api/admin/ping", headers=new_bearer).status_code == 200


def test_pat_never_reaches_the_admin_api(tmp_path: Path) -> None:
    app, owner = _claimed_app(tmp_path)
    pat = owner.post("/api/org/tokens", json={"name": "probe"}).json()["token"]
    client = TestClient(app)
    response = client.get("/api/admin/ping", headers={"Authorization": f"Bearer {pat}"})
    assert response.status_code == 403


def test_admin_jobs_uses_the_canonical_nonempty_wire(tmp_path: Path) -> None:
    app, _owner = _claimed_app(tmp_path)
    bearer = _operator(app)
    job_id = app.state.workspace.queue.enqueue("legacy.probe", {"secret": "hidden"})

    response = TestClient(app).get("/api/admin/jobs", headers=bearer)

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["schema_version"] == "frisket.admin_jobs.v1"
    assert payload["summary"]["queued"] == 1
    assert payload["jobs"][0]["id"] == job_id
    assert payload["jobs"][0]["schema_version"] == "frisket.admin_job.v1"


def test_users_add_issues_a_working_invite_link_with_the_requested_role(
    tmp_path: Path,
) -> None:
    app, _owner = _claimed_app(tmp_path)
    bearer = _operator(app)
    client = TestClient(app)
    added = client.post(
        "/api/admin/users",
        headers=bearer,
        json={"email": "Lead@Example.com", "role": "owner"},
    )
    assert added.status_code == 200, added.text
    assert added.json()["email"] == "lead@example.com"
    assert added.json()["role"] == "owner"
    assert added.json()["reissued"] is False
    link = added.json()["invite_link"]
    parsed = urlparse(link)
    assert parsed.path == "/auth/callback"

    # A conflicting explicit role on a pending invite is refused, never a
    # silent reset; the stored role survives.
    conflict = client.post(
        "/api/admin/users",
        headers=bearer,
        json={"email": "lead@example.com", "role": "member"},
    )
    assert conflict.status_code == 409
    assert "'owner'" in conflict.json()["detail"]

    # Re-adding without a role re-issues (fresh link, same role, one row).
    again = client.post(
        "/api/admin/users", headers=bearer, json={"email": "lead@example.com"}
    )
    assert again.status_code == 200
    assert again.json()["reissued"] is True
    assert again.json()["role"] == "owner"
    assert again.json()["invite_link"] != link
    parsed = urlparse(again.json()["invite_link"])
    with app.state.control_engine.connect() as cx:
        invite_count = cx.execute(
            sa.select(sa.func.count())
            .select_from(pending_invites)
            .where(pending_invites.c.email == "lead@example.com")
        ).scalar_one()
    assert invite_count == 1

    accepted = TestClient(app)
    callback = accepted.get(f"{parsed.path}?{parsed.query}", follow_redirects=False)
    assert callback.status_code == 302, callback.text
    token = parse_qs(parsed.query)["token"][0]
    completed = accepted.post(
        f"/auth/callback?token={token}",
        json={"password": "a sufficiently long password"},
    )
    assert completed.status_code == 200, completed.text
    with app.state.control_engine.connect() as cx:
        role = cx.execute(
            sa.select(memberships.c.role)
            .select_from(memberships.join(users, users.c.id == memberships.c.user_id))
            .where(users.c.email == "lead@example.com")
        ).scalar_one()
    assert role == "owner"

    # Once the invite is accepted, re-inviting the active member is refused.
    active = client.post(
        "/api/admin/users", headers=bearer, json={"email": "lead@example.com"}
    )
    assert active.status_code == 409
    assert "already an active owner" in active.json()["detail"]


def test_users_add_validation_and_audit_attribution(tmp_path: Path) -> None:
    app, _owner = _claimed_app(tmp_path)
    bearer = _operator(app)
    client = TestClient(app)
    assert (
        client.post(
            "/api/admin/users", headers=bearer, json={"email": "not-an-email"}
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/admin/users",
            headers=bearer,
            json={"email": "x@example.com", "role": "czar"},
        ).status_code
        == 400
    )
    assert (
        client.post("/api/admin/users", json={"email": "x@example.com"}).status_code
        == 401
    )
    client.post("/api/admin/users", headers=bearer, json={"email": "x@example.com"})
    with app.state.control_engine.connect() as cx:
        actor_email = cx.execute(
            sa.select(users.c.email)
            .select_from(audit_log.join(users, users.c.id == audit_log.c.user_id))
            .where(audit_log.c.action == "org_invite_set")
            .order_by(audit_log.c.id.desc())
            .limit(1)
        ).scalar_one()
    assert actor_email == "operator@frisket.invalid"


def test_users_list_uses_the_canonical_user_and_invite_envelope(tmp_path: Path) -> None:
    app, _owner = _claimed_app(tmp_path)
    bearer = _operator(app)
    client = TestClient(app)
    client.post("/api/admin/users", headers=bearer, json={"email": "new@example.com"})
    listed = client.get("/api/admin/users", headers=bearer)
    assert listed.status_code == 200, listed.text
    payload = listed.json()
    assert payload["schema_version"] == "frisket.admin_users.v1"
    org = payload["orgs"][0]
    by_email = {row["email"]: row for row in org["users"]}
    owner_row = by_email["owner@example.com"]
    assert owner_row["role"] == "owner"
    assert set(owner_row) == {"user_id", "email", "name", "role", "created_at"}
    invited_row = next(
        row for row in org["pending_invites"] if row["email"] == "new@example.com"
    )
    assert invited_row["role"] == "member"
    assert set(invited_row) == {"email", "org_id", "role", "expires_at"}
    assert "operator@frisket.invalid" not in by_email


def test_users_remove_covers_pending_invites_members_and_the_spa_id_form(
    tmp_path: Path,
) -> None:
    app, _owner = _claimed_app(tmp_path)
    bearer = _operator(app)
    client = TestClient(app)
    client.post("/api/admin/users", headers=bearer, json={"email": "gone@example.com"})
    removed = client.delete("/api/admin/users/gone@example.com", headers=bearer)
    assert removed.status_code == 200 and removed.json()["removed"] is True
    assert (
        client.delete("/api/admin/users/gone@example.com", headers=bearer).status_code
        == 404
    )

    member = _member_client(app)
    with app.state.control_engine.connect() as cx:
        member_id = cx.execute(
            sa.select(users.c.id).where(users.c.email == "member@example.com")
        ).scalar_one()
    assert member.get("/api/me").status_code == 200
    removed_member = client.delete(
        "/api/admin/users/member@example.com", headers=bearer
    )
    assert removed_member.status_code == 200
    with app.state.control_engine.connect() as cx:
        membership_count = cx.execute(
            sa.select(sa.func.count())
            .select_from(memberships)
            .where(memberships.c.user_id == member_id)
        ).scalar_one()
        user_still_exists = cx.execute(
            sa.select(users.c.id).where(users.c.id == member_id)
        ).scalar_one_or_none()
    assert membership_count == 0
    assert user_still_exists == member_id
    # Deactivation revokes live sessions.
    assert member.get("/api/me").status_code == 401

    # Numeric-id removal (the SPA's wire form) still works.
    another = _member_client(app, email="second@example.com")
    assert another.get("/api/me").status_code == 200
    with app.state.control_engine.connect() as cx:
        second_id = cx.execute(
            sa.select(users.c.id).where(users.c.email == "second@example.com")
        ).scalar_one()
    assert (
        client.delete(f"/api/admin/users/{second_id}", headers=bearer).status_code
        == 200
    )

    assert (
        client.delete("/api/admin/users/owner@example.com", headers=bearer).status_code
        == 409
    )


def test_users_role_by_email_for_members_and_pending_invites(
    tmp_path: Path,
) -> None:
    app, _owner = _claimed_app(tmp_path)
    bearer = _operator(app)
    client = TestClient(app)
    client.post("/api/admin/users", headers=bearer, json={"email": "pend@example.com"})
    changed = client.put(
        "/api/admin/users/pend@example.com/role", headers=bearer, json={"role": "owner"}
    )
    assert changed.status_code == 200
    assert changed.json() == {
        "email": "pend@example.com",
        "role": "owner",
        "status": "invited",
    }
    member = _member_client(app)
    del member
    promoted = client.put(
        "/api/admin/users/member@example.com/role",
        headers=bearer,
        json={"role": "owner"},
    )
    assert promoted.status_code == 200
    assert promoted.json()["status"] == "active"
    assert (
        client.put(
            "/api/admin/users/member@example.com/role",
            headers=bearer,
            json={"role": "czar"},
        ).status_code
        == 400
    )
    assert (
        client.put(
            "/api/admin/users/ghost@example.com/role",
            headers=bearer,
            json={"role": "member"},
        ).status_code
        == 404
    )
    # Demoting the only remaining owner is refused once member@ is demoted back.
    client.put(
        "/api/admin/users/member@example.com/role",
        headers=bearer,
        json={"role": "member"},
    )
    assert (
        client.put(
            "/api/admin/users/owner@example.com/role",
            headers=bearer,
            json={"role": "member"},
        ).status_code
        == 409
    )


def test_email_role_update_rechecks_stale_browser_owner_inside_membership_lock(
    tmp_path: Path, monkeypatch
) -> None:
    import frisket.team.admin_routes as admin_routes

    app, owner = _claimed_app(tmp_path)
    _member_client(app, "target@example.com")
    with app.state.control_engine.connect() as cx:
        ids = dict(cx.execute(sa.select(users.c.email, users.c.id)).all())

    scopes: list[Any] = []
    revoked = False
    original_lock = admin_routes.locked_transaction

    @contextmanager
    def revoke_owner_inside_membership_lock(engine, *, lock_scope=None):
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
        admin_routes, "locked_transaction", revoke_owner_inside_membership_lock
    )
    denied = owner.put(
        "/api/admin/users/target@example.com/role", json={"role": "owner"}
    )

    assert denied.status_code == 403, denied.text
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


def test_reset_flow_issues_a_single_use_link_and_sets_the_password(
    tmp_path: Path,
) -> None:
    app, _owner = _claimed_app(tmp_path)
    bearer = _operator(app)
    client = TestClient(app)
    assert (
        client.post(
            "/api/admin/users/ghost@example.com/reset", headers=bearer
        ).status_code
        == 404
    )
    reset = client.post("/api/admin/users/owner@example.com/reset", headers=bearer)
    assert reset.status_code == 200, reset.text
    link = reset.json()["reset_link"]
    path = urlparse(link).path
    assert path.startswith("/auth/reset/")
    page = client.get(path)
    assert page.status_code == 200
    assert "new password" in page.text.lower()
    mismatch = client.post(
        path,
        data={
            "password": "a sufficiently long pw",
            "password_confirmation": "different confirmation",
        },
    )
    assert mismatch.status_code == 400
    done = client.post(
        path,
        data={
            "password": "a brand new long password",
            "password_confirmation": "a brand new long password",
        },
        follow_redirects=False,
    )
    assert done.status_code == 303
    assert client.get(path).status_code == 404
    login = client.post(
        "/auth/password-login",
        json={"email": "owner@example.com", "password": "a brand new long password"},
        headers={"Origin": "http://testserver"},
        follow_redirects=False,
    )
    assert login.status_code == 200


def test_admin_secrets_bridge_and_its_denials(tmp_path: Path) -> None:
    app, owner = _claimed_app(tmp_path)
    bearer = _operator(app)
    member = _member_client(app)
    client = TestClient(app)

    listed = client.get("/api/admin/secrets", headers=bearer)
    assert listed.status_code == 200
    providers = {row["provider"]: row for row in listed.json()["providers"]}
    assert set(providers) == {"anthropic", "openai", "gemini", "openrouter"}
    assert providers["anthropic"]["env_var"] == "ANTHROPIC_API_KEY"
    assert not providers["anthropic"]["configured"]

    stored = client.put(
        "/api/admin/secrets/anthropic",
        headers=bearer,
        json={"key": "sk-ant-test-1234"},
    )
    assert stored.status_code == 200
    assert stored.json()["hint"] == "...1234"
    with app.state.control_engine.connect() as cx:
        encrypted = cx.execute(
            sa.select(org_keys.c.encrypted).where(org_keys.c.provider == "anthropic")
        ).scalar_one()
    assert "sk-ant-test-1234" not in encrypted

    # A browser owner session may use the same admin bridge; a member and a
    # bare request may not.
    assert owner.get("/api/admin/secrets").status_code == 200
    assert member.get("/api/admin/secrets").status_code == 403
    assert client.get("/api/admin/secrets").status_code == 401
    assert (
        client.put(
            "/api/admin/secrets/unknown", headers=bearer, json={"key": "x"}
        ).status_code
        == 400
    )
    assert (
        client.put(
            "/api/admin/secrets/openai", headers=bearer, json={"key": "  "}
        ).status_code
        == 400
    )
    removed = client.delete("/api/admin/secrets/anthropic", headers=bearer)
    assert removed.json() == {"deleted": True}
    assert client.delete("/api/admin/secrets/anthropic", headers=bearer).json() == {
        "deleted": False
    }


def test_media_proxy_setting_round_trips_over_the_admin_bridge(
    tmp_path: Path, monkeypatch
) -> None:
    app, owner = _claimed_app(tmp_path)
    bearer = _operator(app)
    client = TestClient(app)
    member = _member_client(app)
    # Pin the host-environment probe so the payload is deterministic whether
    # or not this test itself runs inside a container.
    from frisket.team import admin_routes

    monkeypatch.setattr(
        admin_routes.media_proxy_config, "container_gateway", lambda: None
    )

    # Unset state reads clean and is auth-gated like the secrets bridge.
    empty = client.get("/api/admin/media-proxy", headers=bearer)
    assert empty.status_code == 200
    assert empty.json() == {
        "configured": False,
        "url": None,
        "source": None,
        "env_invalid": None,
        "container_gateway": None,
    }
    assert client.get("/api/admin/media-proxy").status_code == 401
    assert member.get("/api/admin/media-proxy").status_code == 403

    stored = client.put(
        "/api/admin/media-proxy",
        headers=bearer,
        json={"url": "socks5://127.0.0.1:1080"},
    )
    assert stored.status_code == 200, stored.text
    payload = stored.json()
    assert payload["configured"] is True
    assert payload["url"] == "socks5://127.0.0.1:1080"
    assert payload["source"] == "admin"
    assert payload["warnings"] == []

    read_back = client.get("/api/admin/media-proxy", headers=bearer).json()
    assert read_back["url"] == "socks5://127.0.0.1:1080"

    # The browser owner session may use the same bridge.
    assert owner.get("/api/admin/media-proxy").status_code == 200

    with app.state.control_engine.connect() as cx:
        actions = [
            row.action for row in cx.execute(sa.select(audit_log.c.action)).all()
        ]
    assert "media_proxy_set" in actions

    cleared = client.delete("/api/admin/media-proxy", headers=bearer)
    assert cleared.status_code == 200
    assert cleared.json()["configured"] is False
    assert cleared.json()["cleared"] is True
    again = client.delete("/api/admin/media-proxy", headers=bearer)
    assert again.json()["cleared"] is False


def test_media_proxy_validation_refuses_junk_and_warns_on_non_loopback(
    tmp_path: Path,
) -> None:
    app, _owner = _claimed_app(tmp_path)
    bearer = _operator(app)
    client = TestClient(app)

    for url in ("https://127.0.0.1:1080", "socks5://user:pw@127.0.0.1:1080", ""):
        response = client.put(
            "/api/admin/media-proxy", headers=bearer, json={"url": url}
        )
        assert response.status_code == 400, url

    warned = client.put(
        "/api/admin/media-proxy",
        headers=bearer,
        json={"url": "socks5://10.1.2.3:1080"},
    )
    assert warned.status_code == 200
    assert any("not loopback" in warning for warning in warned.json()["warnings"])


def test_media_proxy_payload_carries_the_container_gateway(
    tmp_path: Path, monkeypatch
) -> None:
    app, _owner = _claimed_app(tmp_path)
    bearer = _operator(app)
    client = TestClient(app)
    from frisket.team import admin_routes

    monkeypatch.setattr(
        admin_routes.media_proxy_config, "container_gateway", lambda: "172.18.0.1"
    )
    payload = client.get("/api/admin/media-proxy", headers=bearer).json()
    assert payload["container_gateway"] == "172.18.0.1"


def test_media_proxy_invalid_env_value_is_surfaced_not_hidden(
    tmp_path: Path, monkeypatch
) -> None:
    app, _owner = _claimed_app(tmp_path)
    bearer = _operator(app)
    client = TestClient(app)
    monkeypatch.setenv("FRISKET_MEDIA_PROXY", "socks4://x:1")
    payload = client.get("/api/admin/media-proxy", headers=bearer).json()
    assert payload["configured"] is False
    assert payload["url"] is None
    assert "scheme" in payload["env_invalid"]


def test_media_proxy_check_probes_through_the_configured_proxy(
    tmp_path: Path, monkeypatch
) -> None:
    from frisket.ops.media_proxy import MediaProxyProbeResult
    from frisket.team import admin_routes

    app, _owner = _claimed_app(tmp_path)
    bearer = _operator(app)
    client = TestClient(app)

    unset = client.post("/api/admin/media-proxy/check", headers=bearer)
    assert unset.status_code == 200
    assert unset.json() == {
        "configured": False,
        "ok": False,
        "error": "no media proxy is set",
    }

    client.put(
        "/api/admin/media-proxy",
        headers=bearer,
        json={"url": "socks5://127.0.0.1:1080"},
    )
    probed: dict[str, str] = {}

    def fake_probe(proxy_url: str, **_kwargs):
        probed["url"] = proxy_url
        return MediaProxyProbeResult(
            ok=True,
            probe_url="https://www.youtube.com/generate_204",
            status_code=204,
            elapsed_ms=41,
        )

    monkeypatch.setattr(
        admin_routes.media_proxy_config, "probe_media_proxy", fake_probe
    )
    checked = client.post("/api/admin/media-proxy/check", headers=bearer)
    assert checked.status_code == 200, checked.text
    body = checked.json()
    assert body["ok"] is True
    assert body["status_code"] == 204
    assert body["configured"] is True
    assert probed["url"] == "socks5://127.0.0.1:1080"


def test_org_media_proxy_status_is_member_readable_and_boolean_only(
    tmp_path: Path, monkeypatch
) -> None:
    from frisket.ops import media_proxy as media_proxy_config
    from frisket.ops.media_proxy import MediaProxyProbeResult

    app, owner = _claimed_app(tmp_path)
    bearer = _operator(app)
    client = TestClient(app)
    member = _member_client(app)

    # Anonymous requests are refused; members read booleans only.
    assert client.get("/api/org/media-proxy/status").status_code == 401

    unset = member.get("/api/org/media-proxy/status")
    assert unset.status_code == 200
    assert unset.json() == {
        "configured": False,
        "connected": None,
        "can_configure": False,
    }

    client.put(
        "/api/admin/media-proxy",
        headers=bearer,
        json={"url": "socks5://127.0.0.1:1080"},
    )
    monkeypatch.setattr(
        media_proxy_config,
        "probe_media_proxy",
        lambda url, **_kwargs: MediaProxyProbeResult(
            ok=True, probe_url="probe", status_code=204
        ),
    )
    media_proxy_config._probe_cache.clear()

    connected = member.get("/api/org/media-proxy/status")
    assert connected.status_code == 200
    body = connected.json()
    assert body == {"configured": True, "connected": True, "can_configure": False}
    assert "url" not in body
    assert "socks5" not in connected.text

    as_owner = owner.get("/api/org/media-proxy/status").json()
    assert as_owner["can_configure"] is True
