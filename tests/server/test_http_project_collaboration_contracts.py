"""HTTP-06-F16A: Team project-collaboration boundary contracts."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import sqlalchemy as sa
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from frisket.contracts.http.models import HttpError
from frisket.contracts.http.project_collaboration import (
    CreateProjectInviteRequest,
    ProjectInviteCreate,
    ProjectInviteList,
    ProjectInviteRevoke,
    ProjectMemberList,
    ProjectMemberRemove,
    ProjectMemberSet,
    SetProjectMemberRequest,
)
from frisket.team.app import TeamConfig, create_team_app
from frisket.team.schema import audit_log
from tests.team_setup_helpers import (
    claim_server,
    seed_member_invite,
    sign_in_with_magic_link,
)


ROUTES = {
    "list_members": (
        "/api/projects/{pid}/members",
        "GET",
        ProjectMemberList,
        None,
        frozenset({401, 403, 500}),
    ),
    "set_member": (
        "/api/projects/{pid}/members",
        "POST",
        ProjectMemberSet,
        SetProjectMemberRequest,
        frozenset({401, 403, 404, 409, 422, 500}),
    ),
    "remove_member": (
        "/api/projects/{pid}/members/{email}",
        "DELETE",
        ProjectMemberRemove,
        None,
        frozenset({401, 403, 404, 409, 500}),
    ),
    "list_project_invites": (
        "/api/projects/{pid}/invites",
        "GET",
        ProjectInviteList,
        None,
        frozenset({401, 403, 404, 500}),
    ),
    "create_project_invite": (
        "/api/projects/{pid}/invites",
        "POST",
        ProjectInviteCreate,
        CreateProjectInviteRequest,
        frozenset({400, 401, 403, 404, 422, 500}),
    ),
    "revoke_project_invite": (
        "/api/projects/{pid}/invites/{invite_id}",
        "DELETE",
        ProjectInviteRevoke,
        None,
        frozenset({401, 403, 404, 422, 500}),
    ),
}


def _config(tmp_path: Path) -> TeamConfig:
    return TeamConfig(
        database_url=f"sqlite:///{tmp_path / 'control.db'}",
        run_queue_database_url=f"sqlite:///{tmp_path / 'queue.db'}",
        data_dir=tmp_path / "data",
        base_url="http://testserver",
        organization_name="Collaboration contracts",
        admin_emails={"owner@example.com"},
    )


def _login(client: TestClient, app: Any, email: str) -> None:
    sign_in_with_magic_link(app, client, email)


def _route_map(app: Any) -> dict[str, APIRoute]:
    return {
        route.name: route
        for route in app.routes
        if isinstance(route, APIRoute) and route.name in ROUTES
    }


def test_six_collaboration_routes_own_closed_contracts_and_truthful_errors(
    tmp_path: Path,
) -> None:
    app = create_team_app(_config(tmp_path), product_telemetry_destination=None)
    routes = _route_map(app)
    assert set(routes) == set(ROUTES)

    for name, (path, method, response_model, request_model, errors) in ROUTES.items():
        route = routes[name]
        assert route.path == path
        assert route.methods == {method}
        assert route.response_model is response_model
        assert route.response_model_exclude_unset is True
        assert set(route.responses) == set(errors)
        assert all(value == {"model": HttpError} for value in route.responses.values())
        body_models = [
            field.field_info.annotation for field in route.dependant.body_params
        ]
        assert body_models == ([] if request_model is None else [request_model])

    schema = app.openapi()
    components = schema["components"]["schemas"]
    for model in ("ProjectMember", "ProjectInvite", "ProjectMemberSet"):
        assert components[model]["additionalProperties"] is False
    assert "id" not in components["ProjectMember"]["required"]
    assert "slug" not in components["ProjectInvite"]["required"]
    assert {
        "email",
        "role",
    } <= set(components["SetProjectMemberRequest"]["properties"])
    assert components["CreateProjectInviteRequest"]["required"] == ["email"]
    assert ProjectMemberList.model_validate(
        [{"user_id": 7, "email": "future@example.com", "role": "viewer"}]
    ).model_dump(exclude_unset=True) == [
        {"user_id": 7, "email": "future@example.com", "role": "viewer"}
    ]
    assert ProjectInviteList.model_validate(
        {
            "invites": [
                {
                    "id": 8,
                    "project_id": "future-project",
                    "email": "future@example.com",
                    "role": "viewer",
                    "created_at": "2026-01-01T00:00:00Z",
                    "expires_at": "2026-01-15T00:00:00Z",
                    "accepted_at": None,
                    "revoked_at": None,
                }
            ]
        }
    ).model_dump(exclude_unset=True) == {
        "invites": [
            {
                "id": 8,
                "project_id": "future-project",
                "email": "future@example.com",
                "role": "viewer",
                "created_at": "2026-01-01T00:00:00Z",
                "expires_at": "2026-01-15T00:00:00Z",
                "accepted_at": None,
                "revoked_at": None,
            }
        ]
    }


def test_real_team_collaboration_shapes_permissions_history_and_effects(
    tmp_path: Path,
) -> None:
    sent_mail: list[tuple[str, str]] = []

    async def mail(email: str, link: str) -> bool:
        sent_mail.append((email, link))
        return True

    app = create_team_app(
        _config(tmp_path), send_magic_email=mail, product_telemetry_destination=None
    )
    owner = claim_server(app, origin="http://testserver")
    editor, outsider = TestClient(app), TestClient(app)
    for email in ("editor@example.com", "outsider@example.com"):
        seed_member_invite(app, email)
        _login(editor if email.startswith("editor") else outsider, app, email)
    sent_mail.clear()

    project = owner.post("/api/projects", json={"name": "Collaborators"})
    assert project.status_code == 200, project.text
    pid = project.json()["id"]

    assert TestClient(app).get(f"/api/projects/{pid}/members").status_code == 401
    assert (
        outsider.post(
            f"/api/projects/{pid}/invites", json={"email": "nope@example.com"}
        ).status_code
        == 403
    )
    not_found = owner.post(
        f"/api/projects/{pid}/members",
        json={"email": "missing@example.com", "role": "viewer"},
    )
    assert not_found.status_code == 404

    audit_before = _audits(app, action="project_member_set")
    refused_editor = owner.post(
        f"/api/projects/{pid}/members",
        json={"email": "editor@example.com", "role": "editor", "ignored": True},
    )
    assert refused_editor.status_code == 422, refused_editor.text
    assert refused_editor.json() == {
        "detail": [
            {
                "type": "extra_forbidden",
                "loc": ["body", "ignored"],
                "msg": "Extra inputs are not permitted",
                "input": True,
            }
        ]
    }
    assert _audits(app, action="project_member_set") == audit_before
    assert {
        row["email"] for row in owner.get(f"/api/projects/{pid}/members").json()
    } == {"owner@example.com"}

    set_editor = owner.post(
        f"/api/projects/{pid}/members",
        json={"email": "editor@example.com", "role": "editor"},
    )
    assert set_editor.status_code == 200, set_editor.text
    assert set_editor.json() == {
        "ok": True,
        "email": "editor@example.com",
        "role": "editor",
    }
    assert _audits(app, action="project_member_set") == audit_before + [
        f"{pid}:editor@example.com:editor"
    ]

    members = owner.get(f"/api/projects/{pid}/members")
    assert members.status_code == 200, members.text
    member_rows = members.json()
    assert all(set(row) == {"user_id", "email", "role"} for row in member_rows)
    assert {row["email"] for row in member_rows} == {
        "owner@example.com",
        "editor@example.com",
    }
    assert editor.get(f"/api/projects/{pid}/members").status_code == 403

    invite_before = _audits(app, action="project_invited")
    refused_invite = editor.post(
        f"/api/projects/{pid}/invites",
        json={"email": "first@example.com", "ignored": "kept-compatible"},
    )
    assert refused_invite.status_code == 422, refused_invite.text
    assert refused_invite.json() == {
        "detail": [
            {
                "type": "extra_forbidden",
                "loc": ["body", "ignored"],
                "msg": "Extra inputs are not permitted",
                "input": "kept-compatible",
            }
        ]
    }
    assert _audits(app, action="project_invited") == invite_before
    assert sent_mail == []
    assert editor.get(f"/api/projects/{pid}/invites").json() == {"invites": []}

    created = editor.post(
        f"/api/projects/{pid}/invites",
        json={"email": "first@example.com"},
    )
    assert created.status_code == 200, created.text
    first = created.json()["invite"]
    assert created.json()["sent"] is True
    assert set(first) == {
        "id",
        "project_id",
        "email",
        "role",
        "created_at",
        "expires_at",
        "accepted_at",
        "revoked_at",
    }
    assert first["project_id"] == pid
    assert first["role"] == "viewer"
    assert first["accepted_at"] is None
    assert first["revoked_at"] is None
    assert not ({"token", "token_hash", "org_id", "invited_by_user_id"} & set(first))
    assert sent_mail == [("first@example.com", sent_mail[0][1])]
    assert "/auth/project-invites/" in sent_mail[0][1]
    assert _audits(app, action="project_invited") == invite_before + [
        f"{pid}:first@example.com:viewer"
    ]

    second = editor.post(
        f"/api/projects/{pid}/invites",
        json={"email": "second@example.com", "role": "editor"},
    )
    assert second.status_code == 200, second.text
    listed = editor.get(f"/api/projects/{pid}/invites")
    assert listed.status_code == 200, listed.text
    history = listed.json()["invites"]
    assert [row["id"] for row in history] == [
        first["id"],
        second.json()["invite"]["id"],
    ]
    assert history[1]["accepted_at"] is None
    assert history[1]["revoked_at"] is None

    revoke_before = _audits(app, action="project_invite_revoked")
    revoked = editor.delete(f"/api/projects/{pid}/invites/{first['id']}")
    assert revoked.status_code == 200, revoked.text
    assert revoked.json() == {"ok": True, "revoked": True}
    repeated_revoke = editor.delete(f"/api/projects/{pid}/invites/{first['id']}")
    assert repeated_revoke.status_code == 200, repeated_revoke.text
    assert repeated_revoke.json() == {"ok": True, "revoked": False}
    assert _audits(app, action="project_invite_revoked") == revoke_before + [
        f"{pid}:{first['id']}"
    ]
    unpruned = editor.get(f"/api/projects/{pid}/invites")
    assert unpruned.status_code == 200, unpruned.text
    assert [row["id"] for row in unpruned.json()["invites"]] == [
        first["id"],
        second.json()["invite"]["id"],
    ]
    assert unpruned.json()["invites"][0]["revoked_at"] is not None

    remove_before = _audits(app, action="project_member_removed")
    removed = owner.delete(f"/api/projects/{pid}/members/editor@example.com")
    assert removed.status_code == 200, removed.text
    assert removed.json() == {"ok": True, "removed": True}
    repeated_remove = owner.delete(f"/api/projects/{pid}/members/editor@example.com")
    assert repeated_remove.status_code == 200, repeated_remove.text
    assert repeated_remove.json() == {"ok": True, "removed": False}
    assert _audits(app, action="project_member_removed") == remove_before + [
        f"{pid}:editor@example.com"
    ]

    last_owner = owner.post(
        f"/api/projects/{pid}/members",
        json={"email": "owner@example.com", "role": "editor"},
    )
    assert last_owner.status_code == 409
    assert (
        editor.post(f"/api/projects/{pid}/invites", json={"email": None}).status_code
        == 422
    )


def _audits(app: Any, *, action: str) -> list[str]:
    with app.state.control_engine.connect() as cx:
        return list(
            cx.execute(
                sa.select(audit_log.c.detail)
                .where(audit_log.c.action == action)
                .order_by(audit_log.c.id)
            ).scalars()
        )
