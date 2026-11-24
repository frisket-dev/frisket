"""Team project creation slugs any name, and recovery never blocks startup."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from frisket.project_identity import validate_project_slug
from frisket.team.app import TeamConfig, create_team_app
from frisket.team.schema import project_creation_intents
from tests.team_setup_helpers import claim_server, sign_in_with_magic_link


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


@pytest.mark.parametrize("name", ["Café", "東京 notes", "ⅷ ½ ²", "İstanbul"])
@pytest.mark.parametrize("idempotent", [False, True])
def test_non_ascii_project_names_create_valid_slugs(
    tmp_path: Path, name: str, idempotent: bool
) -> None:
    app = _team_app(tmp_path)
    client = TestClient(app)
    claim_server(app, client=client)
    sign_in_with_magic_link(app, client, "owner@example.com")
    headers = {"idempotency-key": "create-once"} if idempotent else {}
    made = client.post("/api/projects", json={"name": name}, headers=headers)
    assert made.status_code == 200, made.text
    assert made.json()["name"] == name
    validate_project_slug(made.json()["id"])
    with app.state.control_engine.connect() as cx:
        assert cx.execute(sa.select(project_creation_intents)).all() == []
    assert _team_app(tmp_path) is not None


def test_unrecoverable_creation_intent_is_quarantined_not_raised(
    tmp_path: Path,
) -> None:
    app = _team_app(tmp_path)
    client = TestClient(app)
    claim_server(app, client=client)
    sign_in_with_magic_link(app, client, "owner@example.com")
    with app.state.control_engine.begin() as cx:
        organization_id = cx.execute(
            sa.text("SELECT id FROM organizations")
        ).scalar_one()
        user_id = cx.execute(sa.text("SELECT id FROM users")).scalar_one()
        cx.execute(
            project_creation_intents.insert().values(
                slug="café-0123456789ab",
                organization_id=organization_id,
                creator_user_id=user_id,
                name="Café",
                sensitive=False,
            )
        )
    restarted = _team_app(tmp_path)
    with restarted.state.control_engine.connect() as cx:
        remaining = cx.execute(sa.select(project_creation_intents)).all()
    assert remaining == []
    fresh = TestClient(restarted)
    sign_in_with_magic_link(restarted, fresh, "owner@example.com")
    assert fresh.get("/api/projects").status_code == 200
