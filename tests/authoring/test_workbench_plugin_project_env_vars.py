from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from frisket.team.security.secrets import decrypt_secret
from frisket.server.app import create_app


PLUGIN_ID = "demo.env_settings"
PLUGIN_CAPABILITY = "plugin:trusted_local_backend"
PLUGIN_ROOT = (
    Path(__file__).parent.parent / "fixtures" / "local_plugins" / "demo_env_settings"
)


def _install_and_activate_env_plugin(client: TestClient, project_id: str) -> None:
    installed = client.post(
        f"/api/projects/{project_id}/workbench/plugins/{PLUGIN_ID}/install-local",
        json={
            "source": {"kind": "localPath", "value": str(PLUGIN_ROOT)},
            "arbitraryPackageLoadAllowed": False,
        },
    )
    assert installed.status_code == 200, installed.text
    install_body = installed.json()
    assert install_body["installState"] == "installed"
    assert install_body["receiptId"]

    activated = client.post(
        f"/api/projects/{project_id}/workbench/plugins/{PLUGIN_ID}/activate",
        json={
            "receiptId": install_body["receiptId"],
            "trustAcknowledged": True,
            "permissionsAccepted": [PLUGIN_CAPABILITY],
            "arbitraryPackageLoadAllowed": False,
        },
    )
    assert activated.status_code == 200, activated.text
    assert activated.json()["installState"] == "enabled"


def _env_list(client: TestClient, project_id: str) -> dict[str, Any]:
    response = client.get(
        f"/api/projects/{project_id}/workbench/plugins/{PLUGIN_ID}/env"
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_plugin_project_env_vars_are_project_scoped_encrypted_and_redacted(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    project_a = client.post("/api/projects", json={"name": "Plugin env A"}).json()["id"]
    project_b = client.post("/api/projects", json={"name": "Plugin env B"}).json()["id"]
    _install_and_activate_env_plugin(client, project_a)

    missing = client.get(f"/api/projects/{project_b}/workbench/plugins/{PLUGIN_ID}/env")
    assert missing.status_code == 404, missing.text
    assert missing.json()["detail"]["code"] == "plugin_env_plugin_not_installed"

    initial = _env_list(client, project_a)
    assert initial == {
        "schemaVersion": "frisket.workbench_plugin_env_vars.v1",
        "projectId": project_a,
        "pluginId": PLUGIN_ID,
        "env": [
            {
                "schemaVersion": "frisket.workbench_plugin_env_var.v1",
                "projectId": project_a,
                "pluginId": PLUGIN_ID,
                "name": "DEMO_API_KEY",
                "hint": None,
                "required": True,
                "configured": False,
                "updatedAt": None,
            }
        ],
    }

    secret_value = "stage3-secret-value"
    malformed_requests = [
        client.post(
            f"/api/projects/{project_a}/workbench/plugins/{PLUGIN_ID}/env",
            json={"value": secret_value},
        ),
        client.post(
            f"/api/projects/{project_a}/workbench/plugins/{PLUGIN_ID}/env",
            headers={"content-type": "application/json"},
            content=secret_value,
        ),
        client.post(
            f"/api/projects/{project_a}/workbench/plugins/{PLUGIN_ID}/env",
            json={"name": "DEMO_API_KEY", "value": {"token": secret_value}},
        ),
    ]
    for response in malformed_requests:
        assert response.status_code == 400, response.text
        assert response.json()["detail"]["code"] == "plugin_env_body_invalid"
        assert secret_value not in response.text

    invalid = client.post(
        f"/api/projects/{project_a}/workbench/plugins/{PLUGIN_ID}/env",
        json={"name": "DEMO-API-KEY", "value": "not-used"},
    )
    assert invalid.status_code == 400, invalid.text
    assert invalid.json()["detail"]["code"] == "plugin_env_name_invalid"

    saved = client.post(
        f"/api/projects/{project_a}/workbench/plugins/{PLUGIN_ID}/env",
        json={"name": "demo_api_key", "value": secret_value},
    )
    assert saved.status_code == 200, saved.text
    saved_body = saved.json()
    assert saved_body == {
        "schemaVersion": "frisket.workbench_plugin_env_var.v1",
        "projectId": project_a,
        "pluginId": PLUGIN_ID,
        "name": "DEMO_API_KEY",
        "hint": "...alue",
        "required": True,
        "configured": True,
        "updatedAt": saved_body["updatedAt"],
    }
    assert secret_value not in saved.text

    project = client.app.state.workspace.get(project_a)
    row = project.db.execute(
        "SELECT encrypted, hint FROM workbench_plugin_env_vars "
        "WHERE plugin_id=? AND name=?",
        (PLUGIN_ID, "DEMO_API_KEY"),
    ).fetchone()
    assert row is not None
    encrypted = str(row["encrypted"])
    assert encrypted != secret_value
    assert secret_value not in encrypted
    assert row["hint"] == "...alue"
    assert decrypt_secret(encrypted) == secret_value

    listed = _env_list(client, project_a)
    rendered = json.dumps(listed, sort_keys=True)
    assert secret_value not in rendered
    assert listed["env"][0]["configured"] is True
    assert listed["env"][0]["hint"] == "...alue"

    _install_and_activate_env_plugin(client, project_b)
    project_b_list = _env_list(client, project_b)
    assert project_b_list["env"][0]["configured"] is False
    assert project_b_list["env"][0]["hint"] is None

    deleted = client.delete(
        f"/api/projects/{project_a}/workbench/plugins/{PLUGIN_ID}/env/DEMO_API_KEY"
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json() == {
        "ok": True,
        "deleted": True,
        "projectId": project_a,
        "pluginId": PLUGIN_ID,
        "name": "DEMO_API_KEY",
    }
    after_delete = _env_list(client, project_a)
    assert after_delete["env"][0]["configured"] is False
    assert after_delete["env"][0]["hint"] is None
