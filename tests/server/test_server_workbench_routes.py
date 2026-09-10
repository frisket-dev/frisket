from __future__ import annotations

import pytest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from frisket.authoring.workbench import plugin_runtime_status as plugin_runtime
from frisket.server.routes.workbench import register_workbench_routes
from frisket.server.app import create_app


def test_workbench_plugin_frontend_component_module_byte_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin_id = "fixture.plugin"
    contribution_id = "fixture.plugin.view.main"
    package_sha256 = "sha256:fixture-package"
    manifest_sha256 = "sha256:fixture-manifest"
    module_source = b'export const fixture = "byte-contract";\n'
    manifest_path = tmp_path / "fixture.plugin.json"
    manifest_path.write_text("{}", encoding="utf-8")
    module_path = tmp_path / "frontend" / "main.js"
    module_path.parent.mkdir()
    module_path.write_bytes(module_source)
    manifest_ref = {
        "kind": "plugin_manifest",
        "schema_version": "frisket.plugin.v1",
        "plugin_id": plugin_id,
        "version": "1.2.3",
        "path": str(manifest_path),
        "manifest_sha256": manifest_sha256,
        "package_sha256": package_sha256,
        "byte_count": 321,
        "contributes": {"workbench_views": [contribution_id]},
        "requires": {"capabilities": [], "secrets": []},
        "runtime": {
            "workbench_components": [
                {
                    "contribution_id": contribution_id,
                    "module_key": "trustedLocal.fixturePlugin.module",
                    "component_key": "trustedLocal.fixturePlugin.MainView",
                    "module_path": "frontend/main.js",
                }
            ]
        },
    }
    # Enablement stays project-scoped; identity is the workspace catalog's.
    install_state = {
        "plugin_id": plugin_id,
        "install_state": "enabled",
        "arbitrary_package_load_allowed": False,
    }
    catalog_entry = {
        "plugin_id": plugin_id,
        "receipt_id": "receipt-fixture",
        "manifest_sha256": manifest_sha256,
        "package_sha256": package_sha256,
        "install_source": "{}",
        "runtime_source": "plugin.load_receipt",
        "has_executable_backend": 0,
    }

    class FixtureCatalog:
        def get(self, plugin_id: str) -> dict:
            return catalog_entry

        def manifest_ref(self, plugin_id: str) -> dict:
            return manifest_ref

    monkeypatch.setattr(
        plugin_runtime,
        "_workbench_plugin_install_state",
        lambda project, *, plugin_id: install_state,
    )
    monkeypatch.setattr(
        plugin_runtime,
        "plugin_package_catalog_for_project",
        lambda project: FixtureCatalog(),
    )
    monkeypatch.setattr(
        plugin_runtime,
        "_assert_manifest_ref_module_integrity",
        lambda manifest_ref, *, plugin_root, module_path: None,
    )

    class FixtureService:
        def frontend_component_module(
            self,
            project_id: str,
            *,
            plugin_id: str,
            contribution_id: str,
            expected_package_sha256: str | None = None,
        ) -> str:
            assert project_id == "project-fixture"
            return plugin_runtime.workbench_plugin_frontend_component_module(
                object(),
                plugin_id=plugin_id,
                contribution_id=contribution_id,
                expected_package_sha256=expected_package_sha256,
            )

    app = FastAPI()
    register_workbench_routes(app, service=FixtureService())  # type: ignore[arg-type]
    client = TestClient(app)
    module_url = (
        f"/api/projects/project-fixture/workbench/plugins/{plugin_id}"
        f"/frontend-components/{contribution_id}/module.js"
    )

    missing_digest = client.get(module_url)
    assert missing_digest.content == (
        b'{"detail":{"code":"plugin_frontend_module_package_required",'
        b'"message":"frontend module serving requires the package digest from '
        b'the runtime index URL","details":{"plugin_id":"fixture.plugin",'
        b'"contribution_id":"fixture.plugin.view.main"}}}'
    )
    assert dict(missing_digest.headers) == {
        "content-length": str(len(missing_digest.content)),
        "content-type": "application/json",
    }

    served = client.get(module_url, params={"package": package_sha256})
    assert served.content == module_source
    assert dict(served.headers) == {
        "cache-control": "no-store",
        "x-frisket-plugin-frontend-module": "trusted-local",
        "content-length": str(len(module_source)),
        "content-type": "application/javascript",
    }


@pytest.mark.real_bundled_plugins
def test_plugin_index_bootstraps_bundled_plugins_for_legacy_projects(tmp_path) -> None:
    """A project created BEFORE bundled plugins existed (no creation-time
    bootstrap) must gain them on its first runtime-index read — otherwise every
    bundled surface (the frisket.geo map view) silently disappears for legacy
    projects.
    bootstrap_project_bundled_plugins is idempotent and leaves explicit
    disable/uninstall alone, so the lazy ensure cannot re-enable an opt-out."""
    from fastapi.testclient import TestClient

    from frisket.engine.store.project import Project

    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    # Simulate the legacy path: create the project bundle DIRECTLY, bypassing
    # workspace.create's bootstrap (exactly what pre-bundled-era projects are).
    legacy = Project.create(workspace_root / "legacy.frisket", name="legacy")
    legacy.close()

    app = create_app(workspace_root)
    with TestClient(app) as client:
        index = client.get("/api/projects/legacy/workbench/plugins").json()
        plugin_ids = [p.get("pluginId") for p in index.get("plugins", [])]
        assert "frisket.geo" in plugin_ids, plugin_ids

        # Second read stays consistent (idempotent, memoized).
        again = client.get("/api/projects/legacy/workbench/plugins").json()
        assert [p.get("pluginId") for p in again.get("plugins", [])] == plugin_ids
