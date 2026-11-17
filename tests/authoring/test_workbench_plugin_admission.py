"""Every project-plugin entry point refuses evidence that disagrees with the catalog.

The workspace catalog row carries the package's manifest and package digests;
its manifest evidence (``manifest_ref``) carries the same digests and the
plugin id. Each entry point must refuse when any of the three disagree.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from frisket.authoring.plugin_registry import _reset_default_registry_for_tests
from frisket.authoring.workbench import plugin_runtime, plugin_runtime_status
from frisket.authoring.workbench.plugin_runtime import (
    activate_workbench_plugin_backend_contributions,
    activate_workbench_plugin_manifest,
)
from frisket.authoring.workbench.plugin_runtime_capabilities import (
    enabled_workbench_plugin_ids,
    project_runtime_binding,
)
from frisket.authoring.workbench.plugin_runtime_settings import (
    _loaded_manifest_for_plugin_env,
)
from frisket.authoring.workbench.plugin_runtime_shared import (
    WorkbenchPluginActivationError,
    WorkbenchPluginLifecycleError,
)
from frisket.authoring.workbench.plugin_runtime_status import (
    _effective_required_capabilities,
    _loaded_manifest_from_ref,
    workbench_plugin_frontend_component_module,
    workbench_plugin_runtime_index,
)
from frisket.server.app import create_app

ROOT = Path(__file__).resolve().parents[2]
BUNDLED_ROOT = ROOT / "src" / "frisket" / "authoring" / "bundled_plugins"
GEO = "frisket.geo"
MAP_VIEW = "frisket.geo.view.map"
MAP_POINTS = "frisket.geo.projection.map_points"
OTHER_SHA = "sha256:" + "0" * 64


@pytest.fixture()
def geo_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    for module in (plugin_runtime, plugin_runtime_status):
        monkeypatch.setattr(module, "_bundled_plugins_root", lambda: BUNDLED_ROOT)
    _reset_default_registry_for_tests()
    client = TestClient(create_app(tmp_path / "workspace"))
    project_id = client.post("/api/projects", json={"name": "admission"}).json()["id"]
    project = client.app.state.workspace.get(project_id)
    yield client, project, project_id
    _reset_default_registry_for_tests()


def _tamper(project: Any, mismatch: str) -> None:
    catalog = plugin_runtime_status.plugin_package_catalog_for_project(project)
    ref = catalog.manifest_ref(GEO)
    assert ref is not None
    if mismatch == "plugin_id":
        ref["plugin_id"] = "frisket.geo2"
    elif mismatch == "package_sha":
        ref["package_sha256"] = OTHER_SHA
    else:
        ref["manifest_sha256"] = OTHER_SHA
    conn = catalog._connect()
    try:
        conn.execute(
            "UPDATE plugin_packages SET manifest_ref=? WHERE plugin_id=?",
            (json.dumps(ref, separators=(",", ":"), sort_keys=True), GEO),
        )
        conn.commit()
    finally:
        conn.close()


def _frontend(project: Any, project_id: str) -> Any:
    entry = plugin_runtime_status.plugin_package_catalog_for_project(project).get(GEO)
    return workbench_plugin_frontend_component_module(
        project,
        plugin_id=GEO,
        contribution_id=MAP_VIEW,
        expected_package_sha256=entry["package_sha256"],
    )


def _runtime_binding(project: Any, project_id: str) -> Any:
    binding = next(
        spec
        for binding_type in ("projections",)
        for spec in plugin_runtime.default_registry().runtime_binding_specs(
            binding_type
        )
        if spec.kind == MAP_POINTS
    )
    admitted = project_runtime_binding(
        project, binding_type="projections", kind=binding.kind
    )
    if admitted is None:
        raise LookupError("binding not admitted")
    return admitted


def _enabled_ids(project: Any, project_id: str) -> Any:
    if GEO not in enabled_workbench_plugin_ids(project):
        raise LookupError("plugin not enabled")


def _activation(project: Any, project_id: str) -> Any:
    catalog = plugin_runtime_status.plugin_package_catalog_for_project(project)
    entry = catalog.get(GEO)
    required = _effective_required_capabilities(
        _loaded_manifest_from_ref(catalog.manifest_ref(GEO)).manifest
    )
    return activate_workbench_plugin_manifest(
        project,
        project_id=project_id,
        plugin_id=GEO,
        receipt_id=entry["receipt_id"],
        trust_acknowledged=True,
        permissions_accepted=sorted(required),
        arbitrary_package_load_allowed=False,
    )


def _backend_activation(project: Any, project_id: str) -> Any:
    return activate_workbench_plugin_backend_contributions(
        project,
        project_id=project_id,
        plugin_id=GEO,
        trust_acknowledged=True,
        arbitrary_package_load_allowed=False,
    )


def _plugin_env(project: Any, project_id: str) -> Any:
    return _loaded_manifest_for_plugin_env(project, plugin_id=GEO)


def _runtime_index(project: Any, project_id: str) -> Any:
    plugins = {
        item["pluginId"]: item
        for item in workbench_plugin_runtime_index(project, project_id=project_id)[
            "plugins"
        ]
    }
    if not plugins[GEO]["registryActivated"]:
        raise LookupError("plugin not activated")


ENTRY_POINTS: dict[str, Callable[[Any, str], Any]] = {
    "frontend_module": _frontend,
    "project_runtime_binding": _runtime_binding,
    "enabled_workbench_plugin_ids": _enabled_ids,
    "manifest_activation": _activation,
    "backend_activation": _backend_activation,
    "plugin_env": _plugin_env,
    "runtime_index": _runtime_index,
}
REFUSED = (
    LookupError,
    WorkbenchPluginActivationError,
    WorkbenchPluginLifecycleError,
)


@pytest.mark.parametrize("entry_point", sorted(ENTRY_POINTS))
@pytest.mark.parametrize("mismatch", ["plugin_id", "package_sha", "manifest_sha"])
def test_entry_point_refuses_catalog_evidence_mismatch(
    geo_project, entry_point: str, mismatch: str
) -> None:
    _client, project, project_id = geo_project
    admit = ENTRY_POINTS[entry_point]
    admit(project, project_id)

    _tamper(project, mismatch)

    with pytest.raises(REFUSED):
        admit(project, project_id)
