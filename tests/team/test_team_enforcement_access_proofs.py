"""Executable access proofs for the team enforcement resolver layer."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from frisket.server.app import create_app
from frisket.team.app import TeamProjectAccess
from frisket.team.enforcement import protect_core_app
from frisket.team.invite_service import InviteConflict, InviteForbidden, InviteService
from frisket.team.schema import (
    memberships,
    metadata,
    orgs,
    project_roles,
    projects,
    users,
)


ACTOR = {"id": 7, "email": "admin@example.com", "org_id": 1, "auth": "session"}
ROLE_ORDER = ("viewer", "reviewer", "editor", "owner")


class _RecordingProjectAccess:
    def __init__(self, role: str) -> None:
        self.role = role
        self.required_roles: list[str] = []

    def resolve_project_for_user_id(
        self,
        user_id: int,
        slug: str,
        *,
        preferred_org_id: int | None = None,
        scope_org_id: int | None = None,
    ) -> dict[str, Any] | None:
        return {"org_id": 1, "storage_org_id": 1, "slug": slug}

    def can_on_project(self, org_id: int, slug: str, user_id: int, need: str) -> bool:
        self.required_roles.append(need)
        return ROLE_ORDER.index(self.role) >= ROLE_ORDER.index(need)

    def pop_required_role(self) -> str:
        assert self.required_roles, "the request did not reach the project-role gate"
        assert len(self.required_roles) == 1
        return self.required_roles.pop()


def _core(tmp_path: Path, name: str) -> FastAPI:
    return create_app(
        tmp_path / name,
        serve_spa=False,
        enable_provider_config=False,
        edition="team",
    )


def _protect(
    app: FastAPI,
    access: _RecordingProjectAccess,
    *,
    org_role: Callable[[int, int], str | None] | None = None,
    actor: dict[str, Any] | None = ACTOR,
) -> FastAPI:
    def resolve_user(_request: Request) -> dict[str, Any] | None:
        return None if actor is None else dict(actor)

    return protect_core_app(
        app=app,
        resolve_user=resolve_user,
        admin_emails=lambda: {ACTOR["email"]},
        project_access=access,
        org_role=org_role,
    )


@pytest.mark.parametrize(
    ("method", "path"),
    (
        ("POST", "/api/projects/proof/notification-channels"),
        ("PATCH", "/api/projects/proof/notification-channels/11"),
        ("POST", "/api/projects/proof/notification-routes"),
        ("PATCH", "/api/projects/proof/notification-routes/12"),
    ),
    ids=(
        "create-notification-channel",
        "patch-notification-channel",
        "create-notification-route",
        "patch-notification-route",
    ),
)
def test_notification_owner_routes_discriminate_role_from_body(
    tmp_path: Path, method: str, path: str
) -> None:
    access = _RecordingProjectAccess("editor")
    app = _protect(_core(tmp_path, "notification-owner"), access)

    with TestClient(app, raise_server_exceptions=False) as client:
        mine = client.request(
            method,
            path,
            json={"owner_kind": "user", "owner_ref": f"user:{ACTOR['id']}"},
        )
        assert access.pop_required_role() == "editor"
        assert mine.status_code != 403, mine.text

        elevated = client.request(
            method,
            path,
            json={"owner_kind": "project", "owner_ref": "project:proof"},
        )
        assert access.pop_required_role() == "owner"
        assert elevated.status_code == 403, elevated.text
        assert "requires 'owner'" in elevated.json()["detail"]


def test_action_run_discriminates_review_decision_role_from_body(
    tmp_path: Path,
) -> None:
    access = _RecordingProjectAccess("reviewer")
    app = _protect(_core(tmp_path, "review-decision"), access)
    path = "/api/projects/proof/actions/v1/run"

    with TestClient(app, raise_server_exceptions=False) as client:
        review = client.post(path, json={"action_id": "review.decision"})
        assert access.pop_required_role() == "reviewer"
        assert review.status_code != 403, review.text

        ordinary = client.post(path, json={"action_id": "map.template"})
        assert access.pop_required_role() == "editor"
        assert ordinary.status_code == 403, ordinary.text
        assert "requires 'editor'" in ordinary.json()["detail"]

        legacy_dialect = client.post(path, json={"kind": "review.decision"})
        assert access.pop_required_role() == "editor"
        assert legacy_dialect.status_code == 403, legacy_dialect.text


def test_update_sheet_requires_editor_role(tmp_path: Path) -> None:
    app = _core(tmp_path, "update-sheet-editor")
    pid = str(app.state.workspace.create("Sheet proof", project_id="sheet-proof")["id"])
    sheet_id = app.state.workspace.get(pid).add_sheet("Proof sheet")
    access = _RecordingProjectAccess("viewer")
    _protect(app, access)
    path = f"/api/projects/{pid}/sheets/{sheet_id}"

    with TestClient(app, raise_server_exceptions=False) as client:
        refused = client.patch(path, json={"title_column_id": None})
        assert access.pop_required_role() == "editor"
        assert refused.status_code == 403, refused.text
        assert "requires 'editor'" in refused.json()["detail"]

        access.role = "editor"
        allowed = client.patch(path, json={"title_column_id": None})
        assert access.pop_required_role() == "editor"
        assert allowed.status_code == 200, allowed.text


def test_notification_route_test_uses_path_and_persisted_owner_not_body(
    tmp_path: Path,
) -> None:
    app = _core(tmp_path, "notification-route-test")
    pid = str(app.state.workspace.create("Route proof", project_id="route-proof")["id"])
    project = app.state.workspace.get(pid)
    channel = project.create_notification_channel(kind="in_app", name="In app")
    mine = project.create_notification_route(
        name="Mine",
        channel_id=int(channel["id"]),
        owner_kind="user",
        owner_ref=f"user:{ACTOR['id']}",
    )
    project_owned = project.create_notification_route(
        name="Project owned",
        channel_id=int(channel["id"]),
        owner_kind="project",
        owner_ref=pid,
    )
    access = _RecordingProjectAccess("editor")
    _protect(app, access)

    with TestClient(app, raise_server_exceptions=False) as client:
        allowed = client.post(
            f"/api/projects/{pid}/notification-routes/{mine['id']}/test",
            json={"owner_kind": "project", "owner_ref": pid},
        )
        assert access.pop_required_role() == "editor"
        assert allowed.status_code != 403, allowed.text

        spoofed = client.post(
            f"/api/projects/{pid}/notification-routes/{project_owned['id']}/test",
            json={"owner_kind": "user", "owner_ref": f"user:{ACTOR['id']}"},
        )
        assert access.pop_required_role() == "owner"
        assert spoofed.status_code == 403, spoofed.text
        assert "requires 'owner'" in spoofed.json()["detail"]


def test_invite_service_editor_gate_uses_injected_project_access(
    tmp_path: Path,
) -> None:
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'invite-access.db'}", future=True)
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(orgs.insert().values(id=1, name="Proof org"))
        connection.execute(
            users.insert().values(
                id=ACTOR["id"],
                email=ACTOR["email"],
                default_org_id=1,
            )
        )
        connection.execute(
            memberships.insert().values(user_id=ACTOR["id"], org_id=1, role="owner")
        )
        connection.execute(
            projects.insert().values(
                id=1,
                org_id=1,
                storage_org_id=1,
                slug="proof",
                name="Proof",
                sensitive=False,
                network="inherit",
            )
        )

    access = _RecordingProjectAccess("viewer")
    service = InviteService(engine, org_id=1, access=access)
    try:
        with pytest.raises(InviteForbidden, match="project editor required"):
            service.list_project_invites(actor_user_id=int(ACTOR["id"]), slug="proof")
        assert access.pop_required_role() == "editor"
    finally:
        engine.dispose()


def test_invite_creation_role_ladder_uses_real_project_access(tmp_path: Path) -> None:
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'role-ladder.db'}", future=True)
    metadata.create_all(engine)
    roles = ("viewer", "reviewer", "editor", "owner")
    with engine.begin() as connection:
        connection.execute(orgs.insert().values(id=1, name="Proof org"))
        connection.execute(
            projects.insert().values(
                id=1,
                org_id=1,
                storage_org_id=1,
                slug="proof",
                name="Proof",
                sensitive=False,
                network="inherit",
            )
        )
        for user_id, role in enumerate(roles, start=1):
            connection.execute(
                users.insert().values(
                    id=user_id,
                    email=f"{role}@example.com",
                    default_org_id=1,
                )
            )
            connection.execute(
                memberships.insert().values(user_id=user_id, org_id=1, role="member")
            )
            connection.execute(
                project_roles.insert().values(
                    org_id=1, slug="proof", user_id=user_id, role=role
                )
            )

    service = InviteService(engine, org_id=1, access=TeamProjectAccess(engine, 1))
    try:
        for user_id, role in enumerate(roles, start=1):
            if role in {"viewer", "reviewer"}:
                with pytest.raises(InviteForbidden, match="project editor required"):
                    service.create_project_invite(
                        actor_user_id=user_id,
                        slug="proof",
                        email=f"invite-{role}@example.com",
                        role="viewer",
                    )
            else:
                invite, token = service.create_project_invite(
                    actor_user_id=user_id,
                    slug="proof",
                    email=f"invite-{role}@example.com",
                    role="viewer",
                )
                assert invite["email"] == f"invite-{role}@example.com"
                assert token
    finally:
        engine.dispose()


def test_project_invite_replay_and_stale_inviter_are_refused(tmp_path: Path) -> None:
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'invite-state.db'}", future=True)
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(orgs.insert().values(id=1, name="Proof org"))
        connection.execute(
            users.insert().values(
                id=1,
                email="editor@example.com",
                default_org_id=1,
            )
        )
        connection.execute(
            memberships.insert().values(user_id=1, org_id=1, role="member")
        )
        connection.execute(
            projects.insert().values(
                id=1,
                org_id=1,
                storage_org_id=1,
                slug="proof",
                name="Proof",
                sensitive=False,
                network="inherit",
            )
        )
        connection.execute(
            project_roles.insert().values(
                org_id=1, slug="proof", user_id=1, role="editor"
            )
        )

    service = InviteService(engine, org_id=1, access=TeamProjectAccess(engine, 1))
    try:
        _, replay_token = service.create_project_invite(
            actor_user_id=1,
            slug="proof",
            email="replay@example.com",
            role="viewer",
        )
        assert (
            service.accept_project_invite(
                replay_token, password="a sufficiently long password"
            )
            is not None
        )
        with pytest.raises(InviteConflict) as replayed:
            service.accept_project_invite(
                replay_token, password="a sufficiently long password"
            )
        assert replayed.value.status_code == 410

        _, stale_token = service.create_project_invite(
            actor_user_id=1,
            slug="proof",
            email="stale@example.com",
            role="viewer",
        )
        with engine.begin() as connection:
            connection.execute(
                project_roles.update()
                .where(
                    project_roles.c.org_id == 1,
                    project_roles.c.slug == "proof",
                    project_roles.c.user_id == 1,
                )
                .values(role="reviewer")
            )
        with pytest.raises(InviteForbidden, match="project editor required"):
            service.accept_project_invite(
                stale_token, password="a sufficiently long password"
            )
    finally:
        engine.dispose()


def test_org_role_port_takes_priority_over_server_admin_fallback(
    tmp_path: Path,
) -> None:
    with_port = _protect(
        _core(tmp_path, "spend-with-org-role"),
        _RecordingProjectAccess("owner"),
        org_role=lambda _user_id, _org_id: "member",
    )
    without_port = _protect(
        _core(tmp_path, "spend-without-org-role"),
        _RecordingProjectAccess("owner"),
    )

    with TestClient(with_port, raise_server_exceptions=False) as client:
        refused = client.get("/api/spend")
    assert refused.status_code == 403, refused.text
    assert refused.json()["detail"] == "only an org owner or admin can view org spend"

    with TestClient(without_port, raise_server_exceptions=False) as client:
        allowed = client.get("/api/spend")
    assert allowed.status_code not in {401, 403}, allowed.text


@pytest.mark.parametrize(
    ("role", "auth", "expected"),
    (
        ("owner", "session", 200),
        ("admin", "session", 200),
        ("owner", "pat", 200),
        ("admin", "pat", 200),
        ("member", "session", 403),
        ("editor", "pat", 403),
    ),
)
def test_spend_requires_org_owner_or_admin_for_session_and_pat(
    tmp_path: Path,
    role: str,
    auth: str,
    expected: int,
) -> None:
    actor = {**ACTOR, "auth": auth}
    access = _RecordingProjectAccess("owner")
    app = _protect(
        _core(tmp_path, f"spend-{role}-{auth}"),
        access,
        actor=actor,
        org_role=lambda _user_id, _org_id: role,
    )

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/api/spend")

    assert response.status_code == expected, response.text
    assert access.required_roles == []
    if expected == 403:
        assert (
            response.json()["detail"] == "only an org owner or admin can view org spend"
        )


def test_spend_requires_authentication_before_org_resolution(tmp_path: Path) -> None:
    access = _RecordingProjectAccess("owner")
    app = _protect(
        _core(tmp_path, "spend-anonymous"),
        access,
        actor=None,
        org_role=lambda _user_id, _org_id: "owner",
    )

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/api/spend")

    assert response.status_code == 401, response.text
    assert response.json()["detail"] == "not signed in"
    assert access.required_roles == []


def test_org_owner_fallback_satisfies_the_strongest_base_project_role(
    tmp_path: Path,
) -> None:
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'roles.db'}", future=True)
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(orgs.insert().values(id=1, name="Proof org"))
        connection.execute(
            users.insert().values(
                id=ACTOR["id"],
                email=ACTOR["email"],
                default_org_id=1,
            )
        )
        connection.execute(
            memberships.insert().values(user_id=ACTOR["id"], org_id=1, role="owner")
        )
        connection.execute(
            projects.insert().values(
                id=1,
                org_id=1,
                storage_org_id=1,
                slug="proof",
                name="Proof",
                sensitive=False,
                network="inherit",
            )
        )

    access = TeamProjectAccess(engine, org_id=1)
    try:
        assert access.can_on_project(1, "proof", int(ACTOR["id"]), "owner")
    finally:
        engine.dispose()
