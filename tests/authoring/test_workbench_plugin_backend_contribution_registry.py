from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from frisket.authoring.column_types import get_column_type
from frisket.authoring.plugin_registry import (
    default_registry,
    _reset_default_registry_for_tests,
)
from frisket.server.app import create_app


TRUSTED_LOCAL_BACKEND_CAPABILITY = "plugin:trusted_local_backend"


def _write_manifest(
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
        manifest_path = _write_manifest(tmp_path)
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
        manifest_path = _write_manifest(tmp_path)
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
        manifest_path = _write_manifest(tmp_path)
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
        manifest_path = _write_manifest(tmp_path)
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
        owner_manifest = _write_manifest(
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

        shadow_manifest = _write_manifest(
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
        manifest_path = _write_manifest(
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
