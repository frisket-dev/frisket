"""Team-edition org network default: the org
default lives on ``orgs.network_default``, the project mode on
``projects.network``, and both are materialized into the project bundle (the
``sensitive``-sync precedent) so the bundle-side gate resolves ``inherit``
without a control-plane handle."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from frisket.team.app import TeamConfig, create_team_app
from frisket.team.schema import projects
from tests.team_setup_helpers import claim_server


def _config(tmp_path: Path) -> TeamConfig:
    return TeamConfig(
        database_url=f"sqlite:///{tmp_path / 'control.db'}",
        run_queue_database_url=f"sqlite:///{tmp_path / 'run-queue.db'}",
        data_dir=tmp_path / "data",
        base_url="http://testserver",
        organization_name="Investigations Desk",
        admin_emails={"owner@example.com"},
    )


async def _send_magic_email(_email: str, _link: str) -> bool:
    return True


@pytest.fixture
def team(tmp_path) -> tuple[Any, Any]:
    app = create_team_app(_config(tmp_path), send_magic_email=_send_magic_email)
    browser = claim_server(app)
    return app, browser


def test_org_default_flows_into_bundles_and_project_mode_overrides(team) -> None:
    app, browser = team
    pid = browser.post("/api/projects", json={"name": "Leak Sheet"}).json()["id"]
    project = app.state.workspace.get(pid)

    # Un-configured: org default unset, effective on.
    assert browser.get("/api/org/network").json() == {
        "network_default": None,
        "effective_default": "on",
    }
    assert project.effective_network_policy() == "on"

    # Org flips the default off: the inherit-mode project follows, because
    # the default is materialized into its bundle.
    response = browser.patch("/api/org/network", json={"network_default": "off"})
    assert response.status_code == 200, response.text
    assert project.network_policy() == {"mode": "inherit", "org_default": "off"}
    assert project.effective_network_policy() == "off"

    # Project owner overrides to on: project mode beats the org default,
    # and the control-plane row records the mode.
    response = browser.patch(f"/api/projects/{pid}/network", json={"mode": "on"})
    assert response.status_code == 200, response.text
    assert response.json()["effective"] == "on"
    assert project.effective_network_policy() == "on"
    with app.state.control_engine.connect() as cx:
        stored = cx.execute(
            sa.select(projects.c.network).where(projects.c.slug == pid)
        ).scalar_one()
    assert stored == "on"

    # Back to inherit: the org off applies again.
    browser.patch(f"/api/projects/{pid}/network", json={"mode": "inherit"})
    assert project.effective_network_policy() == "off"

    # Clearing the org default restores the built-in on.
    browser.patch("/api/org/network", json={"network_default": None})
    assert project.effective_network_policy() == "on"


def test_org_and_project_network_routes_validate_input(team) -> None:
    app, browser = team
    pid = browser.post("/api/projects", json={"name": "Validation"}).json()["id"]
    assert (
        browser.patch("/api/org/network", json={"network_default": "inherit"})
    ).status_code == 400
    assert (
        browser.patch(f"/api/projects/{pid}/network", json={"mode": "maybe"})
    ).status_code == 400
    assert (
        browser.patch("/api/projects/nope/network", json={"mode": "off"})
    ).status_code == 404
