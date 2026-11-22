"""Workbench plugin install plans, install state, lifecycle product, backend activation, and contribution registry tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from frisket.authoring.column_types import get_column_type
from frisket.authoring.plugin_registry import (
    _reset_default_registry_for_tests,
    default_registry,
)
from frisket.authoring.workbench.plugin_runtime import ensure_workspace_plugin_packages
from frisket.server.app import create_app

# --- install plan ---

ROOT = Path(__file__).parent.parent / "fixtures" / "local_plugins" / "frisket_geo_smoke"
PLUGIN_ID = "frisket.geosmoke"
TRUSTED_LOCAL_BACKEND_CAPABILITY = "plugin:trusted_local_backend"


def _client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(tmp_path / "workspace"))


def _install_plan_project_id(
    client: TestClient, name: str = "local install plan"
) -> str:
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
        project_id = _install_plan_project_id(client)

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
        project_id = _install_plan_project_id(client, "local install failure")

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
        project_id = _install_plan_project_id(client, "staged update")
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


# --- install state ---


def _install_state_write_manifest(
    tmp_path: Path, *, plugin_id: str = "frisket-ndjson"
) -> Path:
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
                    "actions": [],
                    "importers": ["frisket_ndjson"],
                    "column_types": ["frisket_ndjson_record"],
                    "job_handlers": [],
                },
                "requires": {
                    "capabilities": [TRUSTED_LOCAL_BACKEND_CAPABILITY],
                    "secrets": [],
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def _install_and_activate(
    client: TestClient, project_id: str, manifest_path: Path
) -> tuple[str, dict[str, Any]]:
    # Install writes the workspace catalog (identity); activation
    # writes this project's enablement row and registers the manifest.
    install_response = client.post(
        f"/api/projects/{project_id}/workbench/plugins/frisket-ndjson/install-local",
        json={
            "source": {"kind": "localPath", "value": str(manifest_path)},
            "arbitraryPackageLoadAllowed": False,
        },
    )
    assert install_response.status_code == 200, install_response.text
    receipt_id = install_response.json()["receiptId"]
    assert receipt_id

    activated_response = client.post(
        f"/api/projects/{project_id}/workbench/plugins/frisket-ndjson/activate",
        json={
            "receiptId": receipt_id,
            "trustAcknowledged": True,
            "permissionsAccepted": [TRUSTED_LOCAL_BACKEND_CAPABILITY],
            "arbitraryPackageLoadAllowed": False,
        },
    )
    assert activated_response.status_code == 200, activated_response.text
    return receipt_id, activated_response.json()


def test_workbench_plugin_activation_persists_project_install_state(
    tmp_path: Path,
) -> None:
    _reset_default_registry_for_tests()
    try:
        client = TestClient(create_app(tmp_path / "workspace"))
        project_id = client.post(
            "/api/projects", json={"name": "Workbench plugin install state"}
        ).json()["id"]
        receipt_id, activated = _install_and_activate(
            client, project_id, _install_state_write_manifest(tmp_path)
        )

        # The PROJECT row owns ONLY enablement + caps + grant now; it carries no
        # package identity column.
        project = client.app.state.workspace.get(project_id)
        rows = project.db.execute(
            "SELECT plugin_id, install_state, activation, "
            "arbitrary_package_load_allowed, permissions_accepted, disabled_reason "
            "FROM workbench_plugin_installs"
        ).fetchall()
        assert len(rows) == 1
        row = rows[0]
        assert row["plugin_id"] == "frisket-ndjson"
        assert row["install_state"] == "enabled"
        assert row["activation"] == "registryManifestRegistered"
        assert row["arbitrary_package_load_allowed"] == 0
        assert json.loads(row["permissions_accepted"]) == [
            TRUSTED_LOCAL_BACKEND_CAPABILITY
        ]
        assert row["disabled_reason"] is None

        # IDENTITY is the workspace catalog's, keyed by plugin_id.
        entry = client.app.state.workspace.plugin_package_catalog.get("frisket-ndjson")
        assert entry is not None
        assert entry["receipt_id"] == receipt_id
        assert entry["manifest_sha256"] == activated["manifestSha256"]
        assert entry["package_sha256"] == activated["packageSha256"]
        assert entry["runtime_source"] == "plugin.load_receipt"

        index_response = client.get(f"/api/projects/{project_id}/workbench/plugins")
        assert index_response.status_code == 200, index_response.text
        plugin = index_response.json()["plugins"][0]
        assert plugin["installState"] == "enabled"
        assert plugin["activation"] == "registryManifestRegistered"
        assert plugin["packageSha256"] == activated["packageSha256"]
        assert plugin["registryActivated"] is True
        assert (
            plugin["installStateSchemaVersion"]
            == "frisket.workbench_plugin_install_state.v1"
        )
        assert plugin["disabledReason"] is None
    finally:
        _reset_default_registry_for_tests()


def test_workbench_plugin_disable_is_project_scoped_and_uninstall_is_workspace_wide(
    tmp_path: Path,
) -> None:
    _reset_default_registry_for_tests()
    try:
        client = TestClient(create_app(tmp_path / "workspace"))
        project_id = client.post(
            "/api/projects", json={"name": "Workbench plugin lifecycle state"}
        ).json()["id"]
        receipt_id, _activated = _install_and_activate(
            client, project_id, _install_state_write_manifest(tmp_path)
        )
        backend_activation = client.post(
            f"/api/projects/{project_id}/workbench/plugins/frisket-ndjson/backend/activate",
            json={"trustAcknowledged": True, "arbitraryPackageLoadAllowed": False},
        )
        assert backend_activation.status_code == 200, backend_activation.text
        assert [
            loaded.manifest.id for loaded in default_registry().plugin_manifests()
        ] == ["frisket-ndjson"]
        assert get_column_type("frisket_ndjson_record") is not None

        disabled_response = client.post(
            f"/api/projects/{project_id}/workbench/plugins/frisket-ndjson/disable"
        )
        assert disabled_response.status_code == 200, disabled_response.text
        disabled = disabled_response.json()
        assert disabled["schemaVersion"] == "frisket.workbench_plugin_install_state.v1"
        assert disabled["pluginId"] == "frisket-ndjson"
        assert disabled["receiptId"] == receipt_id
        assert disabled["packageSha256"]
        assert disabled["installState"] == "disabled"
        assert disabled["activation"] == "blocked"
        assert disabled["disabledReason"] == "plugin_disabled"
        assert disabled["registryActivated"] is False
        assert disabled["arbitraryPackageLoadAllowed"] is False

        # NEW behavior: disabling in ONE project only flips that project's
        # enablement. The workspace package stays registered (it may serve other
        # projects); disable no longer unregisters the shared package.
        assert [
            loaded.manifest.id for loaded in default_registry().plugin_manifests()
        ] == ["frisket-ndjson"]
        assert get_column_type("frisket_ndjson_record") is not None
        still_registered = default_registry().to_public()
        assert "frisket_ndjson" in {
            item["name"] for item in still_registered["importers"]
        }

        disabled_index = client.get(f"/api/projects/{project_id}/workbench/plugins")
        assert disabled_index.status_code == 200, disabled_index.text
        plugin = disabled_index.json()["plugins"][0]
        assert plugin["pluginId"] == "frisket-ndjson"
        assert plugin["receiptId"] == receipt_id
        assert plugin["packageSha256"] == disabled["packageSha256"]
        assert plugin["installState"] == "disabled"
        assert plugin["activation"] == "blocked"
        assert plugin["disabledReason"] == "plugin_disabled"
        # The project no longer enables it, so it is not registry-activated FOR
        # THIS PROJECT even though the shared manifest is still registered.
        assert plugin["registryActivated"] is False

        uninstalled_response = client.post(
            f"/api/projects/{project_id}/workbench/plugins/frisket-ndjson/uninstall"
        )
        assert uninstalled_response.status_code == 200, uninstalled_response.text
        uninstalled = uninstalled_response.json()
        assert (
            uninstalled["schemaVersion"] == "frisket.workbench_plugin_install_state.v1"
        )
        assert uninstalled["pluginId"] == "frisket-ndjson"
        assert uninstalled["receiptId"] == receipt_id
        assert uninstalled["packageSha256"] == disabled["packageSha256"]
        assert uninstalled["installState"] == "uninstalled"
        assert uninstalled["activation"] == "removed"
        assert uninstalled["registryActivated"] is False
        assert uninstalled["arbitraryPackageLoadAllowed"] is False

        uninstalled_index = client.get(f"/api/projects/{project_id}/workbench/plugins")
        assert uninstalled_index.status_code == 200, uninstalled_index.text
        assert uninstalled_index.json()["plugins"] == []
        catalog = client.app.state.workspace.plugin_package_catalog
        assert catalog.get("frisket-ndjson") is None
        assert catalog.is_deleted("frisket-ndjson")

        # The audit plugin.load receipt written at install time survives.
        receipt = (
            client.app.state.workspace.get(project_id)
            .db.execute("SELECT id FROM receipts WHERE id=?", (receipt_id,))
            .fetchone()
        )
        assert receipt is not None
    finally:
        _reset_default_registry_for_tests()


def test_workbench_plugin_lifecycle_rejects_unknown_plugin(client) -> None:
    project_id = client.post(
        "/api/projects", json={"name": "Workbench plugin lifecycle unknown"}
    ).json()["id"]

    disabled = client.post(
        f"/api/projects/{project_id}/workbench/plugins/frisket-missing/disable"
    )
    assert disabled.status_code == 404, disabled.text
    assert disabled.json()["detail"]["code"] == "plugin_lifecycle_not_installed"

    uninstalled = client.post(
        f"/api/projects/{project_id}/workbench/plugins/frisket-missing/uninstall"
    )
    assert uninstalled.status_code == 404, uninstalled.text
    assert uninstalled.json()["detail"]["code"] == "plugin_lifecycle_not_installed"


def test_workbench_plugin_lifecycle_rejects_corrupt_install_state(
    tmp_path: Path,
) -> None:
    _reset_default_registry_for_tests()
    try:
        client = TestClient(create_app(tmp_path / "workspace"))
        project_id = client.post(
            "/api/projects", json={"name": "Workbench plugin corrupt state"}
        ).json()["id"]
        _receipt_id, _activated = _install_and_activate(
            client, project_id, _install_state_write_manifest(tmp_path)
        )
        # Corruption now lives in the workspace catalog (the identity authority):
        # strip its identity evidence while the project stays enabled.
        catalog = client.app.state.workspace.plugin_package_catalog
        entry = catalog.get("frisket-ndjson")
        catalog.upsert(
            plugin_id="frisket-ndjson",
            install_source=json.loads(entry["install_source"]),
            manifest_sha256="",
            package_sha256="",
            receipt_id=None,
            manifest_ref=catalog.manifest_ref("frisket-ndjson"),
            has_executable_backend=bool(entry["has_executable_backend"]),
        )

        disabled = client.post(
            f"/api/projects/{project_id}/workbench/plugins/frisket-ndjson/disable"
        )
        assert disabled.status_code == 409, disabled.text
        assert disabled.json()["detail"]["code"] == "plugin_lifecycle_state_corrupt"

        # The project enablement row is untouched by the refused transition.
        project = client.app.state.workspace.get(project_id)
        row = project.db.execute(
            "SELECT install_state FROM workbench_plugin_installs "
            "WHERE plugin_id='frisket-ndjson'"
        ).fetchone()
        assert row["install_state"] == "enabled"
        assert catalog.get("frisket-ndjson")["manifest_sha256"] == ""
    finally:
        _reset_default_registry_for_tests()


# --- lifecycle product ---

CAPABILITY = "plugin:trusted_local_backend"


def _lifecycle_product_project_id(client: TestClient) -> str:
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
        project_id = _lifecycle_product_project_id(client)
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


# --- backend activation ---


def _backend_activation_write_manifest(
    tmp_path: Path, *, plugin_id: str = "frisket-ndjson"
) -> Path:
    # One package per directory so localPath install computes an isolated
    # package identity (the workspace catalog is keyed by plugin_id).
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
                "requires": {
                    "capabilities": [],
                    "secrets": [],
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def _install_local(
    client: TestClient, project_id: str, manifest_path: Path, *, plugin_id: str
) -> dict[str, Any]:
    """Install a trusted-local package. Populates the workspace catalog with the
    package identity and returns the install-plan payload."""
    response = client.post(
        f"/api/projects/{project_id}/workbench/plugins/{plugin_id}/install-local",
        json={
            "source": {"kind": "localPath", "value": str(manifest_path)},
            "arbitraryPackageLoadAllowed": False,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_workbench_plugin_activation_is_trust_gated_manifest_registration(
    tmp_path: Path,
) -> None:
    _reset_default_registry_for_tests()
    try:
        client = TestClient(create_app(tmp_path / "workspace"))
        project_id = client.post(
            "/api/projects", json={"name": "Workbench plugin activation"}
        ).json()["id"]
        manifest_path = _backend_activation_write_manifest(tmp_path)

        install = _install_local(
            client, project_id, manifest_path, plugin_id="frisket-ndjson"
        )
        receipt_id = install["receiptId"]
        assert receipt_id
        # Install writes the workspace catalog and the project enablement row,
        # but registers no executable code until activation.
        assert default_registry().plugin_manifests() == []

        blocked = client.post(
            f"/api/projects/{project_id}/workbench/plugins/frisket-ndjson/activate",
            json={
                "receiptId": receipt_id,
                "trustAcknowledged": False,
                "permissionsAccepted": [],
                "arbitraryPackageLoadAllowed": False,
            },
        )
        assert blocked.status_code == 403, blocked.text
        assert blocked.json()["detail"]["code"] == "plugin_activation_trust_required"
        assert default_registry().plugin_manifests() == []

        unexpected_permissions = client.post(
            f"/api/projects/{project_id}/workbench/plugins/frisket-ndjson/activate",
            json={
                "receiptId": receipt_id,
                "trustAcknowledged": True,
                "permissionsAccepted": ["local.external.activate"],
                "arbitraryPackageLoadAllowed": False,
            },
        )
        assert unexpected_permissions.status_code == 400, unexpected_permissions.text
        assert (
            unexpected_permissions.json()["detail"]["code"]
            == "plugin_activation_permissions_unexpected"
        )
        assert default_registry().plugin_manifests() == []

        activated_response = client.post(
            f"/api/projects/{project_id}/workbench/plugins/frisket-ndjson/activate",
            json={
                "receiptId": receipt_id,
                "trustAcknowledged": True,
                "permissionsAccepted": [],
                "arbitraryPackageLoadAllowed": False,
            },
        )
        assert activated_response.status_code == 200, activated_response.text
        activated = activated_response.json()
        assert activated["schemaVersion"] == "frisket.workbench_plugin_activation.v1"
        assert activated["projectId"] == project_id
        assert activated["pluginId"] == "frisket-ndjson"
        assert activated["receiptId"] == receipt_id
        assert activated["runtimeSource"] == "plugin.load_receipt"
        assert activated["activation"] == "registryManifestRegistered"
        assert activated["registryActivated"] is True
        assert activated["arbitraryPackageLoadAllowed"] is False
        assert activated["manifestSha256"].startswith("sha256:")
        assert activated["registeredPluginManifests"] == ["frisket-ndjson"]
        assert activated["permissionsAccepted"] == []

        registered = default_registry().plugin_manifests()
        assert [item.manifest.id for item in registered] == ["frisket-ndjson"]
        assert registered[0].sha256 == activated["manifestSha256"]

        index_response = client.get(f"/api/projects/{project_id}/workbench/plugins")
        assert index_response.status_code == 200, index_response.text
        plugin = index_response.json()["plugins"][0]
        assert plugin["installState"] == "enabled"
        assert plugin["activation"] == "registryManifestRegistered"
        assert plugin["registryActivated"] is True
    finally:
        _reset_default_registry_for_tests()


def test_workbench_plugin_activation_rejects_manifest_evidence_without_sha(
    tmp_path: Path,
) -> None:
    _reset_default_registry_for_tests()
    try:
        client = TestClient(create_app(tmp_path / "workspace"))
        project_id = client.post(
            "/api/projects", json={"name": "Workbench plugin activation sha"}
        ).json()["id"]
        manifest_path = _backend_activation_write_manifest(tmp_path)

        install = _install_local(
            client, project_id, manifest_path, plugin_id="frisket-ndjson"
        )
        receipt_id = install["receiptId"]

        # Identity now lives in the workspace catalog, not a per-project receipt.
        # Corrupt the catalog's manifest evidence (drop the manifest sha) to
        # prove activation still fails closed.
        catalog = client.app.state.workspace.plugin_package_catalog
        entry = catalog.get("frisket-ndjson")
        manifest_ref = catalog.manifest_ref("frisket-ndjson")
        manifest_ref["manifest_sha256"] = ""
        catalog.upsert(
            plugin_id="frisket-ndjson",
            install_source=json.loads(entry["install_source"]),
            manifest_sha256="",
            package_sha256=entry["package_sha256"],
            receipt_id=entry["receipt_id"],
            manifest_ref=manifest_ref,
            has_executable_backend=bool(entry["has_executable_backend"]),
        )

        invalid_sha = client.post(
            f"/api/projects/{project_id}/workbench/plugins/frisket-ndjson/activate",
            json={
                "receiptId": receipt_id,
                "trustAcknowledged": True,
                "permissionsAccepted": [],
                "arbitraryPackageLoadAllowed": False,
            },
        )
        assert invalid_sha.status_code == 400, invalid_sha.text
        assert (
            invalid_sha.json()["detail"]["code"]
            == "plugin_activation_manifest_sha_missing"
        )
        assert default_registry().plugin_manifests() == []
    finally:
        _reset_default_registry_for_tests()


def test_workbench_plugin_activation_rejects_mismatched_receipt(
    tmp_path: Path,
) -> None:
    _reset_default_registry_for_tests()
    try:
        client = TestClient(create_app(tmp_path / "workspace"))
        project_id = client.post(
            "/api/projects", json={"name": "Workbench plugin activation mismatch"}
        ).json()["id"]
        ndjson_manifest = _backend_activation_write_manifest(
            tmp_path, plugin_id="frisket-ndjson"
        )
        other_manifest = _backend_activation_write_manifest(
            tmp_path, plugin_id="frisket-other"
        )

        _install_local(client, project_id, ndjson_manifest, plugin_id="frisket-ndjson")
        other_install = _install_local(
            client, project_id, other_manifest, plugin_id="frisket-other"
        )

        # A receipt id that belongs to a DIFFERENT catalog package cannot
        # activate this one: the catalog binds one identity per plugin_id.
        mismatch = client.post(
            f"/api/projects/{project_id}/workbench/plugins/frisket-ndjson/activate",
            json={
                "receiptId": other_install["receiptId"],
                "trustAcknowledged": True,
                "permissionsAccepted": [],
                "arbitraryPackageLoadAllowed": False,
            },
        )
        assert mismatch.status_code == 404, mismatch.text
        assert (
            mismatch.json()["detail"]["code"] == "plugin_activation_manifest_mismatch"
        )
    finally:
        _reset_default_registry_for_tests()


# --- contribution registry ---


def _contribution_registry_write_manifest(
    tmp_path: Path,
    *,
    plugin_id: str = "frisket-backend-demo",
    importers: list[str] | None = None,
    column_types: list[str] | None = None,
    job_handlers: list[str] | None = None,
) -> Path:
    # One package per directory: localPath install (workspace catalog) hashes
    # the manifest's parent dir for package identity.
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
                    "actions": [],
                    "importers": importers
                    if importers is not None
                    else ["frisket_backend_demo"],
                    "column_types": column_types
                    if column_types is not None
                    else ["frisket_backend_demo_record"],
                    "job_handlers": job_handlers
                    if job_handlers is not None
                    else ["frisket.backend_demo.job"],
                },
                "requires": {
                    "capabilities": [TRUSTED_LOCAL_BACKEND_CAPABILITY],
                    "secrets": [],
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def _load_and_enable(
    client: TestClient, project_id: str, manifest_path: Path, *, plugin_id: str
) -> tuple[str, str]:
    # Install-local writes the workspace catalog identity; activate
    # then registers the manifest and writes the project enablement row.
    install_response = client.post(
        f"/api/projects/{project_id}/workbench/plugins/{plugin_id}/install-local",
        json={
            "source": {"kind": "localPath", "value": str(manifest_path)},
            "arbitraryPackageLoadAllowed": False,
        },
    )
    assert install_response.status_code == 200, install_response.text
    receipt_id = install_response.json()["receiptId"]
    assert receipt_id

    activation_response = client.post(
        f"/api/projects/{project_id}/workbench/plugins/{plugin_id}/activate",
        json={
            "receiptId": receipt_id,
            "trustAcknowledged": True,
            "permissionsAccepted": [TRUSTED_LOCAL_BACKEND_CAPABILITY],
            "arbitraryPackageLoadAllowed": False,
        },
    )
    assert activation_response.status_code == 200, activation_response.text
    return receipt_id, activation_response.json()["manifestSha256"]


def test_enabled_plugin_registers_backend_contribution_metadata_without_package_loading(
    tmp_path: Path,
) -> None:
    _reset_default_registry_for_tests()
    try:
        client = TestClient(create_app(tmp_path / "workspace"))
        project_id = client.post(
            "/api/projects", json={"name": "Workbench backend contribution registry"}
        ).json()["id"]
        manifest_path = _contribution_registry_write_manifest(tmp_path)
        receipt_id, manifest_sha = _load_and_enable(
            client, project_id, manifest_path, plugin_id="frisket-backend-demo"
        )

        assert get_column_type("frisket_backend_demo_record") is None
        snapshot_before = default_registry().to_public()
        assert "frisket_backend_demo" not in {
            item["name"] for item in snapshot_before["importers"]
        }
        assert "frisket.backend_demo.job" not in {
            item["kind"] for item in snapshot_before["job_handlers"]
        }

        response = client.post(
            f"/api/projects/{project_id}/workbench/plugins/frisket-backend-demo/backend/activate",
            json={"trustAcknowledged": True, "arbitraryPackageLoadAllowed": False},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["schemaVersion"] == "frisket.workbench_plugin_backend_activation.v1"
        assert body["projectId"] == project_id
        assert body["pluginId"] == "frisket-backend-demo"
        assert body["receiptId"] == receipt_id
        assert body["manifestSha256"] == manifest_sha
        assert body["runtimeSource"] == "plugin.load_receipt"
        assert body["arbitraryPackageLoadAllowed"] is False
        assert body["executableHandlersRegistered"] is False
        # url-classification-plugin-matchers-v1: registeredBackendContributions
        # gained a matchers key (host-side matcher-evaluation metadata,
        # registered on the always-run metadata path). This manifest declares
        # none, so it's empty.
        assert body["registeredBackendContributions"] == {
            "columnTypes": ["frisket_backend_demo_record"],
            "importers": ["frisket_backend_demo"],
            "jobHandlers": ["frisket.backend_demo.job"],
            "matchers": [],
        }

        snapshot = default_registry().to_public()
        column = get_column_type("frisket_backend_demo_record")
        assert column is not None
        assert column.core is False
        importer = next(
            item
            for item in snapshot["importers"]
            if item["name"] == "frisket_backend_demo"
        )
        assert importer["plugin"] == "frisket-backend-demo"
        assert importer["has_handler"] is False
        job_handler = next(
            item
            for item in snapshot["job_handlers"]
            if item["kind"] == "frisket.backend_demo.job"
        )
        assert job_handler["plugin"] == "frisket-backend-demo"
        assert job_handler["has_handler"] is False

        repeat = client.post(
            f"/api/projects/{project_id}/workbench/plugins/frisket-backend-demo/backend/activate",
            json={"trustAcknowledged": True, "arbitraryPackageLoadAllowed": False},
        )
        assert repeat.status_code == 200, repeat.text
        assert (
            repeat.json()["registeredBackendContributions"]
            == body["registeredBackendContributions"]
        )
    finally:
        _reset_default_registry_for_tests()


def test_backend_contribution_activation_rejects_untrusted_disabled_and_package_load(
    tmp_path: Path,
) -> None:
    _reset_default_registry_for_tests()
    try:
        client = TestClient(create_app(tmp_path / "workspace"))
        project_id = client.post(
            "/api/projects", json={"name": "Workbench backend contribution policy"}
        ).json()["id"]
        manifest_path = _contribution_registry_write_manifest(tmp_path)
        _receipt_id, _manifest_sha = _load_and_enable(
            client, project_id, manifest_path, plugin_id="frisket-backend-demo"
        )

        untrusted = client.post(
            f"/api/projects/{project_id}/workbench/plugins/frisket-backend-demo/backend/activate",
            json={"trustAcknowledged": False, "arbitraryPackageLoadAllowed": False},
        )
        assert untrusted.status_code == 403, untrusted.text
        assert (
            untrusted.json()["detail"]["code"]
            == "plugin_backend_activation_trust_required"
        )

        package_load = client.post(
            f"/api/projects/{project_id}/workbench/plugins/frisket-backend-demo/backend/activate",
            json={"trustAcknowledged": True, "arbitraryPackageLoadAllowed": True},
        )
        assert package_load.status_code == 403, package_load.text
        assert (
            package_load.json()["detail"]["code"]
            == "plugin_backend_activation_arbitrary_package_load_blocked"
        )

        disabled = client.post(
            f"/api/projects/{project_id}/workbench/plugins/frisket-backend-demo/disable"
        )
        assert disabled.status_code == 200, disabled.text
        blocked = client.post(
            f"/api/projects/{project_id}/workbench/plugins/frisket-backend-demo/backend/activate",
            json={"trustAcknowledged": True, "arbitraryPackageLoadAllowed": False},
        )
        assert blocked.status_code == 409, blocked.text
        assert (
            blocked.json()["detail"]["code"] == "plugin_backend_activation_not_enabled"
        )
    finally:
        _reset_default_registry_for_tests()


def test_backend_contribution_activation_rejects_manifest_sha_mismatch(
    tmp_path: Path,
) -> None:
    _reset_default_registry_for_tests()
    try:
        client = TestClient(create_app(tmp_path / "workspace"))
        project_id = client.post(
            "/api/projects", json={"name": "Workbench backend contribution mismatch"}
        ).json()["id"]
        manifest_path = _contribution_registry_write_manifest(tmp_path)
        _receipt_id, _manifest_sha = _load_and_enable(
            client, project_id, manifest_path, plugin_id="frisket-backend-demo"
        )
        # Package identity is catalog-owned now: corrupt the catalog's recorded
        # manifest sha so backend activation's identity check fails closed.
        catalog = client.app.state.workspace.plugin_package_catalog
        entry = catalog.get("frisket-backend-demo")
        catalog.upsert(
            plugin_id="frisket-backend-demo",
            install_source=json.loads(entry["install_source"]),
            manifest_sha256="sha256:mismatch",
            package_sha256=entry["package_sha256"],
            receipt_id=entry["receipt_id"],
            manifest_ref=catalog.manifest_ref("frisket-backend-demo"),
            has_executable_backend=bool(entry["has_executable_backend"]),
        )

        mismatch = client.post(
            f"/api/projects/{project_id}/workbench/plugins/frisket-backend-demo/backend/activate",
            json={"trustAcknowledged": True, "arbitraryPackageLoadAllowed": False},
        )
        assert mismatch.status_code == 409, mismatch.text
        assert (
            mismatch.json()["detail"]["code"]
            == "plugin_backend_activation_manifest_mismatch"
        )
    finally:
        _reset_default_registry_for_tests()


def test_backend_contribution_activation_requires_persisted_permission_acceptance(
    tmp_path: Path,
) -> None:
    _reset_default_registry_for_tests()
    try:
        client = TestClient(create_app(tmp_path / "workspace"))
        project_id = client.post(
            "/api/projects", json={"name": "Workbench backend contribution permissions"}
        ).json()["id"]
        manifest_path = _contribution_registry_write_manifest(tmp_path)
        _receipt_id, _manifest_sha = _load_and_enable(
            client, project_id, manifest_path, plugin_id="frisket-backend-demo"
        )
        project = client.app.state.workspace.get(project_id)
        project.db.execute(
            "UPDATE workbench_plugin_installs SET permissions_accepted='[]' "
            "WHERE plugin_id='frisket-backend-demo'"
        )
        project.db.commit()

        response = client.post(
            f"/api/projects/{project_id}/workbench/plugins/frisket-backend-demo/backend/activate",
            json={"trustAcknowledged": True, "arbitraryPackageLoadAllowed": False},
        )
        assert response.status_code == 409, response.text
        assert (
            response.json()["detail"]["code"]
            == "plugin_backend_activation_permissions_missing"
        )
    finally:
        _reset_default_registry_for_tests()


def test_backend_contribution_activation_rejects_cross_plugin_column_type_shadow(
    tmp_path: Path,
) -> None:
    _reset_default_registry_for_tests()
    try:
        client = TestClient(create_app(tmp_path / "workspace"))
        project_id = client.post(
            "/api/projects", json={"name": "Workbench backend column shadow"}
        ).json()["id"]
        owner_manifest = _contribution_registry_write_manifest(
            tmp_path,
            plugin_id="frisket-column-owner",
            importers=["frisket_column_owner"],
            column_types=["frisket_shared_backend_record"],
            job_handlers=["frisket.column_owner.job"],
        )
        _load_and_enable(
            client, project_id, owner_manifest, plugin_id="frisket-column-owner"
        )
        owner_activation = client.post(
            f"/api/projects/{project_id}/workbench/plugins/frisket-column-owner/backend/activate",
            json={"trustAcknowledged": True, "arbitraryPackageLoadAllowed": False},
        )
        assert owner_activation.status_code == 200, owner_activation.text
        assert get_column_type("frisket_shared_backend_record") is not None

        shadow_manifest = _contribution_registry_write_manifest(
            tmp_path,
            plugin_id="frisket-column-shadow",
            importers=["frisket_column_shadow"],
            column_types=["frisket_shared_backend_record"],
            job_handlers=["frisket.column_shadow.job"],
        )
        _load_and_enable(
            client, project_id, shadow_manifest, plugin_id="frisket-column-shadow"
        )

        shadow = client.post(
            f"/api/projects/{project_id}/workbench/plugins/frisket-column-shadow/backend/activate",
            json={"trustAcknowledged": True, "arbitraryPackageLoadAllowed": False},
        )
        assert shadow.status_code == 409, shadow.text
        assert shadow.json()["detail"]["code"] == "plugin_backend_activation_conflict"
    finally:
        _reset_default_registry_for_tests()


def test_backend_contribution_activation_rejects_core_registry_collision(
    tmp_path: Path,
) -> None:
    _reset_default_registry_for_tests()
    try:
        client = TestClient(create_app(tmp_path / "workspace"))
        project_id = client.post(
            "/api/projects", json={"name": "Workbench backend contribution collision"}
        ).json()["id"]
        manifest_path = _contribution_registry_write_manifest(
            tmp_path,
            plugin_id="frisket-collision",
            importers=["csv"],
            column_types=["text"],
            job_handlers=["project.run"],
        )
        _receipt_id, _manifest_sha = _load_and_enable(
            client, project_id, manifest_path, plugin_id="frisket-collision"
        )

        collision = client.post(
            f"/api/projects/{project_id}/workbench/plugins/frisket-collision/backend/activate",
            json={"trustAcknowledged": True, "arbitraryPackageLoadAllowed": False},
        )
        assert collision.status_code == 409, collision.text
        assert (
            collision.json()["detail"]["code"] == "plugin_backend_activation_conflict"
        )
    finally:
        _reset_default_registry_for_tests()
