"""Public HTTP boundaries for Solo's project-scoped MCP configuration."""

from __future__ import annotations

from fastapi.testclient import TestClient

from frisket.server.app import create_app
from frisket.team.app import TeamConfig, create_team_app
from tests.team_setup_helpers import claim_server


def test_local_mcp_server_routes_are_typed_and_team_does_not_inherit_them(
    tmp_path,
) -> None:
    local = TestClient(create_app(tmp_path / "local-workspace"))
    pid = local.post("/api/projects", json={"name": "Local MCP"}).json()["id"]
    document = local.app.openapi()
    expected = {
        ("/api/projects/{pid}/mcp-servers", "get"),
        ("/api/projects/{pid}/mcp-servers", "post"),
        ("/api/projects/{pid}/mcp-servers/import", "post"),
        ("/api/projects/{pid}/mcp-servers/{server_id}", "patch"),
        ("/api/projects/{pid}/mcp-servers/{server_id}", "delete"),
        ("/api/projects/{pid}/mcp-servers/{server_id}/test", "post"),
    }
    assert all(
        "200" in document["paths"][path][method]["responses"]
        for path, method in expected
    )

    # Bodies are strict at this boundary: a typo must not persist an inert
    # local executable configuration.  The command is an argv executable, not
    # shell input, so it is required rather than inferred from arbitrary text.
    malformed = local.post(
        f"/api/projects/{pid}/mcp-servers",
        json={"name": "Bad", "commmand": "uvx"},
    )
    assert malformed.status_code == 422

    team = create_team_app(
        TeamConfig(
            database_url=f"sqlite:///{tmp_path / 'control.db'}",
            run_queue_database_url=f"sqlite:///{tmp_path / 'queue.db'}",
            data_dir=tmp_path / "team-data",
            base_url="http://testserver",
            organization_name="No team MCP",
            admin_emails={"owner@example.com"},
        )
    )
    browser = claim_server(team)
    team_pid = browser.post("/api/projects", json={"name": "Team MCP"}).json()["id"]
    refused = browser.get(f"/api/projects/{team_pid}/mcp-servers")
    assert refused.status_code == 404
