from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from frisket.authoring.plugin_registry import _reset_default_registry_for_tests
from frisket.server.app import create_app


ROOT = Path(__file__).parent.parent / "fixtures" / "local_plugins" / "frisket_geo_smoke"
PLUGIN_ID = "frisket.geosmoke"
CAPABILITY = "plugin:trusted_local_backend"


def _client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(tmp_path / "workspace"))


def _project_id(client: TestClient) -> str:
    response = client.post(
        "/api/projects", json={"name": "Workbench plugin lifecycle product"}
    )
    assert response.status_code == 200, response.text
    return response.json()["id"]


def _plugin_from_index(client: TestClient, project_id: str) -> dict:
    response = client.get(f"/api/projects/{project_id}/workbench/plugins")
    assert response.status_code == 200, response.text
    plugins = response.json()["plugins"]
    assert len(plugins) == 1
    return plugins[0]


def test_trusted_local_plugin_lifecycle_is_backend_durable_product_flow(
    tmp_path: Path,
) -> None:
    _reset_default_registry_for_tests()
    try:
        client = _client(tmp_path)
        project_id = _project_id(client)
        manifest_path = ROOT / "plugin.json"

        install = client.post(
            f"/api/projects/{project_id}/workbench/plugins/{PLUGIN_ID}/install-local",
            json={
                "source": {"kind": "localPath", "value": str(manifest_path)},
                "arbitraryPackageLoadAllowed": False,
            },
        )
        assert install.status_code == 200, install.text
        installed = install.json()
        assert installed["schemaVersion"] == "frisket.plugin_install_plan_execution.v1"
        assert installed["pluginId"] == PLUGIN_ID
        assert installed["installState"] == "installed"
        assert installed["activation"] == "manifestLoaded"
        assert installed["runtimeSource"] == "plugin.load_receipt"
        assert installed["arbitraryPackageLoadAllowed"] is False
        assert installed["installFailure"] is None
        assert installed["receiptId"]
        assert installed["manifestSha256"].startswith("sha256:")
        assert (
            next(
                item
                for item in installed["workbenchDescriptorManifests"]
                if item["id"] == "frisket.geosmoke.view.map"
            )["id"]
            == "frisket.geosmoke.view.map"
        )

        indexed = _plugin_from_index(client, project_id)
        assert indexed["installState"] == "installed"
        assert indexed["activation"] == "manifestLoaded"
        assert indexed["registryActivated"] is False
        assert indexed["receiptId"] == installed["receiptId"]

        activation = client.post(
            f"/api/projects/{project_id}/workbench/plugins/{PLUGIN_ID}/activate",
            json={
                "receiptId": installed["receiptId"],
                "trustAcknowledged": True,
                "permissionsAccepted": [CAPABILITY],
                "arbitraryPackageLoadAllowed": False,
            },
        )
        assert activation.status_code == 200, activation.text
        activated = activation.json()
        assert activated["schemaVersion"] == "frisket.workbench_plugin_activation.v1"
        assert activated["installState"] == "enabled"
        assert activated["activation"] == "registryManifestRegistered"
        assert activated["registryActivated"] is True
        assert activated["arbitraryPackageLoadAllowed"] is False

        enabled_index = _plugin_from_index(client, project_id)
        assert enabled_index["installState"] == "enabled"
        assert enabled_index["registryActivated"] is True
        assert (
            next(
                item
                for item in enabled_index["frontendComponentBindings"]
                if item["contributionId"] == "frisket.geosmoke.view.map"
            )["contributionId"]
            == "frisket.geosmoke.view.map"
        )

        disabled_response = client.post(
            f"/api/projects/{project_id}/workbench/plugins/{PLUGIN_ID}/disable"
        )
        assert disabled_response.status_code == 200, disabled_response.text
        disabled = disabled_response.json()
        assert disabled["schemaVersion"] == "frisket.workbench_plugin_install_state.v1"
        assert disabled["installState"] == "disabled"
        assert disabled["activation"] == "blocked"
        assert disabled["disabledReason"] == "plugin_disabled"
        assert disabled["registryActivated"] is False

        disabled_index = _plugin_from_index(client, project_id)
        assert disabled_index["installState"] == "disabled"
        assert disabled_index["activation"] == "blocked"
        assert disabled_index["registryActivated"] is False

        uninstalled_response = client.post(
            f"/api/projects/{project_id}/workbench/plugins/{PLUGIN_ID}/uninstall"
        )
        assert uninstalled_response.status_code == 200, uninstalled_response.text
        uninstalled = uninstalled_response.json()
        assert uninstalled["installState"] == "uninstalled"
        assert uninstalled["activation"] == "removed"
        assert uninstalled["registryActivated"] is False

        uninstalled_index = client.get(f"/api/projects/{project_id}/workbench/plugins")
        assert uninstalled_index.status_code == 200, uninstalled_index.text
        assert uninstalled_index.json()["plugins"] == []
        assert client.app.state.workspace.plugin_package_catalog.get(PLUGIN_ID) is None
        assert client.app.state.workspace.plugin_package_catalog.is_deleted(PLUGIN_ID)

        receipt = (
            client.app.state.workspace.get(project_id)
            .db.execute("SELECT id FROM receipts WHERE id=?", (installed["receiptId"],))
            .fetchone()
        )
        assert receipt is not None
    finally:
        _reset_default_registry_for_tests()
