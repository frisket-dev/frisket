from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from frisket.server.app import create_app


ROOT = Path(__file__).resolve().parents[2]
PLUGIN_ID = "demo.selection_summary"
PANEL_ID = "demo.selection_summary.panel.selection_summary"
COMPONENT_KEY = "trustedLocal.demoSelectionSummary.SelectionSummaryPanel"
PLUGIN_ROOT = ROOT / "tests/fixtures/local_plugins/demo_selection_summary"
PLUGIN_MANIFEST = PLUGIN_ROOT / "plugin.json"


def _activate_selection_summary_plugin(
    client: TestClient,
    project_id: str,
) -> dict[str, Any]:
    # install-local writes the workspace catalog identity that /activate
    # reads (a raw plugin.load action no longer populates it).
    installed = client.post(
        f"/api/projects/{project_id}/workbench/plugins/{PLUGIN_ID}/install-local",
        json={
            "source": {"kind": "localPath", "value": str(PLUGIN_MANIFEST)},
            "arbitraryPackageLoadAllowed": False,
        },
    )
    assert installed.status_code == 200, installed.text
    receipt_id = installed.json()["receiptId"]
    assert receipt_id

    activation_response = client.post(
        f"/api/projects/{project_id}/workbench/plugins/{PLUGIN_ID}/activate",
        json={
            "receiptId": receipt_id,
            "trustAcknowledged": True,
            "permissionsAccepted": [],
            "arbitraryPackageLoadAllowed": False,
        },
    )
    assert activation_response.status_code == 200, activation_response.text
    activation = activation_response.json()
    assert activation["installState"] == "enabled"
    assert activation["registryActivated"] is True
    return activation


def test_selection_summary_panel_descriptor_projects_from_runtime_index(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    project_id = client.post(
        "/api/projects", json={"name": "Selection summary panel"}
    ).json()["id"]

    activation = _activate_selection_summary_plugin(client, project_id)
    assert activation["pluginId"] == PLUGIN_ID

    index_response = client.get(f"/api/projects/{project_id}/workbench/plugins")
    assert index_response.status_code == 200, index_response.text
    runtime_index = index_response.json()
    plugin = next(
        item for item in runtime_index["plugins"] if item["pluginId"] == PLUGIN_ID
    )

    assert plugin["schemaVersion"] == "frisket.workbench_plugin_runtime_plugin.v1"
    assert plugin["installState"] == "enabled"
    assert plugin["activation"] == "registryManifestRegistered"
    assert plugin["contributionSummary"] == [
        {"kind": "workbench_panel", "count": 1, "ids": [PANEL_ID]},
    ]
    assert plugin["frontendComponentBindings"] == [
        {
            "schemaVersion": ("frisket.workbench_plugin_frontend_component_binding.v1"),
            "contributionId": PANEL_ID,
            "moduleKey": "trustedLocal.demoSelectionSummary",
            "componentKey": COMPONENT_KEY,
        }
    ]
    assert plugin["workbenchDescriptorPackage"] == {
        "schemaVersion": "frisket.workbench_descriptor_package.v1",
        "sourcePath": str(PLUGIN_ROOT / "workbench-descriptors.json"),
        "descriptorCount": 1,
        "runtimeOnlyFieldsStripped": ["componentKey"],
    }
    assert len(plugin["workbenchDescriptorManifests"]) == 1

    descriptor = plugin["workbenchDescriptorManifests"][0]
    assert descriptor["schemaVersion"] == "frisket.workbench.panel.v1"
    assert descriptor["id"] == PANEL_ID
    assert descriptor["kind"] == "panel"
    assert descriptor["ownerPluginId"] == PLUGIN_ID
    assert descriptor["title"] == "Selection summary"
    assert descriptor["placements"] == [
        {
            "host": "rightInspector",
            "mode": "panel",
            "slot": "inspection",
            "placementId": "demo-selection-summary-right-inspector",
            "order": 55,
        }
    ]
    assert descriptor["requires"] == [
        {"kind": "hostCapability", "id": "sheet.active"},
        {"kind": "hostCapability", "id": "selection.rows"},
        {"kind": "hostCapability", "id": "host.navigation.openRow"},
    ]
    assert descriptor["dataRequirements"] == [{"kind": "activeSheet"}]
    assert "componentKey" not in descriptor
    assert "appearsWhen" not in descriptor
