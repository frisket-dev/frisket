from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from frisket.authoring.plugin_registry import _reset_default_registry_for_tests
from frisket.server.app import create_app
from frisket.authoring.workbench import plugin_runtime, plugin_runtime_status
from frisket.authoring.workbench.contracts import load_workbench_descriptor_package_file


ROOT = Path(__file__).resolve().parents[2]
BUNDLED_GEO_ROOT = (
    ROOT / "src" / "frisket" / "authoring" / "bundled_plugins" / "frisket.geo"
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
        lambda: ROOT / "src" / "frisket" / "authoring" / "bundled_plugins",
    )
    monkeypatch.setattr(
        plugin_runtime_status,
        "_bundled_plugins_root",
        lambda: ROOT / "src" / "frisket" / "authoring" / "bundled_plugins",
    )
    _reset_default_registry_for_tests()
    yield
    _reset_default_registry_for_tests()


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


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
    descriptors_source = _read("web/src/workbench/descriptors.ts")
    assert "export const MAP_VIEW_DESCRIPTOR" not in descriptors_source, (
        "MAP_VIEW_DESCRIPTOR must not exist as a first-party const — "
        "the map now arrives via the runtime index"
    )


# ---------------------------------------------------------------------------
# The plugin-owned map-points world is live.


def test_map_points_world_is_plugin_owned_and_disable_means_no_map(
    tmp_path: Path, real_bundled_root: None
) -> None:
    from frisket.server.services import map_points as map_points_service

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
