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
from frisket.server.app import create_app


TRUSTED_LOCAL_BACKEND_CAPABILITY = "plugin:trusted_local_backend"


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
            client, project_id, _write_manifest(tmp_path)
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
            client, project_id, _write_manifest(tmp_path)
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


def test_workbench_plugin_lifecycle_rejects_unknown_plugin(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
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
            client, project_id, _write_manifest(tmp_path)
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
