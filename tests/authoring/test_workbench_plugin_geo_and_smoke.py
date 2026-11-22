"""Bundled geo workbench plugin, mirror extract, and real local smoke tests."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from frisket.authoring.plugin_registry import (
    _reset_default_registry_for_tests,
    default_registry,
    get_trusted_backend_handler,
)
from frisket.authoring.workbench import plugin_runtime, plugin_runtime_status
from frisket.authoring.workbench.contracts import (
    load_workbench_descriptor_package_file,
    project_manifest,
)
from frisket.contracts.action import ActionResult
from frisket.engine.projections.runtime import projection_runtime_binding
from frisket.engine.store import Project
from frisket.server.app import create_app
from frisket.server.services import map_points as map_points_service

# --- geo bundled ---

GEO_BUNDLED_ROOT = Path(__file__).resolve().parents[2]
BUNDLED_GEO_ROOT = (
    GEO_BUNDLED_ROOT
    / "src"
    / "frisket"
    / "authoring"
    / "bundled_plugins"
    / "frisket.geo"
)
GEO_PLUGIN_ID = "frisket.geo"
MAP_VIEW_CONTRIBUTION_ID = "frisket.geo.view.map"
MAP_POINTS_PROJECTION_KIND = "frisket.geo.projection.map_points"


@pytest.fixture()
def real_bundled_root(monkeypatch: pytest.MonkeyPatch):
    """Point the bundled-plugins root at the REAL shipped tree, overriding
    the suite-wide hermetic empty root from tests/conftest.py."""
    monkeypatch.setattr(
        plugin_runtime,
        "_bundled_plugins_root",
        lambda: GEO_BUNDLED_ROOT / "src" / "frisket" / "authoring" / "bundled_plugins",
    )
    monkeypatch.setattr(
        plugin_runtime_status,
        "_bundled_plugins_root",
        lambda: GEO_BUNDLED_ROOT / "src" / "frisket" / "authoring" / "bundled_plugins",
    )
    _reset_default_registry_for_tests()
    yield
    _reset_default_registry_for_tests()


def _read(relative: str) -> str:
    return (GEO_BUNDLED_ROOT / relative).read_text(encoding="utf-8")


def _first_party_descriptor_ids() -> set[str]:
    # First-party descriptor data moved from a hand-authored TypeScript array
    # literal (``web/src/workbench/descriptors.ts``) to the checked-in JSON
    # package below — that package IS the first-party descriptor list now.
    artifact = json.loads(
        _read("src/frisket/data/first_party_workbench_descriptors.json")
    )
    return {descriptor["id"] for descriptor in artifact["descriptors"]}


# ---------------------------------------------------------------------------
# The package is a real SDK-built artifact set


def test_bundled_geo_is_a_real_sdk_built_package() -> None:
    assert BUNDLED_GEO_ROOT.is_dir(), (
        "src/frisket/authoring/bundled_plugins/frisket.geo/ must exist as the real "
        "SDK-authored geo plugin package"
    )
    # SDK authorship evidence: the config source AND the built artifacts.
    assert (BUNDLED_GEO_ROOT / "plugin.config.mjs").is_file()
    assert (BUNDLED_GEO_ROOT / "plugin.json").is_file()
    assert (BUNDLED_GEO_ROOT / "workbench-descriptors.json").is_file()
    assert (BUNDLED_GEO_ROOT / "plugin.py").is_file()
    module = BUNDLED_GEO_ROOT / "frontend" / "plugin.js"
    assert module.is_file()


def test_bundled_geo_descriptor_package_owns_the_map_view() -> None:
    package = load_workbench_descriptor_package_file(
        BUNDLED_GEO_ROOT / "workbench-descriptors.json"
    )
    descriptors = {
        descriptor["id"]: descriptor for descriptor in package.descriptor_manifests
    }
    map_view = descriptors[MAP_VIEW_CONTRIBUTION_ID]
    assert map_view["kind"] == "view"
    assert map_view["ownerPluginId"] == GEO_PLUGIN_ID
    assert map_view["projectionKind"] == MAP_POINTS_PROJECTION_KIND

    placements = {
        (placement["host"], placement["mode"]) for placement in map_view["placements"]
    }
    assert ("mainView", "pane") in placements
    # The map view is host-ROUTED via the column-scoped /map route, never an
    # activityRail launcher: that placement surfaced a dead ribbon tile (the
    # reveal handler's mainView branch relies on auto-mount, but the map is the
    # one main-view view excluded from auto-mount), so it was removed.
    assert ("activityRail", "command") not in placements

    required_capabilities = {
        requirement["id"]
        for requirement in map_view["requires"]
        if requirement.get("kind") == "hostCapability"
    }
    assert {
        "projection.status",
        "grid.filter.applyBbox",
        "host.navigation.openRow",
        "projection.data.read",
        "host.library.deckgl",
    } <= required_capabilities

    data_requirements = {
        (req.get("kind"), req.get("columnType"))
        for req in map_view.get("dataRequirements", [])
    }
    assert ("sheetHasColumnType", "geo_point") in data_requirements


# ---------------------------------------------------------------------------
# Bundled install path + runtime index


def _client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(tmp_path / "workspace"))


def _create_project(client: TestClient, name: str) -> str:
    response = client.post("/api/projects", json={"name": name})
    assert response.status_code == 200, response.text
    return response.json()["id"]


def test_bundled_geo_installs_via_bundled_path_into_the_runtime_index(
    tmp_path: Path, real_bundled_root: None
) -> None:
    client = _client(tmp_path)
    project_id = _create_project(client, "geo bundled runtime index")

    index = client.get(f"/api/projects/{project_id}/workbench/plugins")
    assert index.status_code == 200, index.text
    plugins = {item["pluginId"]: item for item in index.json()["plugins"]}
    assert GEO_PLUGIN_ID in plugins, (
        "project bootstrap must seed the bundled geo plugin through the "
        "public bundled install path"
    )
    geo = plugins[GEO_PLUGIN_ID]
    assert geo["installState"] == "enabled"
    assert geo["registryActivated"] is True
    assert geo["source"] == {"kind": "bundled", "value": "frisket.geo"}
    assert geo["manifestSha256"].startswith("sha256:")
    assert geo["packageSha256"].startswith("sha256:")

    map_bindings = [
        binding
        for binding in geo["frontendComponentBindings"]
        if binding["contributionId"] == MAP_VIEW_CONTRIBUTION_ID
    ]
    assert map_bindings, "the map view must have a frontend component binding"
    assert map_bindings[0]["moduleUrl"], (
        "the runtime index must carry frisket.geo.view.map with a moduleUrl "
        "binding — the map arrives via the runtime index, not the app bundle"
    )

    descriptor_ids = {
        descriptor.get("id")
        for descriptor in geo.get("workbenchDescriptorManifests") or []
    }
    assert MAP_VIEW_CONTRIBUTION_ID in descriptor_ids, (
        "the runtime index must carry the map view descriptor manifest"
    )


# ---------------------------------------------------------------------------
# The descriptor left the first-party world


def test_map_view_descriptor_is_no_longer_first_party() -> None:
    assert "frisket.geo.view.map" not in _first_party_descriptor_ids(), (
        "frisket.geo.view.map must leave the first-party descriptor package "
        "(src/frisket/data/first_party_workbench_descriptors.json) — the map "
        "now arrives via the runtime index"
    )


# ---------------------------------------------------------------------------
# The plugin-owned map-points world is live.


def test_map_points_world_is_plugin_owned_and_disable_means_no_map(
    tmp_path: Path, real_bundled_root: None
) -> None:

    assert map_points_service.MAP_POINTS_DESCRIPTOR_WORLD_IS_PLUGIN_OWNED is True, (
        "geo-bundled-plugin-v1 flips the flag for real — the bridge world "
        "(first-party descriptor, unconditional serving) is over"
    )

    client = _client(tmp_path)
    project_id = _create_project(client, "geo bundled map points")
    project = client.app.state.workspace.get(project_id)
    sheet = project.add_sheet("places")
    geo_col = project.add_column(sheet, "location", type="geo_point")
    project.add_rows(
        sheet,
        [{"location": {"lat": 40.7128, "lon": -74.0060}}],
        {"location": geo_col},
    )

    served = client.get(
        f"/api/projects/{project_id}/sheets/{sheet}/map/points"
        f"?column_id={geo_col}&format=arrow"
    )
    assert served.status_code == 200, served.text
    assert served.headers["X-Frisket-Map-Schema"] == "frisket.map_points.arrow.v1"
    # The REAL projection contribution drives the status side-channel.
    assert (
        served.headers.get("X-Frisket-Runtime-Projection-Kind")
        == MAP_POINTS_PROJECTION_KIND
    )

    disabled = client.post(
        f"/api/projects/{project_id}/workbench/plugins/{GEO_PLUGIN_ID}/disable"
    )
    assert disabled.status_code == 200, disabled.text

    refused = client.get(
        f"/api/projects/{project_id}/sheets/{sheet}/map/points"
        f"?column_id={geo_col}&format=arrow"
    )
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["code"] == "map_points_binding_missing"


# --- geo mirror ---

GEO_MIRROR_ROOT = (
    Path(__file__).parent.parent / "fixtures" / "local_plugins" / "frisket_geo_smoke"
)
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
            "source": {
                "kind": "localPath",
                "value": str(GEO_MIRROR_ROOT / "plugin.json"),
            },
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
            "sourcePath": str(GEO_MIRROR_ROOT / "workbench-descriptors.json"),
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


# --- local smoke ---

LOCAL_SMOKE_ROOT = (
    Path(__file__).parent.parent / "fixtures" / "local_plugins" / "frisket_geo_smoke"
)
# The twin owns its OWN projection kind in the frisket.geosmoke namespace; the
# map/points route resolves the binding by its execution role (=map_points),
# not by the bundled plugin's canonical projection-kind string.


def _plugin_load_action(manifest_path: Path) -> dict[str, Any]:
    manifest_hash = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    return {
        "action_id": "plugin.load",
        "scope": {"kind": "project"},
        "params": {"manifest": {"kind": "local_file", "path": str(manifest_path)}},
        "idempotency_key": f"real-local-plugin-smoke@sha256:{manifest_hash}",
    }


def test_real_local_smoke_handler_is_not_registered_without_test_opt_in(
    monkeypatch,
) -> None:
    monkeypatch.delenv("FRISKET_ENABLE_REAL_LOCAL_PLUGIN_SMOKE_HANDLER", raising=False)
    _reset_default_registry_for_tests()
    try:
        default_registry()
        assert get_trusted_backend_handler(SMOKE_HANDLER_KEY) is None
        assert get_trusted_backend_handler(SMOKE_ACTION_HANDLER_KEY) is None
        assert get_trusted_backend_handler(SMOKE_IMPORTER_HANDLER_KEY) is None
        assert get_trusted_backend_handler(SMOKE_OPERATOR_HANDLER_KEY) is None
    finally:
        _reset_default_registry_for_tests()


def test_real_local_descriptor_sidecar_must_match_manifest_contributions(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("FRISKET_ENABLE_REAL_LOCAL_PLUGIN_SMOKE_HANDLER", "1")
    _reset_default_registry_for_tests()
    try:
        plugin_root = tmp_path / "frisket_geo_smoke"
        shutil.copytree(LOCAL_SMOKE_ROOT, plugin_root)
        descriptor_path = plugin_root / "workbench-descriptors.json"
        descriptor_doc = json.loads(descriptor_path.read_text(encoding="utf-8"))
        descriptor_doc["descriptors"][0]["id"] = "frisket.geosmoke.view.undeclared"
        descriptor_path.write_text(
            json.dumps(descriptor_doc, indent=2), encoding="utf-8"
        )

        workspace = tmp_path / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        project_path = workspace / "invalid-sidecar.frisket"
        Project.create(project_path, name="Invalid descriptor sidecar").close()

        client = TestClient(create_app(workspace))
        load_response = client.post(
            "/api/projects/invalid-sidecar/actions/v1/run",
            params={},
            json=_plugin_load_action(plugin_root / "plugin.json"),
        )
        assert load_response.status_code == 400, load_response.text
        load_result = ActionResult.model_validate(load_response.json())
        assert load_result.status == "failed"
        assert load_result.errors[0].code == "invalid_workbench_descriptor_package"
        assert (
            load_result.errors[0].field == "params.manifest.workbench_descriptors_path"
        )
        assert load_result.errors[0].details["path"] == str(descriptor_path)
        assert "undeclared contributions" in load_result.errors[0].message
    finally:
        _reset_default_registry_for_tests()


def test_real_local_descriptor_sidecar_must_cover_runtime_component_bindings(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("FRISKET_ENABLE_REAL_LOCAL_PLUGIN_SMOKE_HANDLER", "1")
    _reset_default_registry_for_tests()
    try:
        plugin_root = tmp_path / "frisket_geo_smoke"
        shutil.copytree(LOCAL_SMOKE_ROOT, plugin_root)
        descriptor_path = plugin_root / "workbench-descriptors.json"
        descriptor_doc = json.loads(descriptor_path.read_text(encoding="utf-8"))
        descriptor_doc["descriptors"][0].update(
            {
                "schemaVersion": "frisket.workbench.panel.v1",
                "id": "frisket.geosmoke.panel.uncovered_probe",
                "kind": "workbench.panel",
            }
        )
        descriptor_path.write_text(
            json.dumps(descriptor_doc, indent=2), encoding="utf-8"
        )
        # Declare the probe id so the namespacing/undeclared gates (which now
        # run first) pass and the load reaches the missing-runtime-component
        # coverage check this test pins.
        manifest_path = plugin_root / "plugin.json"
        manifest_doc = json.loads(manifest_path.read_text(encoding="utf-8"))
        panels = manifest_doc["contributes"].setdefault("workbench_panels", [])
        panels.append("frisket.geosmoke.panel.uncovered_probe")
        manifest_path.write_text(json.dumps(manifest_doc, indent=2), encoding="utf-8")

        workspace = tmp_path / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        project_path = workspace / "missing-runtime-component.frisket"
        Project.create(project_path, name="Missing runtime component").close()

        client = TestClient(create_app(workspace))
        load_response = client.post(
            "/api/projects/missing-runtime-component/actions/v1/run",
            params={},
            json=_plugin_load_action(plugin_root / "plugin.json"),
        )
        assert load_response.status_code == 400, load_response.text
        load_result = ActionResult.model_validate(load_response.json())
        assert load_result.status == "failed"
        assert load_result.errors[0].code == "invalid_workbench_descriptor_package"
        assert "missing runtime component" in load_result.errors[0].message
        assert "frisket.geosmoke.view.real_local_smoke" in load_result.errors[0].message
    finally:
        _reset_default_registry_for_tests()


def test_real_local_descriptor_sidecar_symlink_is_rejected(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("FRISKET_ENABLE_REAL_LOCAL_PLUGIN_SMOKE_HANDLER", "1")
    _reset_default_registry_for_tests()
    try:
        plugin_root = tmp_path / "frisket_geo_smoke"
        shutil.copytree(LOCAL_SMOKE_ROOT, plugin_root)
        descriptor_path = plugin_root / "workbench-descriptors.json"
        descriptor_path.unlink()
        try:
            descriptor_path.symlink_to(LOCAL_SMOKE_ROOT / "workbench-descriptors.json")
        except OSError as exc:
            pytest.skip(f"symlink creation is unavailable: {exc}")

        workspace = tmp_path / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        project_path = workspace / "symlink-sidecar.frisket"
        Project.create(project_path, name="Symlink descriptor sidecar").close()

        client = TestClient(create_app(workspace))
        load_response = client.post(
            "/api/projects/symlink-sidecar/actions/v1/run",
            params={},
            json=_plugin_load_action(plugin_root / "plugin.json"),
        )
        assert load_response.status_code == 400, load_response.text
        load_result = ActionResult.model_validate(load_response.json())
        assert load_result.status == "failed"
        assert (
            load_result.errors[0].code == "invalid_workbench_descriptor_package_source"
        )
        assert (
            load_result.errors[0].field == "params.manifest.workbench_descriptors_path"
        )
        assert "regular file" in load_result.errors[0].message
    finally:
        _reset_default_registry_for_tests()


def test_real_local_plugin_descriptor_generation_load_activation_and_runtime_dispatch(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("FRISKET_ENABLE_REAL_LOCAL_PLUGIN_SMOKE_HANDLER", "1")
    _reset_default_registry_for_tests()
    try:
        descriptor_doc = json.loads(
            (LOCAL_SMOKE_ROOT / "workbench-descriptors.json").read_text(
                encoding="utf-8"
            )
        )
        descriptors = {
            descriptor["id"]: descriptor for descriptor in descriptor_doc["descriptors"]
        }
        smoke_descriptor = descriptors["frisket.geosmoke.view.real_local_smoke"]
        map_descriptor = descriptors["frisket.geosmoke.view.map"]
        smoke_manifest_descriptor = project_manifest(smoke_descriptor)
        map_manifest_descriptor = project_manifest(map_descriptor)
        assert smoke_descriptor["componentKey"] == (
            "trustedLocal.frisketGeo.realLocalSmoke.MapView"
        )
        assert map_descriptor["componentKey"] == (
            "trustedLocal.frisketGeo.realLocalSmoke.MapView"
        )
        assert smoke_manifest_descriptor["id"] == (
            "frisket.geosmoke.view.real_local_smoke"
        )
        assert map_manifest_descriptor["id"] == "frisket.geosmoke.view.map"
        assert "componentKey" not in smoke_manifest_descriptor
        assert "componentKey" not in map_manifest_descriptor

        workspace = tmp_path / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        project_path = workspace / "real-local-smoke.frisket"
        project = Project.create(project_path, name="Real local plugin smoke")
        sheet_id = project.add_sheet("places")
        name_col = project.add_column(sheet_id, "name", type="text")
        geo_col = project.add_column(sheet_id, "point", type="geo_point")
        rows = project.add_rows(
            sheet_id,
            [{"name": "NYC", "point": {"lat": 40.7128, "lon": -74.006}}],
            {"name": name_col, "point": geo_col},
        )
        project.close()

        client = TestClient(create_app(workspace))
        project_id = "real-local-smoke"
        discover_response = client.get(f"/api/projects/{project_id}/sheets")
        assert discover_response.status_code == 200, discover_response.text
        install_response = client.post(
            f"/api/projects/{project_id}/workbench/plugins/{PLUGIN_ID}/install-local",
            json={
                "source": {
                    "kind": "localPath",
                    "value": str(LOCAL_SMOKE_ROOT / "plugin.json"),
                },
                "arbitraryPackageLoadAllowed": False,
            },
        )
        assert install_response.status_code == 200, install_response.text
        install_receipt_id = install_response.json()["receiptId"]
        assert install_receipt_id

        index_response = client.get(f"/api/projects/{project_id}/workbench/plugins")
        assert index_response.status_code == 200, index_response.text
        index = index_response.json()
        plugin = next(
            item for item in index["plugins"] if item["pluginId"] == PLUGIN_ID
        )
        bare_module_url = (
            f"/api/projects/{project_id}/workbench/plugins/{PLUGIN_ID}"
            "/frontend-components/frisket.geosmoke.view.map/module.js"
        )
        assert plugin["packageSha256"].startswith("sha256:")
        frontend_bindings = {
            binding["contributionId"]: binding
            for binding in plugin["frontendComponentBindings"]
        }
        module_url = frontend_bindings["frisket.geosmoke.view.map"]["moduleUrl"]
        assert module_url.startswith(f"{bare_module_url}?package=sha256%3A")
        assert plugin["runtimeSource"] == "plugin.load_receipt"
        assert plugin["receiptId"] == install_receipt_id
        assert plugin["source"] == {
            "kind": "localPath",
            "value": str(LOCAL_SMOKE_ROOT / "plugin.json"),
        }
        assert plugin["contributionSummary"] == [
            {
                "kind": "workbench_view",
                "count": 2,
                "ids": [
                    "frisket.geosmoke.view.real_local_smoke",
                    "frisket.geosmoke.view.map",
                ],
            },
            {
                "kind": "workbench_panel",
                "count": 1,
                "ids": ["frisket.core.panel.projection_status"],
            },
            {"kind": "action", "count": 1, "ids": [SMOKE_ACTION_KIND]},
            {"kind": "importer", "count": 1, "ids": [SMOKE_IMPORTER_KIND]},
            {"kind": "operator", "count": 1, "ids": [SMOKE_OPERATOR_KIND]},
            {
                "kind": "projection",
                "count": 1,
                "ids": [SMOKE_PROJECTION_KIND],
            },
        ]
        assert frontend_bindings == {
            "frisket.geosmoke.view.real_local_smoke": {
                "schemaVersion": (
                    "frisket.workbench_plugin_frontend_component_binding.v1"
                ),
                "contributionId": "frisket.geosmoke.view.real_local_smoke",
                "moduleKey": "trustedLocal.frisketGeo.realLocalSmoke",
                "componentKey": "trustedLocal.frisketGeo.realLocalSmoke.MapView",
                "modulePath": "frontend/map-view.js",
                "moduleUrl": frontend_bindings[
                    "frisket.geosmoke.view.real_local_smoke"
                ]["moduleUrl"],
            },
            "frisket.geosmoke.view.map": {
                "schemaVersion": (
                    "frisket.workbench_plugin_frontend_component_binding.v1"
                ),
                "contributionId": "frisket.geosmoke.view.map",
                "moduleKey": "trustedLocal.frisketGeo.realLocalSmoke",
                "componentKey": "trustedLocal.frisketGeo.realLocalSmoke.MapView",
                "modulePath": "frontend/map-view.js",
                "moduleUrl": module_url,
            },
        }
        smoke_module_url = frontend_bindings["frisket.geosmoke.view.real_local_smoke"][
            "moduleUrl"
        ]
        assert smoke_module_url.startswith(
            f"/api/projects/{project_id}/workbench/plugins/{PLUGIN_ID}"
            "/frontend-components/frisket.geosmoke.view.real_local_smoke/module.js"
            "?package=sha256%3A"
        )
        assert plugin["workbenchDescriptorPackage"] == {
            "schemaVersion": "frisket.workbench_descriptor_package.v1",
            "sourcePath": str(LOCAL_SMOKE_ROOT / "workbench-descriptors.json"),
            "descriptorCount": 2,
            "runtimeOnlyFieldsStripped": ["componentKey"],
        }
        assert len(plugin["workbenchDescriptorManifests"]) == 2
        descriptor_manifests = {
            descriptor["id"]: descriptor
            for descriptor in plugin["workbenchDescriptorManifests"]
        }
        smoke_descriptor_manifest = descriptor_manifests[
            "frisket.geosmoke.view.real_local_smoke"
        ]
        assert smoke_descriptor_manifest["schemaVersion"] == (
            "frisket.workbench.view.v1"
        )
        assert smoke_descriptor_manifest["ownerPluginId"] == PLUGIN_ID
        assert smoke_descriptor_manifest["kind"] == "view"
        assert smoke_descriptor_manifest["placements"] == [
            {
                "host": "mainView",
                "mode": "pane",
                "slot": "work.companion",
                "placementId": "frisket-geo-real-local-smoke",
                "order": -10,
            }
        ]
        assert smoke_descriptor_manifest["dataRequirements"] == [
            {"kind": "activeSheet"}
        ]
        assert "componentKey" not in smoke_descriptor_manifest

        descriptor_manifest = descriptor_manifests["frisket.geosmoke.view.map"]
        assert descriptor_manifest["schemaVersion"] == "frisket.workbench.view.v1"
        assert descriptor_manifest["id"] == "frisket.geosmoke.view.map"
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
        assert "componentKey" not in descriptor_manifest

        blocked_module = client.get(module_url)
        assert blocked_module.status_code == 409, blocked_module.text
        assert blocked_module.json()["detail"]["code"] == (
            "plugin_frontend_module_not_enabled"
        )

        activation_response = client.post(
            f"/api/projects/{project_id}/workbench/plugins/{PLUGIN_ID}/activate",
            json={
                "receiptId": install_receipt_id,
                "trustAcknowledged": True,
                "permissionsAccepted": [TRUSTED_LOCAL_BACKEND_CAPABILITY],
                "arbitraryPackageLoadAllowed": False,
            },
        )
        assert activation_response.status_code == 200, activation_response.text
        activation = activation_response.json()
        assert activation["installState"] == "enabled"
        assert activation["runtimeSource"] == "plugin.load_receipt"
        assert activation["registryActivated"] is True

        missing_digest_module = client.get(bare_module_url)
        assert missing_digest_module.status_code == 409, missing_digest_module.text
        assert missing_digest_module.json()["detail"]["code"] == (
            "plugin_frontend_module_package_required"
        )
        wrong_digest_module = client.get(f"{bare_module_url}?package=sha256%3Abad")
        assert wrong_digest_module.status_code == 409, wrong_digest_module.text
        assert wrong_digest_module.json()["detail"]["code"] == (
            "plugin_frontend_module_package_mismatch"
        )

        module_response = client.get(module_url)
        assert module_response.status_code == 200, module_response.text
        assert module_response.headers["content-type"].startswith(
            "application/javascript"
        )
        assert (
            module_response.headers["x-frisket-plugin-frontend-module"]
            == "trusted-local"
        )
        assert "trusted-local-smoke-map-plugin-ui" in module_response.text

        metadata_only = client.post(
            f"/api/projects/{project_id}/workbench/plugins/{PLUGIN_ID}/backend/activate",
            json={
                "trustAcknowledged": True,
                "arbitraryPackageLoadAllowed": False,
            },
        )
        assert metadata_only.status_code == 200, metadata_only.text
        assert metadata_only.json()["registeredExecutableHandlers"] == {
            "jobHandlers": [],
        }
        assert metadata_only.json()["registeredRuntimeBindings"] == {
            "actions": [],
            "importers": [],
            "operators": [],
            "projections": [],
            "jobHandlers": [],
        }
        assert projection_runtime_binding(SMOKE_PROJECTION_KIND) is None

        executable = client.post(
            f"/api/projects/{project_id}/workbench/plugins/{PLUGIN_ID}/backend/activate",
            json={
                "trustAcknowledged": True,
                "arbitraryPackageLoadAllowed": False,
                "executableHandlersAllowed": True,
            },
        )
        assert executable.status_code == 200, executable.text
        assert executable.json()["registeredExecutableHandlers"] == {
            "jobHandlers": [],
        }
        assert executable.json()["registeredRuntimeBindings"] == {
            "actions": [SMOKE_ACTION_KIND],
            "importers": [SMOKE_IMPORTER_KIND],
            "operators": [SMOKE_OPERATOR_KIND],
            "projections": [SMOKE_PROJECTION_KIND],
            "jobHandlers": [],
        }

        binding = projection_runtime_binding(SMOKE_PROJECTION_KIND)
        assert binding is not None
        assert binding.plugin == PLUGIN_ID
        assert binding.handler_key == SMOKE_HANDLER_KEY

        points_response = client.get(
            f"/api/projects/{project_id}/sheets/{sheet_id}/map/points",
            params={"column_id": geo_col, "attrs": str(name_col)},
        )
        assert points_response.status_code == 200, points_response.text
        assert (
            points_response.headers["x-frisket-runtime-projection-kind"]
            == SMOKE_PROJECTION_KIND
        )
        assert points_response.headers["x-frisket-runtime-projection-status"] == "stale"
        assert (
            points_response.headers["x-frisket-runtime-projection-generation"]
            == "real-local-smoke-gen-1"
        )
        assert (
            points_response.headers[
                "x-frisket-runtime-projection-build-idempotency-key"
            ]
            == "real-local-smoke-map-points@gen-2"
        )
        assert (
            points_response.headers["x-frisket-runtime-projection-artifacts"]
            == "projection://real-local-smoke/gen-1"
        )

        action_response = client.post(
            f"/api/projects/{project_id}/actions/v1/run",
            params={},
            json={
                "action_id": SMOKE_ACTION_KIND,
                "scope": {
                    "kind": "sheet_rows",
                    "sheet_id": sheet_id,
                    "row_ids": rows,
                },
                "params": {"name": "name"},
                "idempotency_key": "real-local-smoke-action@sha256:v1",
            },
        )
        assert action_response.status_code == 200, action_response.text
        action_body = action_response.json()
        assert action_body["status"] == "completed"
        assert len(action_body["outputs"]) == 1
        action_output = action_body["outputs"][0]
        assert action_output["kind"] == "column"
        assert action_output["name"] == "smoke_result"
        assert action_output["ref"]["sheet_id"] == sheet_id
        assert set(action_output["ref"]["row_ids"]) == set(rows)
        smoke_result_col = action_output["ref"]["column_id"]
        runtime_project = client.app.state.workspace.get(project_id)
        assert runtime_project.get_values(sheet_id, smoke_result_col) == {
            rows[0]: "real-local-action:NYC"
        }

        import_response = client.post(
            f"/api/projects/{project_id}/actions/v1/run",
            params={},
            json={
                "action_id": "import.runtime",
                "scope": {"kind": "project"},
                "sheet_name": "Real Local Runtime Import",
                "params": {
                    "importer_kind": SMOKE_IMPORTER_KIND,
                    "source": {
                        "kind": "runtime",
                        "label": "real-local-smoke.fixture",
                        "fingerprint": "sha256:real-local-smoke",
                    },
                    "handler_params": {"city": "New York"},
                },
                "idempotency_key": "real-local-smoke-import@sha256:v1",
            },
        )
        assert import_response.status_code == 200, import_response.text
        import_body = import_response.json()
        assert import_body["status"] == "completed"

        runtime_project = client.app.state.workspace.get(project_id)
        import_sheet = runtime_project.db.execute(
            "SELECT id FROM sheets WHERE name='Real Local Runtime Import'"
        ).fetchone()
        assert import_sheet is not None
        name_column = runtime_project.db.execute(
            "SELECT id FROM columns WHERE sheet_id=? AND name='name'",
            (import_sheet["id"],),
        ).fetchone()
        assert name_column is not None

        filtered_import = client.get(
            f"/api/projects/{project_id}/sheets/{import_sheet['id']}/data",
            params={
                "filter": json.dumps(
                    {"name": {SMOKE_OPERATOR_KIND: "New York"}},
                    separators=(",", ":"),
                )
            },
        )
        assert filtered_import.status_code == 200, filtered_import.text
        filtered_body = filtered_import.json()
        assert [
            row["cells"][str(name_column["id"])] for row in filtered_body["rows"]
        ] == ["New York"]
        assert rows
    finally:
        _reset_default_registry_for_tests()


def test_installing_a_different_package_replaces_the_one_workspace_identity(
    tmp_path: Path, monkeypatch
) -> None:
    """Single-workspace-authority rewrite of the old
    plugin_activation_registry_conflict guard.

    Under single-workspace authority a plugin_id maps to EXACTLY ONE package in
    the workspace catalog. Installing a byte-different package under the SAME id
    REPLACES that one entry (last-writer-wins) rather than being refused as a
    cross-project shadow — there is no second slot to shadow. The
    cross-project-contention 409 (plugin_activation_registry_conflict) was
    deleted with that cutover; genuine live package-identity integrity checks remain
    (e.g. an activation receipt that no longer matches the catalog's current
    identity is refused). Here we prove the replace: a genuine fixture is
    installed + activated, then a byte-different copy under the same id installs
    over it and activates cleanly, leaving ONE workspace identity at the new sha.
    """
    monkeypatch.setenv("FRISKET_ENABLE_REAL_LOCAL_PLUGIN_SMOKE_HANDLER", "1")
    _reset_default_registry_for_tests()
    try:
        workspace = tmp_path / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)

        # Incumbent: install + activate the genuine fixture -> workspace catalog
        # identity = sha_A.
        Project.create(workspace / "incumbent.frisket", name="Incumbent").close()
        client = TestClient(create_app(workspace))
        catalog = client.app.state.workspace.plugin_package_catalog
        incumbent_install = client.post(
            f"/api/projects/incumbent/workbench/plugins/{PLUGIN_ID}/install-local",
            json={
                "source": {
                    "kind": "localPath",
                    "value": str(LOCAL_SMOKE_ROOT / "plugin.json"),
                },
                "arbitraryPackageLoadAllowed": False,
            },
        )
        assert incumbent_install.status_code == 200, incumbent_install.text
        incumbent_receipt = incumbent_install.json()["receiptId"]
        incumbent_activate = client.post(
            f"/api/projects/incumbent/workbench/plugins/{PLUGIN_ID}/activate",
            json={
                "receiptId": incumbent_receipt,
                "trustAcknowledged": True,
                "permissionsAccepted": [TRUSTED_LOCAL_BACKEND_CAPABILITY],
                "arbitraryPackageLoadAllowed": False,
            },
        )
        assert incumbent_activate.status_code == 200, incumbent_activate.text
        assert incumbent_activate.json()["registryActivated"] is True
        sha_a = catalog.get(PLUGIN_ID)["manifest_sha256"]

        # Shadow: SAME id, DIFFERENT manifest bytes (a benign version bump keeps
        # the manifest valid while changing its sha), installed from a second
        # project sharing the same workspace catalog.
        shadow_root = tmp_path / "shadow_pkg"
        shutil.copytree(LOCAL_SMOKE_ROOT, shadow_root)
        shadow_manifest = shadow_root / "plugin.json"
        shadow_doc = json.loads(shadow_manifest.read_text(encoding="utf-8"))
        assert shadow_doc["id"] == PLUGIN_ID
        shadow_doc["version"] = "0.1.1"
        shadow_manifest.write_text(json.dumps(shadow_doc, indent=2), encoding="utf-8")

        Project.create(workspace / "shadow.frisket", name="Shadow").close()
        shadow_install = client.post(
            f"/api/projects/shadow/workbench/plugins/{PLUGIN_ID}/install-local",
            json={
                "source": {"kind": "localPath", "value": str(shadow_manifest)},
                "arbitraryPackageLoadAllowed": False,
            },
        )
        assert shadow_install.status_code == 200, shadow_install.text
        shadow_receipt = shadow_install.json()["receiptId"]

        # The workspace catalog now holds the shadow identity (one slot, replaced).
        sha_b = catalog.get(PLUGIN_ID)["manifest_sha256"]
        assert sha_b != sha_a

        shadow_activate = client.post(
            f"/api/projects/shadow/workbench/plugins/{PLUGIN_ID}/activate",
            json={
                "receiptId": shadow_receipt,
                "trustAcknowledged": True,
                "permissionsAccepted": [TRUSTED_LOCAL_BACKEND_CAPABILITY],
                "arbitraryPackageLoadAllowed": False,
            },
        )
        assert shadow_activate.status_code == 200, shadow_activate.text
        assert shadow_activate.json()["registryActivated"] is True
        assert shadow_activate.json()["manifestSha256"] == sha_b
    finally:
        _reset_default_registry_for_tests()
