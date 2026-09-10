"""The Team and browser admin paths share one strict response contract."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from frisket.contracts.http.admin_operations import (
    AdminAuditResponse,
    AdminErrorsResponse,
    AdminJobsResponse,
    AdminUsersResponse,
)
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


def test_admin_paths_are_exact_aliases_of_the_canonical_browser_wire(
    tmp_path: Path,
) -> None:
    app, client = _team_app(tmp_path)
    client.post("/api/client-errors", json={"message": "contract fixture"})

    cases = (
        ("users", AdminUsersResponse, "frisket.admin_users.v1"),
        ("jobs", AdminJobsResponse, "frisket.admin_jobs.v1"),
        ("audit", AdminAuditResponse, "frisket.admin_audit.v1"),
        ("errors", AdminErrorsResponse, "frisket.admin_errors.v1"),
    )
    openapi = app.openapi()
    for name, contract, version in cases:
        shared = client.get(f"/api/admin/{name}")
        browser = client.get(f"/api/admin/browser/{name}")
        assert shared.status_code == browser.status_code == 200
        assert shared.content == browser.content
        payload = contract.model_validate_json(shared.content)
        assert payload.schema_version == version

        response_schema = openapi["paths"][f"/api/admin/{name}"]["get"]["responses"][
            "200"
        ]["content"]["application/json"]["schema"]
        assert "anyOf" not in response_schema

    user = client.get("/api/admin/users").json()["orgs"][0]["users"][0]
    assert "user_id" in user and "id" not in user
    assert all(
        "project_id" in project and "id" not in project
        for project in client.get("/api/admin/audit").json()["filters"]["projects"]
    )


def test_touched_admin_role_request_is_strict_and_closed(tmp_path: Path) -> None:
    _app, client = _team_app(tmp_path)
    user_id = client.get("/api/admin/users").json()["orgs"][0]["users"][0]["user_id"]

    assert (
        client.patch(
            f"/api/admin/users/{user_id}/role",
            json={"role": "owner", "legacy": True},
        ).status_code
        == 422
    )
    assert (
        client.patch(f"/api/admin/users/{user_id}/role", json={"role": 1}).status_code
        == 422
    )
