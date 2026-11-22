"""The operator user listing and the browser user listing share one contract."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from frisket.contracts.http.admin_browser import AdminBrowserUsersV1
from frisket.team.app import TeamConfig, create_team_app
from tests.team_setup_helpers import claim_server


def _team_app(tmp_path: Path) -> tuple[Any, TestClient]:
    app = create_team_app(
        TeamConfig(
            database_url=f"sqlite:///{tmp_path / 'control.db'}",
            run_queue_database_url=f"sqlite:///{tmp_path / 'queue.db'}",
            data_dir=tmp_path / "data",
            base_url="http://testserver",
            organization_name="Contract Desk",
            magic_link_enabled=False,
        )
    )
    return app, claim_server(app, workspace_name="Contract Desk")


def test_operator_user_listing_is_an_exact_alias_of_the_browser_wire(
    tmp_path: Path,
) -> None:
    app, client = _team_app(tmp_path)

    shared = client.get("/api/admin/users")
    browser = client.get("/api/admin/browser/users")
    assert shared.status_code == browser.status_code == 200
    assert shared.content == browser.content
    payload = AdminBrowserUsersV1.model_validate_json(shared.content)
    assert payload.schema_version == "frisket.admin_users.v1"

    response_schema = app.openapi()["paths"]["/api/admin/users"]["get"]["responses"][
        "200"
    ]["content"]["application/json"]["schema"]
    assert "anyOf" not in response_schema

    user = client.get("/api/admin/users").json()["organizations"][0]["users"][0]
    assert "user_id" in user and "id" not in user
