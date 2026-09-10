"""Bundled plugin runtime-binding rehydration after restart.

After a plain backend restart with no package drift, an enabled bundled plugin's
backend contributions — specifically its executable projection runtime
binding — are rehydrated automatically, so projection-backed surfaces serve
without any manual ``/backend/activate`` poke.

The live incident this pins (wafflehouses, 2026-07-15 evening): after
restarting the dev backend, the map showed "no plugin owns the map_points
projection role for this project" — the map-points route refused with
409 ``map_points_binding_missing``
(``src/frisket/server/services/map_points.py:379``) — while frisket.geo sat
``installState: enabled`` in the runtime index. Recovery took one manual
``POST .../frisket.geo/backend/activate`` with
``{"trustAcknowledged":true,"executableHandlersAllowed":true}`` per project.
Root cause: only ``activate_workbench_plugin_backend_contributions`` with
``executable_handlers_allowed=True`` calls
``_register_executable_runtime_bindings``
(``src/frisket/authoring/workbench/plugin_runtime.py``); the bundled bootstrap skips
enabled-no-drift installs by design and the runtime-index read registers
manifests but not executable runtime bindings. So EVERY restart silently
breaks every projection-backed surface for every project until hand-poked.

This is exactly the case tests/test_bundled_plugin_drift_autoreload.py
deliberately excludes (its module NOTE): that task covers restart WITH
package drift; this one covers the plain restart with NO source mutation.

DONE means (this file's first test): bootstrap a project with the real
frisket.geo package (enabled), serve map-points 200 as baseline, then
simulate a plain restart (registry reset + fresh app over the same
workspace, package bytes UNTOUCHED) — and the map-points route serves 200
again in the restarted app WITHOUT any activate/backend-activate call.

Guards that must hold in BOTH worlds (green at admission, must survive the
rehydration work):
- A plugin the operator DISABLED before restart does not rehydrate: after
  restart the map-points route refuses 409 ``map_points_binding_missing``
  ("disabled geo plugin means no map" — pinned green today by
  tests/test_workbench_plugin_geo_bundled.py's disable case).
- The runtime-index read that triggers rehydration stays idempotent: two
  consecutive index reads in the restarted app leave the plugin enabled with
  the SAME ``receiptId``/``packageSha256`` — no receipt churn from
  rehydration. Rehydration restores previously consented state via the
  persisted ``permissions_accepted``
  evidence; it never re-runs ``plugin.load`` or grants new consent.

Fixture idiom follows tests/test_bundled_plugin_drift_autoreload.py /
tests/test_plugin_load_project_capabilities.py: a per-test bundled root
holding a COPY of the real shipped frisket.geo package (monkeypatching
``plugin_runtime._bundled_plugins_root`` over the suite-wide hermetic empty
root from tests/conftest.py), process-global registry reset around each
test, and a restart simulated as registry reset + fresh app over the same
workspace dir. This file never mutates the package source — no drift, ever.
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


@pytest.fixture()
def geo_bundled_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A per-test bundled-plugins root holding a copy of the real shipped
    frisket.geo package, overriding the suite-wide hermetic empty root from
    tests/conftest.py. A copy (not the live tree) keeps the idiom identical
    to the drift test's fixture, but NOTHING in this file mutates it — the
    whole point is a restart with zero package drift."""
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
    """Simulate a PLAIN app restart over the same on-disk workspace: the
    process registry is empty again and a fresh app (fresh WorkbenchService,
    so a fresh runtime-index bootstrap pass) serves the same projects. No
    package bytes changed — this is every ordinary backend restart."""
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


def _seed_geo_sheet(client: TestClient, project_id: str) -> str:
    """One sheet with a geo_point column and one row; returns the map-points
    URL that the role-map_points projection runtime binding serves."""
    project = client.app.state.workspace.get(project_id)
    sheet = project.add_sheet("places")
    geo_col = project.add_column(sheet, "location", type="geo_point")
    project.add_rows(
        sheet,
        [{"location": {"lat": 40.7128, "lon": -74.0060}}],
        {"location": geo_col},
    )
    return (
        f"/api/projects/{project_id}/sheets/{sheet}/map/points"
        f"?column_id={geo_col}&format=arrow"
    )


