from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from frisket.authoring.plugin_registry import _reset_default_registry_for_tests
from frisket.authoring.workbench.plugin_runtime import ensure_workspace_plugin_packages
from frisket.server.app import create_app


ROOT = Path(__file__).parent.parent / "fixtures" / "local_plugins" / "frisket_geo_smoke"
PLUGIN_ID = "frisket.geosmoke"
TRUSTED_LOCAL_BACKEND_CAPABILITY = "plugin:trusted_local_backend"


def _client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(tmp_path / "workspace"))


def _project_id(client: TestClient, name: str = "local install plan") -> str:
    response = client.post("/api/projects", json={"name": name})
    assert response.status_code == 200, response.text
    return response.json()["id"]


def _local_install_plan(
    client: TestClient, project_id: str, manifest_path: Path
) -> Any:
    return client.post(
        f"/api/projects/{project_id}/workbench/plugins/{PLUGIN_ID}/install-local",
        json={
            "source": {"kind": "localPath", "value": str(manifest_path)},
            "arbitraryPackageLoadAllowed": False,
        },
    )


def test_local_install_plan_runs_plugin_load_and_hands_off_to_activation(
    tmp_path: Path,
) -> None:
    _reset_default_registry_for_tests()
    try:
        client = _client(tmp_path)
        project_id = _project_id(client)

        response = _local_install_plan(client, project_id, ROOT / "plugin.json")
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["schemaVersion"] == "frisket.plugin_install_plan_execution.v1"
        assert payload["projectId"] == project_id
        assert payload["pluginId"] == PLUGIN_ID
        assert payload["installState"] == "installed"
        assert payload["activation"] == "manifestLoaded"
        assert payload["runtimeSource"] == "plugin.load_receipt"
        assert payload["arbitraryPackageLoadAllowed"] is False
        assert payload["installFailure"] is None
        assert payload["source"] == {
            "kind": "localPath",
            "value": str(ROOT / "plugin.json"),
        }
        assert payload["receiptId"]
        assert payload["manifestSha256"].startswith("sha256:")
        assert payload["packageSha256"].startswith("sha256:")
        assert payload["workbenchDescriptorPackage"]["schemaVersion"] == (
            "frisket.workbench_descriptor_package.v1"
        )
        descriptor_manifest = next(
            item
            for item in payload["workbenchDescriptorManifests"]
            if item["id"] == "frisket.geosmoke.view.map"
        )
        assert descriptor_manifest["id"] == "frisket.geosmoke.view.map"
        assert descriptor_manifest["schemaVersion"] == "frisket.workbench.view.v1"
        assert descriptor_manifest["ownerPluginId"] == PLUGIN_ID
        assert descriptor_manifest["kind"] == "view"
        assert descriptor_manifest["placements"] == [
            {
                "host": "mainView",
                "mode": "pane",
                "slot": "work.companion",
                "placementId": "frisket-geo-map-companion",
            }
        ]

        index_response = client.get(f"/api/projects/{project_id}/workbench/plugins")
        assert index_response.status_code == 200, index_response.text
        index = index_response.json()
        plugin = index["plugins"][0]
        assert plugin["pluginId"] == PLUGIN_ID
        assert plugin["installState"] == "installed"
        assert plugin["activation"] == "manifestLoaded"
        assert plugin["receiptId"] == payload["receiptId"]
        assert plugin["packageSha256"] == payload["packageSha256"]
        assert plugin["registryActivated"] is False
        assert (
            next(
                item
                for item in plugin["workbenchDescriptorManifests"]
                if item["id"] == "frisket.geosmoke.view.map"
            )["id"]
            == "frisket.geosmoke.view.map"
        )

        activation = client.post(
            f"/api/projects/{project_id}/workbench/plugins/{PLUGIN_ID}/activate",
            json={
                "receiptId": payload["receiptId"],
                "trustAcknowledged": True,
                "permissionsAccepted": [TRUSTED_LOCAL_BACKEND_CAPABILITY],
                "arbitraryPackageLoadAllowed": False,
            },
        )
        assert activation.status_code == 200, activation.text
        assert activation.json()["installState"] == "enabled"
        assert activation.json()["packageSha256"] == payload["packageSha256"]
    finally:
        _reset_default_registry_for_tests()


