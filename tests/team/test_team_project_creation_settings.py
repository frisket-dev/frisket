"""Team project creation carries Allowed services and usage-data sharing."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from fastapi.testclient import TestClient

from frisket.team.bootstrap import init_identity_schema
from frisket.team.schema import metadata, project_creation_intents, projects
from frisket.team.app import (
    TeamConfig,
    _intent_allowed_services,
    _intent_usage_data_sharing,
    create_team_app,
)
from tests.team_setup_helpers import (
    claim_server,
    seed_member_invite,
    sign_in_with_magic_link,
)


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


def _owner(tmp_path: Path) -> tuple[Any, TestClient]:
    app = _team_app(tmp_path)
    client = TestClient(app)
    claim_server(app, client=client)
    sign_in_with_magic_link(app, client, "owner@example.com")
    return app, client


def test_team_create_accepts_both_settings_and_keeps_sensitive_in_step(
    tmp_path: Path,
) -> None:
    app, client = _owner(tmp_path)
    made = client.post(
        "/api/projects",
        json={
            "name": "",
            "allowed_services": "this_machine_only",
            "usage_data_sharing": "off",
        },
    )
    assert made.status_code == 200, made.text
    body = made.json()
    assert body["name"] == "Untitled project"
    assert body["allowed_services"] == "this_machine_only"
    assert body["usage_data_sharing"] == "off"
    manifest = json.loads(
        (tmp_path / "data" / f"{body['id']}.frisket" / "manifest.json").read_text()
    )
    assert manifest["allowed_services"] == "this_machine_only"
    with app.state.control_engine.connect() as cx:
        sensitive = cx.execute(
            sa.select(projects.c.sensitive).where(projects.c.slug == body["id"])
        ).scalar_one()
    assert sensitive is True


def test_team_create_is_idempotent_over_both_settings(tmp_path: Path) -> None:
    _app, client = _owner(tmp_path)
    request = {"name": "Notes", "allowed_services": "approved_services"}
    headers = {"Idempotency-Key": "create-notes"}
    first = client.post("/api/projects", json=request, headers=headers)
    again = client.post("/api/projects", json=request, headers=headers)
    assert first.status_code == again.status_code == 200, again.text
    assert again.json()["id"] == first.json()["id"]
    changed = client.post(
        "/api/projects",
        json={**request, "allowed_services": "all_services"},
        headers=headers,
    )
    assert changed.status_code == 409


def test_v9_control_plane_adds_intent_settings_and_keeps_rows(
    tmp_path: Path,
) -> None:
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'v9.db'}", future=True)
    init_identity_schema(engine)
    with engine.begin() as cx:
        for name in ("allowed_services", "usage_data_sharing"):
            cx.exec_driver_sql(
                f"ALTER TABLE project_creation_intents DROP COLUMN {name}"
            )
        cx.exec_driver_sql("UPDATE schema_marker SET schema_version = 9 WHERE id = 1")
        cx.exec_driver_sql(
            "INSERT INTO organizations (id, name) VALUES (1, 'Newsroom')"
        )
        cx.exec_driver_sql(
            "INSERT INTO users (id, email) VALUES (1, 'owner@example.test')"
        )
        cx.exec_driver_sql(
            "INSERT INTO project_creation_intents "
            "(slug, organization_id, creator_user_id, name, sensitive) "
            "VALUES ('old', 1, 1, 'Old', 1)"
        )

    init_identity_schema(engine)

    with engine.connect() as cx:
        assert (
            cx.exec_driver_sql(
                "SELECT schema_version FROM schema_marker WHERE id = 1"
            ).scalar_one()
            == 10
        )
        intent = cx.execute(sa.select(project_creation_intents)).one()
    assert (intent.sensitive, intent.allowed_services, intent.usage_data_sharing) == (
        True,
        None,
        None,
    )
    assert _intent_allowed_services(intent) == "all_services"
    assert _intent_usage_data_sharing(intent) == "off"
    assert set(metadata.tables) >= {"project_creation_intents"}
    engine.dispose()


def test_organization_owner_manages_approved_endpoints_members_read(
    tmp_path: Path,
) -> None:
    app, owner = _owner(tmp_path)
    member = TestClient(app)
    seed_member_invite(app, "member@example.com")
    sign_in_with_magic_link(app, member, "member@example.com")
    body = {"endpoints": [{"host": "llm.newsroom.example.test", "label": "Desk"}]}

    denied = member.put("/api/organization/approved-endpoints", json=body)
    assert denied.status_code == 403, denied.text
    saved = owner.put("/api/organization/approved-endpoints", json=body)
    assert saved.status_code == 200, saved.text
    listed = member.get("/api/organization/approved-endpoints")
    assert listed.status_code == 200, listed.text
    assert listed.json()["endpoints"][0]["host"] == "llm.newsroom.example.test"
    assert (tmp_path / "data" / ".frisket" / "approved_endpoints.json").exists()