def test_enabled_plugin_projection_binding_rehydrates_on_plain_restart(
    tmp_path: Path, geo_bundled_root: Path
) -> None:
    """RED CORE: the plain no-drift restart. Baseline map-points 200 in the
    first app; then a restart with NOTHING changed on disk must serve
    map-points 200 again — with no activate/backend-activate call anywhere
    in this test."""
    workspace = tmp_path / "workspace"
    client = _client(workspace)
    project_id = _create_project(client, "restart rehydration")

    geo_before = _geo_index_entry(client, project_id)
    assert geo_before["installState"] == "enabled"
    map_points_url = _seed_geo_sheet(client, project_id)
    baseline = client.get(map_points_url)
    assert baseline.status_code == 200, baseline.text

    # The incident: a plain restart. No package drift, no manual pokes.
    restarted = _restarted_client(workspace)

    # The incident's exact shape: the runtime index still says enabled...
    geo_after = _geo_index_entry(restarted, project_id)
    assert geo_after["installState"] == "enabled", (
        "a plain restart must not change an enabled plugin's install state: "
        f"got {geo_after['installState']!r}"
    )

    # ...and the projection-backed surface must serve WITHOUT a manual
    # backend/activate poke. Today this is the red: every restart 409s here.
    rehydrated = restarted.get(map_points_url)
    assert rehydrated.status_code == 200, (
        "after a plain backend restart (NO package drift) an ENABLED bundled "
        "plugin's executable projection runtime binding must be rehydrated "
        "automatically so the map-points route serves again — today every "
        "restart refuses 409 map_points_binding_missing ('no plugin owns the "
        "map_points projection role for this project') until someone manually "
        "POSTs backend/activate with executableHandlersAllowed:true: got "
        f"{rehydrated.status_code}: {rehydrated.text}"
    )


def test_disabled_plugin_does_not_rehydrate_on_plain_restart(
    tmp_path: Path, geo_bundled_root: Path
) -> None:
    """GUARD (both worlds): rehydration is for ENABLED plugins only. A plugin
    the operator disabled before the restart must NOT come back — the
    map-points route keeps refusing 409 map_points_binding_missing after the
    restart ('disabled geo plugin means no map')."""
    workspace = tmp_path / "workspace"
    client = _client(workspace)
    project_id = _create_project(client, "restart disabled stays dark")

    assert _geo_index_entry(client, project_id)["installState"] == "enabled"
    map_points_url = _seed_geo_sheet(client, project_id)
    assert client.get(map_points_url).status_code == 200

    disabled = client.post(
        f"/api/projects/{project_id}/workbench/plugins/{GEO_PLUGIN_ID}/disable"
    )
    assert disabled.status_code == 200, disabled.text

    restarted = _restarted_client(workspace)

    geo_after = _geo_index_entry(restarted, project_id)
    assert geo_after["installState"] == "disabled", (
        "a plain restart must not resurrect a plugin the operator disabled: "
        f"got installState={geo_after['installState']!r}"
    )
    refused = restarted.get(map_points_url)
    assert refused.status_code == 409, (
        "a DISABLED plugin's projection binding must NOT be rehydrated on "
        f"restart: got {refused.status_code}: {refused.text}"
    )
    assert refused.json()["detail"]["code"] == "map_points_binding_missing"


def test_rehydrating_index_reads_are_idempotent_no_receipt_churn(
    tmp_path: Path, geo_bundled_root: Path
) -> None:
    """GUARD (both worlds): the runtime-index read that triggers rehydration
    stays idempotent. Two consecutive index reads in the restarted app leave
    the plugin enabled with the SAME receiptId/packageSha256 — rehydration
    restores previously-consented state, it does not re-run plugin.load or
    mint receipts per read."""
    workspace = tmp_path / "workspace"
    client = _client(workspace)
    project_id = _create_project(client, "restart rehydration idempotent")
    assert _geo_index_entry(client, project_id)["installState"] == "enabled"

    restarted = _restarted_client(workspace)

    first = _geo_index_entry(restarted, project_id)
    second = _geo_index_entry(restarted, project_id)
    assert first["installState"] == "enabled"
    assert second["installState"] == "enabled"
    assert first.get("receiptId"), "an enabled bundled install carries a receipt"
    assert second["receiptId"] == first["receiptId"], (
        "consecutive runtime-index reads must not churn the plugin.load "
        f"receipt: {first['receiptId']!r} -> {second['receiptId']!r}"
    )
    assert second["packageSha256"] == first["packageSha256"], (
        "consecutive runtime-index reads over an unchanged package must keep "
        "the same recorded package identity: "
        f"{first['packageSha256']!r} -> {second['packageSha256']!r}"
    )
