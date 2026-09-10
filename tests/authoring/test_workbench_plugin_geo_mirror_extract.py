from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from frisket.authoring.plugin_registry import (
    _reset_default_registry_for_tests,
    default_registry,
    get_trusted_backend_handler,
)
from frisket.engine.projections.runtime import projection_runtime_binding
from frisket.server.app import create_app
from frisket.engine.store import Project


ROOT = Path(__file__).parent.parent / "fixtures" / "local_plugins" / "frisket_geo_smoke"
PLUGIN_ID = "frisket.geosmoke"
TRUSTED_LOCAL_BACKEND_CAPABILITY = "plugin:trusted_local_backend"
# The twin owns its OWN projection kind in the frisket.geosmoke namespace; the
# map/points route resolves the binding by its execution role (=map_points),
# not by the bundled plugin's canonical projection-kind string.
SMOKE_PROJECTION_KIND = "frisket.geosmoke.projection.map_points"
SMOKE_HANDLER_KEY = "frisket.geosmoke:real_local_smoke_map_points"
SMOKE_ACTION_KIND = "frisket.geosmoke.real_local_smoke"
SMOKE_ACTION_HANDLER_KEY = "frisket.geosmoke:real_local_smoke"
SMOKE_IMPORTER_KIND = "frisket_geosmoke_real_local_smoke_import"
SMOKE_IMPORTER_HANDLER_KEY = "frisket.geosmoke:real_local_smoke_importer"
SMOKE_OPERATOR_KIND = "frisket.geosmoke.operator.real_local_smoke_equals"
SMOKE_OPERATOR_HANDLER_KEY = "frisket.geosmoke:real_local_smoke_operator"


def _create_geo_project(tmp_path: Path) -> tuple[TestClient, str, int, int, list[int]]:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    project_id = "geo-mirror-extract"
    project = Project.create(workspace / f"{project_id}.frisket", name="Geo mirror")
    sheet_id = project.add_sheet("places")
    name_col = project.add_column(sheet_id, "name", type="text")
    geo_col = project.add_column(sheet_id, "point", type="geo_point")
    rows = project.add_rows(
        sheet_id,
        [
            {"name": "NYC", "point": {"lat": 40.7128, "lon": -74.006}},
            {"name": "Boston", "point": {"lat": 42.3601, "lon": -71.0589}},
        ],
        {"name": name_col, "point": geo_col},
    )
    project.close()
    return TestClient(create_app(workspace)), project_id, sheet_id, geo_col, rows


def _load_activate_and_enable_backend(
    client: TestClient,
    project_id: str,
) -> tuple[str, dict[str, Any]]:
    # install-local writes the workspace catalog identity that /activate
    # reads (a raw plugin.load action no longer populates it).
    installed = client.post(
        f"/api/projects/{project_id}/workbench/plugins/{PLUGIN_ID}/install-local",
        json={
            "source": {"kind": "localPath", "value": str(ROOT / "plugin.json")},
            "arbitraryPackageLoadAllowed": False,
        },
    )
    assert installed.status_code == 200, installed.text
    receipt_id = str(installed.json()["receiptId"])

    activate_response = client.post(
        f"/api/projects/{project_id}/workbench/plugins/{PLUGIN_ID}/activate",
        json={
            "receiptId": receipt_id,
            "trustAcknowledged": True,
            "permissionsAccepted": [TRUSTED_LOCAL_BACKEND_CAPABILITY],
            "arbitraryPackageLoadAllowed": False,
        },
    )
    assert activate_response.status_code == 200, activate_response.text
    assert activate_response.json()["registryActivated"] is True

    backend_response = client.post(
        f"/api/projects/{project_id}/workbench/plugins/{PLUGIN_ID}/backend/activate",
        json={
            "trustAcknowledged": True,
            "arbitraryPackageLoadAllowed": False,
            "executableHandlersAllowed": True,
        },
    )
    assert backend_response.status_code == 200, backend_response.text
    return receipt_id, backend_response.json()


