"""Behavior contracts for the generated browser-admin HTTP surface."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

import sqlalchemy as sa
from fastapi.testclient import TestClient

from frisket.contracts.http.admin_browser import AdminBrowserJob, AdminBrowserUsersV1
from frisket.team.app import TeamConfig, create_team_app
from frisket.team.operator_service import mint_operator_token
from frisket.team.schema import audit_log, client_errors, memberships, users
from tests.team_setup_helpers import (
    claim_server,
    seed_member_invite,
    sign_in_with_magic_link,
)


async def _mail(_email: str, _link: str) -> bool:
    return True


def _claimed_app(
    tmp_path: Path,
    *,
    send_mail: Callable[[str, str], Awaitable[bool]] = _mail,
    **overrides: Any,
) -> tuple[Any, TestClient]:
    values = dict(
        database_url=f"sqlite:///{tmp_path / 'control.db'}",
        run_queue_database_url=f"sqlite:///{tmp_path / 'queue.db'}",
        data_dir=tmp_path / "data",
        base_url="http://testserver",
        organization_name="Ops Desk",
        magic_link_enabled=False,
    )
    values.update(overrides)
    app = create_team_app(TeamConfig(**values), send_magic_email=send_mail)
    owner = TestClient(app)
    claim_server(app, client=owner, workspace_name="Ops Desk")
    return app, owner


def _member_client(app: Any, email: str = "member@example.com") -> TestClient:
    seed_member_invite(app, email)
    client = TestClient(app)
    sign_in_with_magic_link(app, client, email)
    return client


def test_browser_admin_health_is_a_versioned_runtime_contract(tmp_path: Path) -> None:
    app, owner = _claimed_app(tmp_path)

    response = owner.get("/api/admin/browser/health")

    assert response.status_code == 200, response.text
    assert response.json() == {
        "schema_version": "frisket.admin_health.v1",
        "ok": True,
        "db": {
            "ok": True,
            "dialect": "sqlite",
            "latency_ms": response.json()["db"]["latency_ms"],
            "error": None,
        },
        "blob_store": {"ok": True, "error": None},
        "active_runs": 0,
        "queue": {
            "ok": True,
            "counts": {
                "queued": 0,
                "running": 0,
                "done": 0,
                "failed": 0,
                "cancelled": 0,
            },
            "error": None,
        },
    }
    assert isinstance(response.json()["db"]["latency_ms"], int)
    assert app.openapi()["paths"]["/api/admin/browser/health"]["get"]


def test_browser_admin_users_and_all_mutations_use_the_canonical_wires(
    tmp_path: Path,
) -> None:
    sent: list[tuple[str, str]] = []

    async def mail(email: str, link: str) -> bool:
        sent.append((email, link))
        return True

    app, owner = _claimed_app(tmp_path, send_mail=mail)
    org_id = app.state.team_org_id

    listed = owner.get("/api/admin/browser/users")
    assert listed.status_code == 200, listed.text
    payload = listed.json()
    assert payload["schema_version"] == "frisket.admin_users.v1"
    assert payload["capabilities"] == {
        "assignable_roles": ["owner", "member"],
        "invite_ttl_days": 7,
        "magic_link_ttl_minutes": 30,
    }
    assert payload["orgs"][0]["id"] == org_id
    assert payload["orgs"][0]["suspended"] is False
    assert payload["orgs"][0]["users"][0]["email"] == "owner@example.com"
    owner_id = payload["orgs"][0]["users"][0]["user_id"]
    hosted_payload = {
        **payload,
        "orgs": [
            {
                **payload["orgs"][0],
                "users": [
                    {
                        **payload["orgs"][0]["users"][0],
                        "role": "admin",
                    }
                ],
            }
        ],
    }
    assert (
        AdminBrowserUsersV1.model_validate(hosted_payload).orgs[0].users[0].role
        == "admin"
    )
    schemas = app.openapi()["components"]["schemas"]
    for model_name in (
        "AdminBrowserUser",
        "AdminBrowserUpdateRoleRequest",
        "AdminBrowserUpdateRoleResult",
    ):
        assert schemas[model_name]["properties"]["role"]["enum"] == [
            "owner",
            "admin",
            "member",
        ]
    assert schemas["AdminBrowserUserCapabilities"]["properties"]["assignable_roles"][
        "items"
    ]["enum"] == ["owner", "admin", "member"]
    assert (
        owner.patch(
            f"/api/admin/browser/users/{owner_id}/role",
            json={"org_id": org_id, "role": "member"},
        ).status_code
        == 409
    )
    assert (
        owner.delete(
            f"/api/admin/browser/users/{owner_id}", params={"org_id": org_id}
        ).status_code
        == 409
    )
    assert (
        owner.post(
            "/api/admin/browser/users/invite",
            json={"org_id": org_id + 1, "email": "wrong-org@example.com"},
        ).status_code
        == 404
    )

    invited = owner.post(
        "/api/admin/browser/users/invite",
        json={"org_id": org_id, "email": "New@Example.com"},
    )
    assert invited.status_code == 200, invited.text
    assert invited.json()["ok"] is True
    assert invited.json()["sent"] is True
    assert invited.json()["org_id"] == org_id
    assert invited.json()["email"] == "new@example.com"
    assert invited.json()["role"] == "member"
    assert invited.json()["expires_at"] is not None
    assert sent and sent[-1][0] == "new@example.com"
    pending = owner.get("/api/admin/browser/users").json()["orgs"][0]["pending_invites"]
    assert (
        next(row for row in pending if row["email"] == "new@example.com")["role"]
        == "member"
    )

    member = _member_client(app, "member@example.com")
    with app.state.control_engine.connect() as cx:
        member_id = int(
            cx.execute(
                sa.select(users.c.id).where(users.c.email == "member@example.com")
            ).scalar_one()
        )
    refused = owner.patch(
        f"/api/admin/browser/users/{member_id}/role",
        json={"org_id": org_id, "role": "admin"},
    )
    assert refused.status_code == 400, refused.text
    with app.state.control_engine.connect() as cx:
        assert (
            cx.execute(
                sa.select(memberships.c.role).where(
                    memberships.c.org_id == org_id,
                    memberships.c.user_id == member_id,
                )
            ).scalar_one()
            == "member"
        )
    promoted = owner.patch(
        f"/api/admin/browser/users/{member_id}/role",
        json={"org_id": org_id, "role": "owner"},
    )
    assert promoted.status_code == 200, promoted.text
    assert promoted.json() == {
        "ok": True,
        "org_id": org_id,
        "user_id": member_id,
        "role": "owner",
        "self": False,
    }

    removed = owner.delete(
        f"/api/admin/browser/users/{member_id}", params={"org_id": org_id}
    )
    assert removed.status_code == 200, removed.text
    assert removed.json() == {
        "ok": True,
        "org_id": org_id,
        "user_id": member_id,
        "email": None,
        "membership_removed": True,
        "invite_revoked": False,
        "self": False,
    }
    assert member.get("/api/me").status_code == 401

    revoked = owner.delete(
        "/api/admin/browser/users/invites/new@example.com",
        params={"org_id": org_id},
    )
    assert revoked.status_code == 200, revoked.text
    assert revoked.json() == {
        "ok": True,
        "org_id": org_id,
        "email": "new@example.com",
        "revoked": True,
    }


def test_browser_admin_jobs_are_nonempty_and_cancel_is_truthful(tmp_path: Path) -> None:
    app, owner = _claimed_app(tmp_path)
    queue = app.state.workspace.queue
    completed_id = queue.enqueue("source.poll", {"project_id": "alpha"})
    claimed = queue.claim("browser-contract")
    assert claimed is not None and claimed.id == completed_id
    assert queue.complete(
        completed_id,
        "browser-contract",
        {"skipped": True, "reason": None},
    )
    job_id = queue.enqueue("probe", {"project_id": "alpha", "reason": None})

    listed = owner.get("/api/admin/browser/jobs")
    assert listed.status_code == 200, listed.text
    payload = listed.json()
    assert payload["schema_version"] == "frisket.admin_jobs.v1"
    assert payload["summary"]["queued"] == 1
    assert payload["summary"]["done"] == 1
    assert payload["summary"]["stalled"] == 0
    assert payload["summary"]["no_live_worker"] is True
    assert payload["workers"] == {"live": 0, "liveness_window_seconds": 60}
    by_id = {job["id"]: job for job in payload["jobs"]}
    assert by_id[job_id]["refs"]["project_id"] == "alpha"
    AdminBrowserJob.model_validate(
        {**by_id[completed_id], "result_summary": {"reason": None}}
    )

    cancelled = owner.post(f"/api/admin/browser/jobs/{job_id}/cancel")
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json() == {
        "ok": True,
        "outcome": "cancelled",
        "job_id": job_id,
        "retryable": False,
        "detail": None,
    }

    terminal = owner.post(f"/api/admin/browser/jobs/{job_id}/cancel")
    assert terminal.status_code == 409, terminal.text
    assert terminal.json() == {
        "ok": False,
        "outcome": "already_terminal",
        "job_id": job_id,
        "retryable": False,
        "detail": "job is already cancelled",
    }

    missing = owner.post("/api/admin/browser/jobs/999999/cancel")
    assert missing.status_code == 404, missing.text
    assert missing.json() == {
        "ok": False,
        "outcome": "conflict",
        "job_id": 999999,
        "retryable": False,
        "detail": "job not found",
    }


def test_browser_admin_audit_combines_filters_and_errors_honor_limit(
    tmp_path: Path,
) -> None:
    app, owner = _claimed_app(tmp_path)
    org_id = app.state.team_org_id
    project_id = owner.post("/api/projects", json={"name": "Alpha"}).json()["id"]
    with app.state.control_engine.begin() as cx:
        owner_id = int(
            cx.execute(
                sa.select(users.c.id).where(users.c.email == "owner@example.com")
            ).scalar_one()
        )
        cx.execute(
            audit_log.insert(),
            [
                {
                    "user_id": owner_id,
                    "org_id": org_id,
                    "action": "org_key_set",
                    "detail": "openai",
                    "created_at": datetime.now(UTC),
                },
            ],
        )
        cx.execute(
            audit_log.insert(),
            [
                {
                    "user_id": owner_id,
                    "org_id": org_id,
                    "action": "org_env_set",
                    "detail": f"RECENT_{index}",
                    "created_at": datetime.now(UTC),
                }
                for index in range(1001)
            ],
        )
        cx.execute(
            client_errors.insert(),
            [
                {
                    "org_id": org_id,
                    "user_id": owner_id,
                    "source": "browser",
                    "severity": "error",
                    "name": f"Error {index}",
                    "message": f"failure {index}",
                    "project_id": project_id,
                    "context_json": '{"nullable":null}',
                    "created_at": datetime.now(UTC),
                }
                for index in range(3)
            ],
        )

    unfiltered = owner.get("/api/admin/browser/audit")
    assert unfiltered.status_code == 200, unfiltered.text
    assert unfiltered.json()["schema_version"] == "frisket.admin_audit.v1"
    assert set(unfiltered.json()["filters"]["actions"]) >= {
        "project_created",
        "org_key_set",
    }

    audit = owner.get(
        "/api/admin/browser/audit",
        params={
            "org_id": org_id,
            "project_id": project_id,
            "user": "OWNER@EXAMPLE.COM",
            "action": "PROJECT_CREATED",
            "limit": 1,
        },
    )
    assert audit.status_code == 200, audit.text
    audit_payload = audit.json()
    assert len(audit_payload["events"]) == 1
    event = audit_payload["events"][0]
    assert (event["project_id"], event["actor_email"], event["action"]) == (
        project_id,
        "owner@example.com",
        "project_created",
    )
    assert audit_payload["filters"]["orgs"] == [{"id": org_id, "name": "Ops Desk"}]
    assert {item["project_id"] for item in audit_payload["filters"]["projects"]} == {
        project_id
    }
    assert {"project_created", "org_key_set"} <= set(
        audit_payload["filters"]["actions"]
    )

    errors = owner.get("/api/admin/browser/errors", params={"limit": 2})
    assert errors.status_code == 200, errors.text
    error_payload = errors.json()
    assert error_payload["schema_version"] == "frisket.admin_errors.v1"
    assert len(error_payload["errors"]) == 2
    assert "retention" not in error_payload
    assert error_payload["errors"][0]["context"] == {"nullable": None}
    assert error_payload["errors"][0]["project_id"] == project_id

    assert (
        owner.get("/api/admin/browser/errors", params={"limit": 0}).status_code == 422
    )
    assert (
        owner.get("/api/admin/browser/errors", params={"limit": 501}).status_code == 422
    )


def test_browser_admin_rejects_anonymous_member_and_pat_on_read_and_mutation(
    tmp_path: Path,
) -> None:
    app, owner = _claimed_app(tmp_path)
    org_id = app.state.team_org_id
    anonymous = TestClient(app)
    member = _member_client(app)
    pat = owner.post("/api/org/tokens", json={"name": "browser-admin-probe"}).json()[
        "token"
    ]
    pat_client = TestClient(app)
    pat_headers = {"Authorization": f"Bearer {pat}"}

    for client, expected, headers in (
        (anonymous, 401, {}),
        (member, 403, {}),
        (pat_client, 403, pat_headers),
    ):
        assert (
            client.get("/api/admin/browser/health", headers=headers).status_code
            == expected
        )
        assert (
            client.post(
                "/api/admin/browser/users/invite",
                headers=headers,
                json={"org_id": org_id, "email": "blocked@example.com"},
            ).status_code
            == expected
        )

    operator_token, _label = mint_operator_token(
        app.state.control_engine, org_id=org_id, label="browser-v1"
    )
    operator_headers = {"Authorization": f"Bearer {operator_token}"}
    assert (
        TestClient(app)
        .get("/api/admin/browser/health", headers=operator_headers)
        .status_code
        == 200
    )
    operator_invite = TestClient(app).post(
        "/api/admin/browser/users/invite",
        headers=operator_headers,
        json={"org_id": org_id, "email": "operator-invited@example.com"},
    )
    assert operator_invite.status_code == 200, operator_invite.text

    with app.state.control_engine.connect() as cx:
        assert (
            cx.execute(
                sa.select(sa.func.count())
                .select_from(memberships)
                .where(memberships.c.org_id == org_id)
            ).scalar_one()
            == 2
        )


def test_operator_token_can_create_and_revoke_invites_on_both_admin_wires(
    tmp_path: Path,
) -> None:
    app, _owner = _claimed_app(tmp_path)
    org_id = app.state.team_org_id
    operator_token, _label = mint_operator_token(
        app.state.control_engine, org_id=org_id, label="invite-wires"
    )
    headers = {"Authorization": f"Bearer {operator_token}"}

    for wire, email in (
        ("legacy", "operator-legacy@example.com"),
        ("browser", "operator-browser@example.com"),
    ):
        if wire == "legacy":
            created = TestClient(app).post(
                "/api/admin/users/invite", headers=headers, json={"email": email}
            )
            revoked = TestClient(app).delete(
                f"/api/admin/users/invites/{email}", headers=headers
            )
        else:
            created = TestClient(app).post(
                "/api/admin/browser/users/invite",
                headers=headers,
                json={"org_id": org_id, "email": email},
            )
            revoked = TestClient(app).delete(
                f"/api/admin/browser/users/invites/{email}",
                headers=headers,
                params={"org_id": org_id},
            )
        assert created.status_code == 200, created.text
        assert revoked.status_code == 200, revoked.text

    with app.state.control_engine.connect() as cx:
        assert (
            cx.execute(sa.select(sa.func.count()).select_from(memberships)).scalar_one()
            == 1
        )
