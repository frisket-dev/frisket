"""Team project creation carries Allowed services and usage-data sharing."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

import frisket.team.app as team_app

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


def test_team_create_carries_the_cover_seed(tmp_path: Path) -> None:
    _app, client = _owner(tmp_path)
    made = client.post("/api/projects", json={"name": "Notes", "cover_seed": "q7z"})
    assert made.status_code == 200, made.text
    pid = made.json()["id"]
    assert made.json()["cover_seed"] == "q7z"
    listed = {row["id"]: row for row in client.get("/api/projects").json()}
    assert listed[pid]["cover_seed"] == "q7z"
    unseeded = client.post("/api/projects", json={"name": "Plain"}).json()
    assert unseeded["cover_seed"] is None
    refused = client.post("/api/projects", json={"name": "Bad", "cover_seed": "a b"})
    assert refused.status_code == 422, refused.text


def test_team_idempotent_create_compares_the_cover_seed(tmp_path: Path) -> None:
    _app, client = _owner(tmp_path)
    headers = {"Idempotency-Key": "create-seeded"}
    request = {"name": "Seeded", "cover_seed": "aaaa"}
    first = client.post("/api/projects", json=request, headers=headers)
    again = client.post("/api/projects", json=request, headers=headers)
    assert first.status_code == again.status_code == 200, again.text
    assert again.json()["cover_seed"] == "aaaa"
    changed = client.post(
        "/api/projects", json={**request, "cover_seed": "bbbb"}, headers=headers
    )
    assert changed.status_code == 409


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
        for name in ("allowed_services", "usage_data_sharing", "cover_seed"):
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
            == 11
        )
        intent = cx.execute(sa.select(project_creation_intents)).one()
    assert (
        intent.sensitive,
        intent.allowed_services,
        intent.usage_data_sharing,
        intent.cover_seed,
    ) == (True, None, None, None)
    assert _intent_allowed_services(intent) == "all_services"
    assert _intent_usage_data_sharing(intent) == "off"
    assert set(metadata.tables) >= {"project_creation_intents"}
    engine.dispose()


def _columns(engine: sa.Engine, table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(engine).get_columns(table)}


def test_fresh_control_plane_has_every_intent_and_default_column(
    tmp_path: Path,
) -> None:
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'fresh.db'}", future=True)
    init_identity_schema(engine)
    with engine.connect() as cx:
        version = cx.exec_driver_sql(
            "SELECT schema_version FROM schema_marker WHERE id = 1"
        ).scalar_one()
    assert version == 11
    assert {"allowed_services", "usage_data_sharing", "cover_seed"} <= _columns(
        engine, "project_creation_intents"
    )
    assert "allowed_services_default" in _columns(engine, "organizations")
    assert "network_default" not in _columns(engine, "organizations")
    assert "network" not in _columns(engine, "projects")
    engine.dispose()


def test_v10_control_plane_keeps_cover_seeds_and_folds_network(
    tmp_path: Path,
) -> None:
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'v10.db'}", future=True)
    init_identity_schema(engine)
    with engine.begin() as cx:
        cx.exec_driver_sql(
            "ALTER TABLE organizations DROP COLUMN allowed_services_default"
        )
        cx.exec_driver_sql(
            "ALTER TABLE organizations ADD COLUMN network_default VARCHAR"
        )
        cx.exec_driver_sql("ALTER TABLE projects ADD COLUMN network VARCHAR")
        cx.exec_driver_sql("UPDATE schema_marker SET schema_version = 10 WHERE id = 1")
        cx.exec_driver_sql(
            "INSERT INTO organizations (id, name, network_default) "
            "VALUES (1, 'Newsroom', 'off')"
        )
        cx.exec_driver_sql(
            "INSERT INTO users (id, email) VALUES (1, 'owner@example.test')"
        )
        cx.exec_driver_sql(
            "INSERT INTO project_creation_intents "
            "(slug, organization_id, creator_user_id, name, sensitive, cover_seed) "
            "VALUES ('seeded', 1, 1, 'Seeded', 0, 'k3x9')"
        )

    init_identity_schema(engine)

    with engine.connect() as cx:
        assert (
            cx.exec_driver_sql(
                "SELECT schema_version FROM schema_marker WHERE id = 1"
            ).scalar_one()
            == 11
        )
        intent = cx.execute(sa.select(project_creation_intents)).one()
        default = cx.exec_driver_sql(
            "SELECT allowed_services_default FROM organizations WHERE id = 1"
        ).scalar_one()
    assert intent.cover_seed == "k3x9"
    assert default == "approved_services"
    assert "network_default" not in _columns(engine, "organizations")
    assert "network" not in _columns(engine, "projects")
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
    assert listed.json()["editable"] is False
    assert owner.get("/api/organization/approved-endpoints").json()["editable"] is True
    assert (tmp_path / "data" / ".frisket" / "approved_endpoints.json").exists()


def test_retry_after_registration_returns_the_bundle_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first attempt registers the project, then fails before its
    receipt records the bundle; the retry must report what the bundle holds,
    which ``projects.sensitive`` alone cannot (on and inherit both read
    not sensitive)."""
    app = _team_app(tmp_path)
    client = TestClient(app, raise_server_exceptions=False)
    claim_server(app, client=client)
    sign_in_with_magic_link(app, client, "owner@example.com")
    original_complete = team_app._complete_project_intent

    def complete_then_crash(*args: Any, **kwargs: Any) -> dict[str, Any]:
        original_complete(*args, **kwargs)
        raise RuntimeError("simulated crash before the receipt is published")

    request = {
        "name": "Notes",
        "allowed_services": "approved_services",
        "usage_data_sharing": "on",
    }
    headers = {"Idempotency-Key": "crash-after-registration"}
    monkeypatch.setattr(team_app, "_complete_project_intent", complete_then_crash)
    crashed = client.post("/api/projects", json=request, headers=headers)
    assert crashed.status_code == 500
    monkeypatch.setattr(team_app, "_complete_project_intent", original_complete)

    retried = client.post("/api/projects", json=request, headers=headers)

    assert retried.status_code == 200, retried.text
    assert retried.json()["allowed_services"] == "approved_services"
    assert retried.json()["usage_data_sharing"] == "on"
    assert retried.json()["sensitive"] is False