def test_geo_fixture_projects_descriptors_and_native_runtime_bindings(
    tmp_path: Path,
) -> None:
    _reset_default_registry_for_tests()
    try:
        client, project_id, sheet_id, geo_col, rows = _create_geo_project(tmp_path)
        receipt_id, backend_payload = _load_activate_and_enable_backend(
            client,
            project_id,
        )

        index_response = client.get(f"/api/projects/{project_id}/workbench/plugins")
        assert index_response.status_code == 200, index_response.text
        index = index_response.json()
        plugin = next(
            item for item in index["plugins"] if item["pluginId"] == PLUGIN_ID
        )
        assert plugin["runtimeSource"] == "plugin.load_receipt"
        assert plugin["receiptId"] == receipt_id
        assert plugin["workbenchDescriptorPackage"] == {
            "schemaVersion": "frisket.workbench_descriptor_package.v1",
            "sourcePath": str(ROOT / "workbench-descriptors.json"),
            "descriptorCount": 2,
            "runtimeOnlyFieldsStripped": ["componentKey"],
        }
        descriptor_manifests = {
            descriptor["id"]: descriptor
            for descriptor in plugin["workbenchDescriptorManifests"]
        }
        assert set(descriptor_manifests) == {
            "frisket.geosmoke.view.real_local_smoke",
            "frisket.geosmoke.view.map",
        }
        # Pin revision (geo-bundled-plugin-v1): the smoke twin's map view now
        # mirrors the REAL bundled package's descriptor shape — a projection
        # view (projectionKind) scoped to geo_point via the contract
        # vocabulary (sheetHasColumnType), matching
        # src/frisket/authoring/bundled_plugins/frisket.geo/workbench-descriptors.json.
        assert descriptor_manifests["frisket.geosmoke.view.map"] == {
            "schemaVersion": "frisket.workbench.view.v1",
            "id": "frisket.geosmoke.view.map",
            "kind": "view",
            "ownerPluginId": PLUGIN_ID,
            "title": "Map",
            "projectionKind": "frisket.geosmoke.projection.map_points",
            "placements": [
                {
                    "host": "mainView",
                    "mode": "pane",
                    "slot": "work.companion",
                    "placementId": "frisket-geo-map-companion",
                }
            ],
            "requires": [
                {"kind": "hostCapability", "id": "projection.status"},
                {"kind": "hostCapability", "id": "grid.filter.applyBbox"},
            ],
            "dataRequirements": [
                {"kind": "sheetHasColumnType", "columnType": "geo_point"}
            ],
        }
        assert "componentKey" not in json.dumps(
            plugin["workbenchDescriptorManifests"],
            separators=(",", ":"),
        )

        assert backend_payload["registeredRuntimeBindings"] == {
            "actions": [SMOKE_ACTION_KIND],
            "importers": [SMOKE_IMPORTER_KIND],
            "operators": [SMOKE_OPERATOR_KIND],
            "projections": [SMOKE_PROJECTION_KIND],
            "jobHandlers": [],
        }

        bindings = {
            binding_type: {
                spec.kind: spec
                for spec in default_registry().runtime_binding_specs(binding_type)
            }
            for binding_type in ("actions", "importers", "operators", "projections")
        }
        assert bindings["actions"][SMOKE_ACTION_KIND].handler_api == (
            "plugin_typed_action_native"
        )
        assert bindings["actions"][SMOKE_ACTION_KIND].handler_key == (
            SMOKE_ACTION_HANDLER_KEY
        )
        assert bindings["importers"][SMOKE_IMPORTER_KIND].handler_api == (
            "plugin_importer_subprocess"
        )
        assert bindings["importers"][SMOKE_IMPORTER_KIND].handler_key == (
            SMOKE_IMPORTER_HANDLER_KEY
        )
        assert bindings["operators"][SMOKE_OPERATOR_KIND].handler_api == (
            "plugin_operator_subprocess"
        )
        assert bindings["operators"][SMOKE_OPERATOR_KIND].handler_key == (
            SMOKE_OPERATOR_HANDLER_KEY
        )
        projection_binding = bindings["projections"][SMOKE_PROJECTION_KIND]
        assert projection_binding.handler_api == "plugin_projection_subprocess"
        assert projection_binding.handler_key == SMOKE_HANDLER_KEY
        assert projection_binding.metadata["execution"] == {
            "mode": "runtime_plan",
            "role": "map_points",
        }
        assert projection_runtime_binding(SMOKE_PROJECTION_KIND) is not None

        assert get_trusted_backend_handler(SMOKE_ACTION_HANDLER_KEY) is None
        assert get_trusted_backend_handler(SMOKE_IMPORTER_HANDLER_KEY) is None
        assert get_trusted_backend_handler(SMOKE_OPERATOR_HANDLER_KEY) is None
        assert get_trusted_backend_handler(SMOKE_HANDLER_KEY) is None

        points_response = client.get(
            f"/api/projects/{project_id}/sheets/{sheet_id}/map/points",
            params={"column_id": geo_col},
        )
        assert points_response.status_code == 200, points_response.text
        assert (
            points_response.headers["x-frisket-runtime-projection-kind"]
            == SMOKE_PROJECTION_KIND
        )
        assert points_response.headers["x-frisket-runtime-projection-status"] == "stale"
        assert (
            points_response.headers[
                "x-frisket-runtime-projection-build-idempotency-key"
            ]
            == "real-local-smoke-map-points@gen-2"
        )
        assert rows
    finally:
        _reset_default_registry_for_tests()
