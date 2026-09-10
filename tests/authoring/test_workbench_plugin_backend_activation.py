from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from frisket.authoring.plugin_registry import (
    default_registry,
    _reset_default_registry_for_tests,
)
from frisket.server.app import create_app


def _write_manifest(tmp_path: Path, *, plugin_id: str = "frisket-ndjson") -> Path:
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
        manifest_path = _write_manifest(tmp_path)

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
        manifest_path = _write_manifest(tmp_path)

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
        ndjson_manifest = _write_manifest(tmp_path, plugin_id="frisket-ndjson")
        other_manifest = _write_manifest(tmp_path, plugin_id="frisket-other")

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
