from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from frisket.server.app import create_app


PLUGIN_ID = "demo.env_settings"
PLUGIN_ROOT = (
    Path(__file__).parent.parent / "fixtures" / "local_plugins" / "demo_env_settings"
)


def _install_and_activate(client: TestClient, pid: str) -> None:
    installed = client.post(
        f"/api/projects/{pid}/workbench/plugins/{PLUGIN_ID}/install-local",
        json={
            "source": {"kind": "localPath", "value": str(PLUGIN_ROOT)},
            "arbitraryPackageLoadAllowed": False,
        },
    )
    assert installed.status_code == 200, installed.text
    activated = client.post(
        f"/api/projects/{pid}/workbench/plugins/{PLUGIN_ID}/activate",
        json={
            "receiptId": installed.json()["receiptId"],
            "trustAcknowledged": True,
            "permissionsAccepted": ["plugin:trusted_local_backend"],
            "arbitraryPackageLoadAllowed": False,
        },
    )
    assert activated.status_code == 200, activated.text


def test_workbench_plugin_settings_read_defaults_and_patch_project_values(
    tmp_path,
) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = client.post("/api/projects", json={"name": "Plugin settings"}).json()["id"]
    _install_and_activate(client, pid)

    initial = client.get(f"/api/projects/{pid}/workbench/plugins/{PLUGIN_ID}/settings")
    assert initial.status_code == 200, initial.text
    assert initial.json()["schemaVersion"] == "frisket.workbench_plugin_settings.v1"
    by_id = {item["id"]: item for item in initial.json()["settings"]}
    assert by_id["demo.env_settings.enabled"]["effectiveValue"] is True
    assert by_id["demo.env_settings.mode"]["effectiveValue"] == "summary"
    assert by_id["demo.env_settings.mode"]["source"] == "default"

    patched = client.patch(
        f"/api/projects/{pid}/workbench/plugins/{PLUGIN_ID}/settings",
        json={"values": {"demo.env_settings.mode": "detail"}},
    )
    assert patched.status_code == 200, patched.text
    mode = {item["id"]: item for item in patched.json()["settings"]}[
        "demo.env_settings.mode"
    ]
    assert mode["effectiveValue"] == "detail"
    assert mode["source"] == "project"


def test_workbench_plugin_settings_reject_unknown_and_secret_like_values(
    tmp_path,
) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = client.post("/api/projects", json={"name": "Plugin settings"}).json()["id"]
    _install_and_activate(client, pid)

    unknown = client.patch(
        f"/api/projects/{pid}/workbench/plugins/{PLUGIN_ID}/settings",
        json={"values": {"demo.env_settings.missing": "detail"}},
    )
    assert unknown.status_code == 422
    assert unknown.json()["detail"]["code"] == "invalid_plugin_settings"

    secret_like = client.patch(
        f"/api/projects/{pid}/workbench/plugins/{PLUGIN_ID}/settings",
        json={"values": {"demo.env_settings.mode": "api_key=secret"}},
    )
    assert secret_like.status_code == 422
    assert secret_like.json()["detail"]["code"] == "invalid_plugin_settings"
    assert "api_key=secret" not in secret_like.text
