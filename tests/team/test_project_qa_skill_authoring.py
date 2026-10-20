from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from fastapi.testclient import TestClient

from frisket.engine.store.project_qa import ProjectQAStore
from frisket.team.app import TeamConfig, create_team_app
from frisket.team.schema import memberships, users
from tests.team_setup_helpers import (
    claim_server,
    seed_member_invite,
    sign_in_with_magic_link,
)


def _skill_content(name: str) -> str:
    return (
        f"---\nname: {name}\ndescription: Check representative sources.\n---\n\n"
        "# Source checks\n\nInspect representative sources.\n"
    )


def _append_proposal(
    store: ProjectQAStore, thread_id: str, *, proposal_id: str, name: str
) -> str:
    turn = store.submit_turn(
        thread_id, request_id=f"draft-{proposal_id}", question="Create the skill"
    )
    store.append_skill_proposal(
        turn["id"],
        payload={
            "proposal_id": proposal_id,
            "target_skill_id": None,
            "base_revision": None,
            "name": name,
            "description": "Check representative sources.",
            "content": _skill_content(name),
            "enabled": True,
            "scope": "organization",
        },
    )
    store.finish_turn(turn["id"], status="completed")
    return str(turn["id"])


def test_team_skill_save_requires_current_admin_and_project_access(
    tmp_path: Path,
) -> None:
    async def send_magic_email(_email: str, _link: str) -> bool:
        return True

    app = create_team_app(
        TeamConfig(
            database_url=f"sqlite:///{tmp_path / 'control.db'}",
            run_queue_database_url=f"sqlite:///{tmp_path / 'queue.db'}",
            data_dir=tmp_path / "data",
            base_url="http://testserver",
            organization_name="Investigations Desk",
            admin_emails={"owner@example.com"},
        ),
        send_magic_email=send_magic_email,
    )
    owner = claim_server(app, email="owner@example.com")
    member = TestClient(app)
    seed_member_invite(app, "editor@example.com")
    sign_in_with_magic_link(app, member, "editor@example.com")

    project = owner.post("/api/projects", json={"name": "Shared Evidence"})
    assert project.status_code == 200, project.text
    project_id = project.json()["id"]
    granted = owner.post(
        f"/api/projects/{project_id}/members",
        json={"email": "editor@example.com", "role": "editor"},
    )
    assert granted.status_code == 200, granted.text

    created_thread = owner.post(
        f"/api/projects/{project_id}/qa/threads",
        json={"scope": {"kind": "project"}},
    )
    assert created_thread.status_code == 200, created_thread.text
    thread_id = created_thread.json()["id"]
    detail_path = f"/api/projects/{project_id}/qa/threads/{thread_id}"
    assert owner.get(detail_path).json()["can_manage_skills"] is True
    assert member.get(detail_path).json()["can_manage_skills"] is False

    store = ProjectQAStore(app.state.workspace.get(project_id))
    first_turn = _append_proposal(
        store, thread_id, proposal_id="team-first", name="team-first"
    )
    first_path = detail_path + f"/turns/{first_turn}/skill-proposals/team-first/save"
    first_body = {
        "request_id": "save-team-first",
        "content": _skill_content("team-first"),
        "expected_revision": None,
    }

    editor_denied = member.post(first_path, json=first_body)
    assert editor_denied.status_code == 403, editor_denied.text
    assert editor_denied.json()["detail"] == "not an admin"
    saved = owner.post(first_path, json=first_body)
    assert saved.status_code == 200, saved.text
    assert saved.json()["skill"]["name"] == "team-first"

    second_turn = _append_proposal(
        store, thread_id, proposal_id="team-second", name="team-second"
    )
    with app.state.control_engine.begin() as connection:
        user_ids = dict(
            connection.execute(
                sa.select(users.c.email, users.c.id).where(
                    users.c.email.in_(("owner@example.com", "editor@example.com"))
                )
            ).all()
        )
        # Preserve a claimed Team server while revoking the original actor's
        # organization-admin authority. Their explicit project-owner row
        # remains, so project access survives and only the admin revocation
        # can account for the later denial.
        connection.execute(
            memberships.update()
            .where(
                memberships.c.org_id == app.state.team_org_id,
                memberships.c.user_id == user_ids["editor@example.com"],
            )
            .values(role="owner")
        )
        connection.execute(
            memberships.update()
            .where(
                memberships.c.org_id == app.state.team_org_id,
                memberships.c.user_id == user_ids["owner@example.com"],
            )
            .values(role="member")
        )

    revoked_detail = owner.get(detail_path)
    assert revoked_detail.status_code == 200, revoked_detail.text
    assert revoked_detail.json()["can_manage_skills"] is False
    revoked = owner.post(
        detail_path + f"/turns/{second_turn}/skill-proposals/team-second/save",
        json={
            "request_id": "save-team-second",
            "content": _skill_content("team-second"),
            "expected_revision": None,
        },
    )
    assert revoked.status_code == 403, revoked.text
    assert revoked.json()["detail"] == "not an admin"
    assert [skill["name"] for skill in app.state.skill_library.list()["skills"]] == [
        "team-first"
    ]
