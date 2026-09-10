"""Bundled plugins automatically reload when their package identity drifts.

An enabled bundled plugin whose on-disk package
identity drifts from its install-state receipt is auto-refreshed by the
bundled-plugin bootstrap (``bootstrap_project_bundled_plugins``,
src/frisket/authoring/workbench/plugin_runtime.py — runs on every runtime-index read via
src/frisket/server/services/workbench.py and on workspace create), instead of
bricking every drift-affected surface until three hand-sequenced API calls.

The live incident this pins (wafflehouses, 2026-07-15): a bundled-source change
after the ``plugin.load`` receipt was recorded left (a) the frontend module
route failing closed with 409 ``plugin_code_integrity_mismatch`` ("Trusted-local
plugin UI failed to load.") and (b) the map-points route refusing with 409
``map_points_binding_missing`` because ``project_runtime_binding`` rejects a
registry binding whose ``package_sha256`` no longer matches the install-state
digest.

DONE means, for an enabled bundled plugin after drift (this file's first test):
- the runtime index shows the plugin still enabled with a CHANGED
  ``packageSha256`` (a fresh ``plugin.load`` receipt, not the stale one);
- the frontend module URL from that index serves 200 — no
  ``plugin_code_integrity_mismatch``;
- the role-``map_points`` projection runtime binding is re-registered, so the
  map-points route serves again (``project_runtime_binding`` returns it).

AND (second test) drift refresh never touches non-enabled states: a plugin the
operator disabled stays disabled across drift + reload ("disable survives
reload"; the same existing-state guard also protects uninstalled and
installed-resting auto_enable opt-outs).

Fixture idiom follows tests/test_workbench_plugin_geo_bundled.py: monkeypatch
``plugin_runtime._bundled_plugins_root`` (tests/conftest.py pins an empty root
suite-wide) and reset the process-global plugin registry around the test. This
file copies the REAL shipped frisket.geo package into a per-test bundled root
so it can mutate the package source to create genuine drift. An "app restart"
(the upgrade / git-pull scenario that surfaces stale receipts) is simulated by
resetting the registry and building a fresh app over the same workspace dir.

NOTE (probed while authoring the red, 2026-07-15): a plain restart with NO
drift also leaves the projection runtime binding unregistered until something
re-activates it — that registry-rehydration gap is SEPARATE from this task, so
no assertion here covers the no-drift restart case.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from frisket.authoring.plugin_registry import _reset_default_registry_for_tests
from frisket.server.app import create_app
from frisket.authoring.workbench import plugin_runtime, plugin_runtime_status


ROOT = Path(__file__).resolve().parents[2]
REAL_BUNDLED_GEO_ROOT = (
    ROOT / "src" / "frisket" / "authoring" / "bundled_plugins" / "frisket.geo"
)
GEO_PLUGIN_ID = "frisket.geo"
MAP_VIEW_CONTRIBUTION_ID = "frisket.geo.view.map"


@pytest.fixture()
def drifting_bundled_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A per-test bundled-plugins root holding a mutable COPY of the real
    shipped frisket.geo package, overriding the suite-wide hermetic empty
    root from tests/conftest.py (same override idiom as
    tests/test_workbench_plugin_geo_bundled.py's real_bundled_root — but a
    copy, because these tests mutate package source to create drift)."""
    bundled_root = tmp_path / "bundled_plugins"
    bundled_root.mkdir()
    shutil.copytree(
        REAL_BUNDLED_GEO_ROOT,
        bundled_root / GEO_PLUGIN_ID,
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    monkeypatch.setattr(plugin_runtime, "_bundled_plugins_root", lambda: bundled_root)
    monkeypatch.setattr(
        plugin_runtime_status, "_bundled_plugins_root", lambda: bundled_root
    )
    _reset_default_registry_for_tests()
    yield bundled_root
    _reset_default_registry_for_tests()


def _client(workspace: Path) -> TestClient:
    return TestClient(create_app(workspace))


def _restarted_client(workspace: Path) -> TestClient:
    """Simulate an app restart over the same on-disk workspace: the process
    registry is empty again and a fresh app (fresh WorkbenchService, so a
    fresh runtime-index bootstrap pass) serves the same projects — the state
    every project is in right after an app upgrade or dev `git pull` changes
    bundled plugin source."""
    _reset_default_registry_for_tests()
    return _client(workspace)


def _create_project(client: TestClient, name: str) -> str:
    response = client.post("/api/projects", json={"name": name})
    assert response.status_code == 200, response.text
    return response.json()["id"]


def _geo_index_entry(client: TestClient, project_id: str) -> dict:
    index = client.get(f"/api/projects/{project_id}/workbench/plugins")
    assert index.status_code == 200, index.text
    plugins = {item["pluginId"]: item for item in index.json()["plugins"]}
    assert GEO_PLUGIN_ID in plugins, "bootstrap must seed the bundled geo plugin"
    return plugins[GEO_PLUGIN_ID]


def _map_module_url(geo_entry: dict) -> str:
    bindings = [
        binding
        for binding in geo_entry["frontendComponentBindings"]
        if binding["contributionId"] == MAP_VIEW_CONTRIBUTION_ID
    ]
    assert bindings and bindings[0].get("moduleUrl"), (
        "the runtime index must carry the map view's moduleUrl binding"
    )
    return bindings[0]["moduleUrl"]


def _drift_geo_package(bundled_root: Path) -> None:
    """Mutate the bundled package's frontend module source — the exact drift
    shape of the live incident (commit e89e6b6f changed frontend/plugin.js
    after receipts were recorded)."""
    module = bundled_root / GEO_PLUGIN_ID / "frontend" / "plugin.js"
    module.write_text(
        module.read_text(encoding="utf-8") + "\n// drift: bundled source changed\n",
        encoding="utf-8",
    )


def test_enabled_bundled_plugin_auto_refreshes_on_package_drift(
    tmp_path: Path, drifting_bundled_root: Path
) -> None:
    workspace = tmp_path / "workspace"
    client = _client(workspace)
    project_id = _create_project(client, "drift autoreload")

    geo_before = _geo_index_entry(client, project_id)
    assert geo_before["installState"] == "enabled"
    stale_package_sha256 = geo_before["packageSha256"]
    assert stale_package_sha256.startswith("sha256:")

    # Baseline sanity before drift: the module route serves and the plugin's
    # role-map_points projection binding serves the map-points route.
    assert client.get(_map_module_url(geo_before)).status_code == 200
    project = client.app.state.workspace.get(project_id)
    sheet = project.add_sheet("places")
    geo_col = project.add_column(sheet, "location", type="geo_point")
    project.add_rows(
        sheet,
        [{"location": {"lat": 40.7128, "lon": -74.0060}}],
        {"location": geo_col},
    )
    map_points_url = (
        f"/api/projects/{project_id}/sheets/{sheet}/map/points"
        f"?column_id={geo_col}&format=arrow"
    )
    baseline_points = client.get(map_points_url)
    assert baseline_points.status_code == 200, baseline_points.text

    # The incident: bundled package source drifts after receipts were
    # recorded, then the app comes back up and reads the runtime index.
    _drift_geo_package(drifting_bundled_root)
    reloaded = _restarted_client(workspace)

    geo_after = _geo_index_entry(reloaded, project_id)
    assert geo_after["installState"] == "enabled", (
        "drift auto-refresh must keep the plugin enabled"
    )
    assert geo_after["packageSha256"].startswith("sha256:")
    assert geo_after["packageSha256"] != stale_package_sha256, (
        "an enabled bundled plugin whose on-disk package drifted from its "
        "install-state receipt must be auto-refreshed by the runtime-index "
        "bootstrap (a fresh plugin.load receipt with the LIVE package sha) — "
        "a stale receipt bricks the plugin's surfaces until three manual "
        "install/activate/backend-activate calls"
    )

    module_after = reloaded.get(_map_module_url(geo_after))
    assert module_after.status_code == 200, (
        "after drift auto-refresh the frontend module route must serve again "
        "(the incident's symptom was 409 plugin_code_integrity_mismatch / "
        "'Trusted-local plugin UI failed to load.'): "
        f"got {module_after.status_code}: {module_after.text}"
    )

    points_after = reloaded.get(map_points_url)
    assert points_after.status_code == 200, (
        "after drift auto-refresh the projection runtime binding must be "
        "re-registered (project_runtime_binding returns the role-map_points "
        "binding), so the map-points route serves instead of refusing with "
        f"map_points_binding_missing: got {points_after.status_code}: "
        f"{points_after.text}"
    )


def test_drift_refresh_never_reenables_a_disabled_plugin(
    tmp_path: Path, drifting_bundled_root: Path
) -> None:
    """'Disable survives reload': an explicit operator disable recorded BEFORE
    the package drifts must still hold after drift + reload — drift refresh is
    for enabled plugins only, never a backdoor re-enable of an opt-out."""
    workspace = tmp_path / "workspace"
    client = _client(workspace)
    project_id = _create_project(client, "drift disabled stays disabled")

    assert _geo_index_entry(client, project_id)["installState"] == "enabled"
    disabled = client.post(
        f"/api/projects/{project_id}/workbench/plugins/{GEO_PLUGIN_ID}/disable"
    )
    assert disabled.status_code == 200, disabled.text

    _drift_geo_package(drifting_bundled_root)
    reloaded = _restarted_client(workspace)

    geo_after = _geo_index_entry(reloaded, project_id)
    assert geo_after["installState"] == "disabled", (
        "a drifted package must NOT resurrect a plugin the operator disabled: "
        f"got installState={geo_after['installState']!r}"
    )
    assert geo_after["registryActivated"] is False
