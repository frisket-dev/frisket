from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient
import pytest

from frisket.authoring.workbench import plugin_runtime_status as plugin_runtime
from frisket.server.app import create_app


def _write_manifest(tmp_path: Path, *, plugin_id: str = "frisket-ndjson") -> Path:
    package_dir = tmp_path / plugin_id
    package_dir.mkdir(parents=True, exist_ok=True)
    path = package_dir / "plugin.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "frisket.plugin.v1",
                "id": plugin_id,
                "version": "0.1.0",
                "contributes": {
                    "actions": ["import.ndjson"],
                    "importers": ["ndjson"],
                    "column_types": ["geo_point"],
                    "job_handlers": [],
                },
                "requires": {"capabilities": [], "secrets": []},
            }
        ),
        encoding="utf-8",
    )
    return path


def _install_local(
    client: TestClient, project_id: str, manifest_path: Path, *, plugin_id: str
) -> dict[str, Any]:
    response = client.post(
        f"/api/projects/{project_id}/workbench/plugins/{plugin_id}/install-local",
        json={
            "source": {"kind": "localPath", "value": str(manifest_path)},
            "arbitraryPackageLoadAllowed": False,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_workbench_plugin_runtime_index_byte_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The index derives package identity from the workspace catalog,
    # not a per-project receipt scan. Package IDENTITY (manifest evidence + the
    # digests) is the catalog's; the project's row supplies enablement (here
    # absent, so the plugin projects as a bare "installed" catalog entry).
    manifest_ref = {
        "kind": "plugin_manifest",
        "schema_version": "frisket.plugin.v1",
        "plugin_id": "fixture.plugin",
        "version": "1.2.3",
        "path": "/fixtures/fixture.plugin.json",
        "manifest_sha256": "sha256:fixture-manifest",
        "package_sha256": "sha256:fixture-package",
        "byte_count": 321,
        "contributes": {
            "workbench_views": ["fixture.plugin.view.main"],
            "actions": ["fixture.action"],
        },
        "requires": {
            "capabilities": ["project:read"],
            "secrets": ["FIXTURE_TOKEN"],
        },
        "runtime": {
            "workbench_components": [
                {
                    "contribution_id": "fixture.plugin.view.main",
                    "module_key": "trustedLocal.fixturePlugin.module",
                    "component_key": "trustedLocal.fixturePlugin.MainView",
                    "module_path": "frontend/main.js",
                }
            ]
        },
    }
    catalog_entry = {
        "plugin_id": "fixture.plugin",
        "install_source": "{}",
        "manifest_sha256": "sha256:fixture-manifest",
        "package_sha256": "sha256:fixture-package",
        "runtime_source": "plugin.load_receipt",
        "receipt_id": "receipt-fixture",
        "manifest_ref": json.dumps(manifest_ref),
        "has_executable_backend": 0,
        "install_failure": None,
        "updated_at": "",
    }

    class FixtureCatalog:
        def all(self) -> dict[str, dict[str, Any]]:
            return {"fixture.plugin": catalog_entry}

        def manifest_ref(self, plugin_id: str) -> dict[str, Any] | None:
            return manifest_ref if plugin_id == "fixture.plugin" else None

    fixture_project = object()
    monkeypatch.setattr(
        plugin_runtime,
        "plugin_package_catalog_for_project",
        lambda project: FixtureCatalog(),
    )
    monkeypatch.setattr(
        plugin_runtime,
        "default_registry",
        lambda: SimpleNamespace(plugin_manifests=lambda: []),
    )
    monkeypatch.setattr(
        plugin_runtime,
        "_workbench_plugin_install_states",
        lambda project: {},
    )
    monkeypatch.setattr(
        plugin_runtime,
        "load_first_party_workbench_descriptor_package",
        lambda: SimpleNamespace(
            schema_version="frisket.workbench_descriptor_package.v1",
            descriptor_manifests=[
                {
                    "schemaVersion": "frisket.workbench.view.v1",
                    "id": "frisket.core.view.fixture",
                }
            ],
        ),
    )

    rendered = json.dumps(
        plugin_runtime.workbench_plugin_runtime_index(
            fixture_project,
            project_id="project/fixture",
        ),
        separators=(",", ":"),
    ).encode()
    expected = json.dumps(
        {
            "schemaVersion": "frisket.workbench_plugin_runtime_index.v1",
            "projectId": "project/fixture",
            "arbitraryPackageLoadAllowed": False,
            "receiptScanLimit": 5000,
            "skippedInvalidReceipts": 0,
            "skippedInvalidManifestRefs": 0,
            "loadedPluginCount": 1,
            "plugins": [
                {
                    "schemaVersion": "frisket.workbench_plugin_runtime_plugin.v1",
                    "pluginId": "fixture.plugin",
                    "version": "1.2.3",
                    "installState": "installed",
                    "activation": "manifestLoaded",
                    "runtimeSource": "plugin.load_receipt",
                    "receiptId": "receipt-fixture",
                    "manifestSha256": "sha256:fixture-manifest",
                    "packageSha256": "sha256:fixture-package",
                    "byteCount": 321,
                    "source": {
                        "kind": "local_file",
                        "path": "/fixtures/fixture.plugin.json",
                    },
                    "contributionSummary": [
                        {
                            "kind": "workbench_view",
                            "count": 1,
                            "ids": ["fixture.plugin.view.main"],
                        },
                        {
                            "kind": "action",
                            "count": 1,
                            "ids": ["fixture.action"],
                        },
                    ],
                    "frontendComponentBindings": [
                        {
                            "schemaVersion": (
                                "frisket.workbench_plugin_frontend_component_binding.v1"
                            ),
                            "contributionId": "fixture.plugin.view.main",
                            "moduleKey": "trustedLocal.fixturePlugin.module",
                            "componentKey": "trustedLocal.fixturePlugin.MainView",
                            "modulePath": "frontend/main.js",
                            "moduleUrl": (
                                "/api/projects/project%2Ffixture/workbench/plugins/"
                                "fixture.plugin/frontend-components/"
                                "fixture.plugin.view.main/module.js?"
                                "package=sha256%3Afixture-package"
                            ),
                        }
                    ],
                    "requires": {
                        "capabilities": ["project:read"],
                        "secrets": ["FIXTURE_TOKEN"],
                    },
                    "arbitraryPackageLoadAllowed": False,
                    "registryActivated": False,
                    "installStateSchemaVersion": None,
                    "disabledReason": None,
                }
            ],
            "firstParty": {
                "schemaVersion": "frisket.workbench_descriptor_package.v1",
                "descriptors": [
                    {
                        "schemaVersion": "frisket.workbench.view.v1",
                        "id": "frisket.core.view.fixture",
                    }
                ],
            },
        },
        separators=(",", ":"),
    ).encode()

    assert rendered == expected


def test_workbench_plugin_runtime_index_projects_loaded_manifest_receipts(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    project_id = client.post(
        "/api/projects", json={"name": "Workbench plugin index"}
    ).json()["id"]
    manifest_path = _write_manifest(tmp_path)

    install = _install_local(
        client, project_id, manifest_path, plugin_id="frisket-ndjson"
    )
    assert install["receiptId"]

    index_response = client.get(f"/api/projects/{project_id}/workbench/plugins")
    assert index_response.status_code == 200, index_response.text
    body = index_response.json()
    assert body["schemaVersion"] == "frisket.workbench_plugin_runtime_index.v1"
    assert body["projectId"] == project_id
    assert body["arbitraryPackageLoadAllowed"] is False
    assert body["skippedInvalidReceipts"] == 0
    assert body["skippedInvalidManifestRefs"] == 0
    assert body["loadedPluginCount"] == 1

    plugin = body["plugins"][0]
    assert plugin["schemaVersion"] == "frisket.workbench_plugin_runtime_plugin.v1"
    assert plugin["pluginId"] == "frisket-ndjson"
    assert plugin["version"] == "0.1.0"
    assert plugin["installState"] == "installed"
    assert plugin["activation"] == "manifestLoaded"
    assert plugin["runtimeSource"] == "plugin.load_receipt"
    assert plugin["receiptId"] == install["receiptId"]
    assert plugin["manifestSha256"].startswith("sha256:")
    # Source is now the validated install source recorded in the workspace
    # catalog, keyed by plugin_id.
    assert plugin["source"]["kind"] == "localPath"
    assert plugin["source"]["value"] == str(manifest_path)
    assert plugin["contributionSummary"] == [
        {"kind": "action", "count": 1, "ids": ["import.ndjson"]},
        {"kind": "importer", "count": 1, "ids": ["ndjson"]},
        {"kind": "column_type", "count": 1, "ids": ["geo_point"]},
    ]
    assert plugin["requires"] == {
        "capabilities": [],
        "secrets": [],
    }
    assert plugin["arbitraryPackageLoadAllowed"] is False


def test_workbench_plugin_runtime_index_projects_frontend_component_bindings(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    project_id = client.post(
        "/api/projects", json={"name": "Workbench plugin frontend bindings"}
    ).json()["id"]
    package_dir = tmp_path / "frisket.geo"
    package_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = package_dir / "plugin.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": "frisket.plugin.v1",
                "id": "frisket.geo",
                "version": "0.1.0",
                "contributes": {
                    "workbench_views": ["frisket.geo.view.map"],
                    "workbench_panels": [
                        # Cross-surface DECLARATION (legal): the plugin lights
                        # up the core projection panel...
                        "frisket.core.panel.projection_status",
                        # ...but component BINDINGS are always the plugin's
                        # own code (plugin-binding-namespace-hardening-v1).
                        "frisket.geo.panel.receipt_status",
                    ],
                },
                "requires": {"capabilities": [], "secrets": []},
                "runtime": {
                    "workbench_components": [
                        {
                            "contribution_id": "frisket.geo.view.map",
                            "module_key": "trustedLocal.frisketGeo.receiptModule",
                            "component_key": "trustedLocal.frisketGeo.receiptMapView",
                        },
                        {
                            "contribution_id": "frisket.geo.panel.receipt_status",
                            "module_key": "trustedLocal.frisketGeo.receiptModule",
                            "component_key": "trustedLocal.frisketGeo.receiptProjectionStatus",
                        },
                    ]
                },
            }
        ),
        encoding="utf-8",
    )

    _install_local(client, project_id, manifest_path, plugin_id="frisket.geo")

    index_response = client.get(f"/api/projects/{project_id}/workbench/plugins")
    assert index_response.status_code == 200, index_response.text
    plugin = index_response.json()["plugins"][0]
    assert plugin["pluginId"] == "frisket.geo"
    assert plugin["contributionSummary"] == [
        {"kind": "workbench_view", "count": 1, "ids": ["frisket.geo.view.map"]},
        {
            "kind": "workbench_panel",
            "count": 2,
            "ids": [
                "frisket.core.panel.projection_status",
                "frisket.geo.panel.receipt_status",
            ],
        },
    ]
    assert plugin["frontendComponentBindings"] == [
        {
            "schemaVersion": "frisket.workbench_plugin_frontend_component_binding.v1",
            "contributionId": "frisket.geo.view.map",
            "moduleKey": "trustedLocal.frisketGeo.receiptModule",
            "componentKey": "trustedLocal.frisketGeo.receiptMapView",
        },
        {
            "schemaVersion": "frisket.workbench_plugin_frontend_component_binding.v1",
            "contributionId": "frisket.geo.panel.receipt_status",
            "moduleKey": "trustedLocal.frisketGeo.receiptModule",
            "componentKey": "trustedLocal.frisketGeo.receiptProjectionStatus",
        },
    ]


def test_workbench_plugin_runtime_index_filters_invalid_frontend_component_bindings(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    project_id = client.post(
        "/api/projects", json={"name": "Workbench plugin invalid frontend bindings"}
    ).json()["id"]
    package_dir = tmp_path / "frisket.geo"
    package_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = package_dir / "plugin.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": "frisket.plugin.v1",
                "id": "frisket.geo",
                "version": "0.1.0",
                "contributes": {"workbench_views": ["frisket.geo.view.map"]},
                "requires": {"capabilities": [], "secrets": []},
                "runtime": {
                    "workbench_components": [
                        {
                            "contribution_id": "frisket.geo.view.map",
                            "module_key": "trustedLocal.frisketGeo",
                            "component_key": "trustedLocal.frisketGeo.views.MapView",
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )

    _install_local(client, project_id, manifest_path, plugin_id="frisket.geo")

    # Corrupt the WORKSPACE CATALOG's manifest evidence (identity is catalog-
    # owned now): a whitespace-padded component_key must be filtered out.
    catalog = client.app.state.workspace.plugin_package_catalog
    entry = catalog.get("frisket.geo")
    manifest_ref = catalog.manifest_ref("frisket.geo")
    manifest_ref["runtime"]["workbench_components"][0]["component_key"] = (
        " trustedLocal.frisketGeo.views.MapView "
    )
    catalog.upsert(
        plugin_id="frisket.geo",
        install_source=json.loads(entry["install_source"]),
        manifest_sha256=entry["manifest_sha256"],
        package_sha256=entry["package_sha256"],
        receipt_id=entry["receipt_id"],
        manifest_ref=manifest_ref,
        has_executable_backend=bool(entry["has_executable_backend"]),
    )

    index_response = client.get(f"/api/projects/{project_id}/workbench/plugins")
    assert index_response.status_code == 200, index_response.text
    plugin = index_response.json()["plugins"][0]
    assert plugin["frontendComponentBindings"] == []