def test_local_install_plan_persists_failed_descriptor_state(
    tmp_path: Path,
) -> None:
    _reset_default_registry_for_tests()
    try:
        plugin_dir = tmp_path / "bad_geo"
        plugin_dir.mkdir()
        manifest_path = plugin_dir / "plugin.json"
        manifest_path.write_text((ROOT / "plugin.json").read_text(encoding="utf-8"))
        descriptor_package = json.loads(
            (ROOT / "workbench-descriptors.json").read_text(encoding="utf-8")
        )
        descriptor_package["descriptors"][0]["id"] = (
            "frisket.geosmoke.view.not_declared"
        )
        (plugin_dir / "workbench-descriptors.json").write_text(
            json.dumps(descriptor_package), encoding="utf-8"
        )

        client = _client(tmp_path)
        project_id = _project_id(client, "local install failure")

        response = _local_install_plan(client, project_id, manifest_path)
        assert response.status_code == 409, response.text
        payload = response.json()
        assert payload["schemaVersion"] == "frisket.plugin_install_plan_execution.v1"
        assert payload["projectId"] == project_id
        assert payload["pluginId"] == PLUGIN_ID
        assert payload["installState"] == "failed"
        assert payload["activation"] == "failed"
        assert payload["receiptId"] is None
        assert payload["manifestSha256"] == ""
        assert payload["arbitraryPackageLoadAllowed"] is False
        assert payload["packageSha256"] == ""
        assert payload["source"] == {"kind": "localPath", "value": str(manifest_path)}
        assert payload["installFailure"]["code"] == (
            "invalid_workbench_descriptor_package"
        )
        assert payload["installFailure"]["retryable"] is True

        # Enablement ('failed') is the project row's; the package identity and
        # the failure detail are the workspace catalog's.
        project = client.app.state.workspace.get(project_id)
        row = project.db.execute(
            "SELECT plugin_id, install_state, activation "
            "FROM workbench_plugin_installs WHERE plugin_id=?",
            (PLUGIN_ID,),
        ).fetchone()
        assert row is not None
        assert row["install_state"] == "failed"
        assert row["activation"] == "failed"

        entry = client.app.state.workspace.plugin_package_catalog.get(PLUGIN_ID)
        assert entry is not None
        assert entry["receipt_id"] is None
        assert entry["manifest_sha256"] == ""
        assert entry["package_sha256"] == ""
        assert json.loads(entry["install_source"]) == {
            "kind": "localPath",
            "value": str(manifest_path),
        }
        stored_failure = json.loads(entry["install_failure"])
        assert stored_failure["code"] == "invalid_workbench_descriptor_package"

        index_response = client.get(f"/api/projects/{project_id}/workbench/plugins")
        assert index_response.status_code == 200, index_response.text
        failed_plugin = index_response.json()["plugins"][0]
        assert failed_plugin["pluginId"] == PLUGIN_ID
        assert failed_plugin["installState"] == "failed"
        assert failed_plugin["activation"] == "failed"
        assert failed_plugin["receiptId"] is None
        assert failed_plugin["installFailure"]["code"] == (
            "invalid_workbench_descriptor_package"
        )
        assert failed_plugin["registryActivated"] is False
    finally:
        _reset_default_registry_for_tests()


def test_failed_update_preserves_package_and_failed_reinstall_preserves_tombstone(
    tmp_path: Path,
) -> None:
    _reset_default_registry_for_tests()
    try:
        client = _client(tmp_path)
        project_id = _project_id(client, "staged update")
        installed = _local_install_plan(client, project_id, ROOT / "plugin.json")
        assert installed.status_code == 200, installed.text
        install_payload = installed.json()
        activated = client.post(
            f"/api/projects/{project_id}/workbench/plugins/{PLUGIN_ID}/activate",
            json={
                "receiptId": install_payload["receiptId"],
                "trustAcknowledged": True,
                "permissionsAccepted": [TRUSTED_LOCAL_BACKEND_CAPABILITY],
                "arbitraryPackageLoadAllowed": False,
            },
        )
        assert activated.status_code == 200, activated.text

        catalog = client.app.state.workspace.plugin_package_catalog
        before = catalog.get(PLUGIN_ID)
        assert before is not None
        failed_update = _local_install_plan(
            client, project_id, tmp_path / "missing" / "plugin.json"
        )
        assert failed_update.status_code == 409, failed_update.text
        after = catalog.get(PLUGIN_ID)
        assert after is not None
        for key in ("manifest_sha256", "package_sha256", "receipt_id", "manifest_ref"):
            assert after[key] == before[key]
        project = client.app.state.workspace.get(project_id)
        state = project.db.execute(
            "SELECT install_state FROM workbench_plugin_installs WHERE plugin_id=?",
            (PLUGIN_ID,),
        ).fetchone()
        assert state is not None and state["install_state"] == "enabled"

        uninstalled = client.post(
            f"/api/projects/{project_id}/workbench/plugins/{PLUGIN_ID}/uninstall"
        )
        assert uninstalled.status_code == 200, uninstalled.text
        assert catalog.get(PLUGIN_ID) is None
        assert catalog.is_deleted(PLUGIN_ID)

        failed_reinstall = _local_install_plan(
            client, project_id, tmp_path / "still-missing" / "plugin.json"
        )
        assert failed_reinstall.status_code == 409, failed_reinstall.text
        assert catalog.get(PLUGIN_ID) is None
        assert catalog.is_deleted(PLUGIN_ID)
        _reset_default_registry_for_tests()
        ensure_workspace_plugin_packages(client.app.state.workspace.root)
        assert catalog.get(PLUGIN_ID) is None
    finally:
        _reset_default_registry_for_tests()
