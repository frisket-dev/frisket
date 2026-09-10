"""The team twin of the project-delete route: owner-gated, name-confirmed,
audited, and control-plane-consistent.

The core route (tests/server/test_server_project_lifecycle_routes.py) already
covers the danger-zone gates in isolation (server-side name confirmation,
runs-in-flight refusal, bundle removal). This suite pins the behaviour the team
edition adds on top: a non-owner is denied, a correct deletion removes the
control-plane rows and writes an audit entry, and the typed-name confirmation is
still enforced through the twin."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from frisket.team.app import TeamConfig, create_team_app
from tests.team_setup_helpers import (
    claim_server,
    seed_member_invite,
    sign_in_with_magic_link,
)

pytestmark = pytest.mark.gap


def _team_app(tmp_path: Path) -> Any:
    async def send(_email: str, _link: str) -> bool:
        return True

    return create_team_app(
        TeamConfig(
            database_url=f"sqlite:///{tmp_path / 'control.db'}",
            run_queue_database_url=f"sqlite:///{tmp_path / 'run-queue.db'}",
            data_dir=tmp_path / "data",
            base_url="http://testserver",
            organization_name="One Org",
            admin_emails={"owner@example.com"},
        ),
        send_magic_email=send,
    )


def _login(client: TestClient, app: Any, email: str) -> None:
    claim_server(app, client=client)
    if email != "owner@example.com":
        seed_member_invite(app, email)
    sign_in_with_magic_link(app, client, email)


def test_team_delete_denies_non_owner_and_owner_deletes_with_audit(
    tmp_path: Path,
) -> None:
    from frisket.team.schema import audit_log, projects

    app = _team_app(tmp_path)
    owner, editor = TestClient(app), TestClient(app)
    _login(owner, app, "owner@example.com")
    _login(editor, app, "editor@example.com")

    made = owner.post("/api/projects", json={"name": "Doomed"})
    assert made.status_code == 200, made.text
    pid = made.json()["id"]
    owner.post(
        f"/api/projects/{pid}/members",
        json={"email": "editor@example.com", "role": "editor"},
    )
    bundle = tmp_path / "data" / f"{pid}.frisket"
    assert bundle.exists()

    # An editor (below owner on the ladder) cannot delete.
    denied = editor.request(
        "DELETE", f"/api/projects/{pid}", json={"confirm_name": "Doomed"}
    )
    assert denied.status_code == 403, denied.text
    assert bundle.exists()

    # A wrong name is refused even for the owner (server-side confirmation).
    wrong = owner.request(
        "DELETE", f"/api/projects/{pid}", json={"confirm_name": "wrong"}
    )
    assert wrong.status_code == 422, wrong.text
    assert bundle.exists()

    # The owner deletes with the exact name: bundle gone, control-plane row
    # gone, and the action is attributed in the audit log.
    ok = owner.request(
        "DELETE", f"/api/projects/{pid}", json={"confirm_name": "Doomed"}
    )
    assert ok.status_code == 200, ok.text
    assert ok.json() == {"ok": True, "deleted": pid}
    assert not bundle.exists()
    assert owner.get("/api/projects").json() == []

    with app.state.control_engine.connect() as cx:
        remaining = cx.execute(
            sa.select(sa.func.count())
            .select_from(projects)
            .where(projects.c.slug == pid)
        ).scalar_one()
        audited = cx.execute(
            sa.select(audit_log.c.detail).where(
                audit_log.c.action == "project_deleted",
                audit_log.c.detail == pid,
            )
        ).fetchall()
    assert remaining == 0
    assert len(audited) == 1
