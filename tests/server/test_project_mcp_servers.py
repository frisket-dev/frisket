"""Solo project MCP-server settings are a deliberately local-only surface."""

from __future__ import annotations

import pytest

from fastapi.testclient import TestClient

from frisket.server.app import create_app


def _project(client: TestClient) -> str:
    response = client.post("/api/projects", json={"name": "MCP settings"})
    assert response.status_code == 200, response.text
    return response.json()["id"]


def test_project_mcp_server_crud_binds_env_by_value_or_project_secret(tmp_path) -> None:
    """Configuration is project-scoped, revisioned, and never reads secrets back."""
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = _project(client)
    secret = "crm-token-that-must-not-reach-the-settings-api"
    saved_secret = client.post(
        f"/api/projects/{pid}/secrets",
        json={"name": "CRM_TOKEN", "value": secret},
    )
    assert saved_secret.status_code == 200, saved_secret.text
    saved_replacement_secret = client.post(
        f"/api/projects/{pid}/secrets",
        json={"name": "REPLACEMENT_TOKEN", "value": "replacement-secret"},
    )
    assert saved_replacement_secret.status_code == 200, saved_replacement_secret.text

    created = client.post(
        f"/api/projects/{pid}/mcp-servers",
        json={
            "name": "Local CRM",
            "command": "mcp-test-command-that-does-not-exist",
            "args": ["--safe-mode"],
            "cwd": "/tmp",
            "env": {
                "LOG_LEVEL": {"value": "info"},
                "CRM_TOKEN": {"project_secret": "CRM_TOKEN"},
            },
        },
    )
    assert created.status_code == 200, created.text
    server = created.json()
    server_id = server["id"]
    assert server["name"] == "Local CRM"
    assert server["command"] == "mcp-test-command-that-does-not-exist"
    assert server["args"] == ["--safe-mode"]
    assert server["cwd"] == "/tmp"
    assert server["env"] == {
        "LOG_LEVEL": {"value": "info"},
        "CRM_TOKEN": {"project_secret": "CRM_TOKEN"},
    }
    assert server["enabled"] is True
    assert server["revision"] == 1
    assert server["lastTest"] is None
    assert secret not in created.text
    secrets_by_name = {
        row["name"]: row
        for row in client.get(f"/api/projects/{pid}/secrets").json()["secrets"]
    }
    assert secrets_by_name["CRM_TOKEN"]["consumers"] == [
        {"kind": "mcp_connector", "id": server_id}
    ]

    # Starting an unavailable program is a recorded test failure, not a 500
    # and not an accidental successful health claim.  Diagnostics are a
    # public response, so they must carry neither project-secret plaintext nor
    # its arbitrary configured value.
    tested = client.post(f"/api/projects/{pid}/mcp-servers/{server_id}/test")
    assert tested.status_code == 200, tested.text
    assert tested.json()["id"] == server_id
    assert tested.json()["lastTest"]["status"] == "failed"
    assert secret not in tested.text

    updated = client.patch(
        f"/api/projects/{pid}/mcp-servers/{server_id}",
        json={
            "name": "CRM, updated",
            "args": ["--read-only"],
            "env": {"CRM_TOKEN": {"project_secret": "REPLACEMENT_TOKEN"}},
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["id"] == server_id
    assert updated.json()["revision"] == 2
    assert updated.json()["lastTest"] is None
    secrets_by_name = {
        row["name"]: row
        for row in client.get(f"/api/projects/{pid}/secrets").json()["secrets"]
    }
    assert secrets_by_name["CRM_TOKEN"]["consumers"] == []
    assert secrets_by_name["REPLACEMENT_TOKEN"]["consumers"] == [
        {"kind": "mcp_connector", "id": server_id}
    ]

    disabled = client.patch(
        f"/api/projects/{pid}/mcp-servers/{server_id}", json={"enabled": False}
    )
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["id"] == server_id
    assert disabled.json()["revision"] == 3
    assert disabled.json()["enabled"] is False

    listed = client.get(f"/api/projects/{pid}/mcp-servers")
    assert listed.status_code == 200, listed.text
    assert listed.json()["schemaVersion"] == "frisket.project_mcp_servers.v1"
    assert listed.json()["projectId"] == pid
    assert listed.json()["servers"] == [disabled.json()]
    assert secret not in listed.text

    deleted = client.delete(f"/api/projects/{pid}/mcp-servers/{server_id}")
    assert deleted.status_code == 200, deleted.text
    assert deleted.json() == {"ok": True, "deleted": True, "id": server_id}
    assert client.get(f"/api/projects/{pid}/mcp-servers").json()["servers"] == []
    secrets_by_name = {
        row["name"]: row
        for row in client.get(f"/api/projects/{pid}/secrets").json()["secrets"]
    }
    assert secrets_by_name["REPLACEMENT_TOKEN"]["consumers"] == []


@pytest.mark.parametrize("field_name", ["name", "command", "args", "env", "enabled"])
def test_project_mcp_server_patch_rejects_null_required_fields_before_persistence(
    tmp_path, field_name: str
) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = _project(client)
    created = client.post(
        f"/api/projects/{pid}/mcp-servers",
        json={"name": "Local tools", "command": "uvx", "cwd": "/tmp"},
    )
    assert created.status_code == 200, created.text
    server = created.json()

    refused = client.patch(
        f"/api/projects/{pid}/mcp-servers/{server['id']}",
        json={field_name: None},
    )

    assert refused.status_code == 422, refused.text
    listed = client.get(f"/api/projects/{pid}/mcp-servers")
    assert listed.status_code == 200, listed.text
    assert listed.json()["servers"] == [server]


def test_project_mcp_server_patch_allows_null_to_clear_cwd(tmp_path) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = _project(client)
    created = client.post(
        f"/api/projects/{pid}/mcp-servers",
        json={"name": "Local tools", "command": "uvx", "cwd": "/tmp"},
    )
    assert created.status_code == 200, created.text
    server = created.json()

    cleared = client.patch(
        f"/api/projects/{pid}/mcp-servers/{server['id']}", json={"cwd": None}
    )

    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["cwd"] is None
    assert cleared.json()["revision"] == server["revision"] + 1


def test_project_mcp_server_import_accepts_the_common_stdio_shape_only(
    tmp_path,
) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = _project(client)

    imported = client.post(
        f"/api/projects/{pid}/mcp-servers/import",
        json={
            "mcpServers": {
                "Document tools": {
                    "command": "uvx",
                    "args": ["document-tools-mcp"],
                }
            }
        },
    )
    assert imported.status_code == 200, imported.text
    assert imported.json()["schemaVersion"] == "frisket.project_mcp_servers.v1"
    assert len(imported.json()["servers"]) == 1
    server = imported.json()["servers"][0]
    assert server["name"] == "Document tools"
    assert server["command"] == "uvx"
    assert server["args"] == ["document-tools-mcp"]
    assert server["enabled"] is True
    assert server["revision"] == 1
    assert server["lastTest"] is None

    # Solo supports local stdio only.  Treating an imported URL as if it were
    # a command would silently expand the feature into HTTP MCP transport.
    refused = client.post(
        f"/api/projects/{pid}/mcp-servers/import",
        json={"mcpServers": {"Remote": {"url": "https://mcp.example.test"}}},
    )
    assert refused.status_code == 422
    unclassified_env = client.post(
        f"/api/projects/{pid}/mcp-servers/import",
        json={
            "mcpServers": {
                "Unclassified": {"command": "uvx", "env": {"TOKEN": "value"}}
            }
        },
    )
    assert unclassified_env.status_code == 422

    explicit_env = client.post(
        f"/api/projects/{pid}/mcp-servers/import",
        json={
            "mcpServers": {
                "Classified": {
                    "command": "uvx",
                    "env": {"LOG_LEVEL": {"value": "debug"}},
                }
            }
        },
    )
    assert explicit_env.status_code == 200, explicit_env.text
    assert explicit_env.json()["servers"][-1]["env"] == {
        "LOG_LEVEL": {"value": "debug"}
    }
