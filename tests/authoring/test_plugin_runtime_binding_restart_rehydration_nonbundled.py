"""Non-bundled plugin runtime-binding rehydration after restart.

After a plain backend restart with
NO package drift, a NON-bundled (trusted-local, ``install_source.kind ==
"localPath"``) plugin's executable projection runtime binding is rehydrated
automatically too, so projection-backed surfaces serve without any manual
``/backend/activate`` poke — matching the guarantee
tests/test_plugin_runtime_binding_restart_rehydration.py already pins for
bundled installs.

Direct precedent: tests/test_plugin_runtime_binding_restart_rehydration.py
(``plugin-runtime-binding-restart-rehydration-v1``, evaluator-closed
2026-07-15). That task's fix,
``_rehydrate_enabled_bundled_plugin_registrations``
(src/frisket/authoring/workbench/plugin_runtime.py), re-runs the original bootstrap's
activation functions from persisted receipt evidence + ``permissions_accepted``
consent at the bundled bootstrap's skip branch -- but it gates hard on
``source.get("kind") != "bundled"`` (plugin_runtime.py:604) and returns early
for every other install-source kind. Bundled installs rehydrate their
executable bindings because the bundled bootstrap ALWAYS grants
``executable_handlers_allowed=True`` as a standing host-shipped grant; a
trusted-local install's ``executableHandlersAllowed`` grant is a PER-REQUEST
flag on the ``/backend/activate`` body
(``src/frisket/server/routes/workbench.py``) that is never persisted into
install state, so there is nothing for a non-bundled rehydration path to read
even if one existed today.

Fixing this requires (a)
persisting the ``executableHandlersAllowed`` grant in install state AT
BACKEND ACTIVATION TIME, and (b) extending rehydration beyond
``install_source.kind == "bundled"`` to read that persisted grant for
enabled, non-bundled installs -- restoring exactly what was previously
consented, never widening it.

DONE means (this file's first test): install a real trusted-local plugin
package (a copy of the shipped ``frisket.geo`` package, installed via
``install-local`` from OUTSIDE the bundled-plugins root so it is a genuine
``localPath`` source, never a ``bundled`` one), activate the manifest, then
``/backend/activate`` with ``executableHandlersAllowed: true``. Baseline
map-points 200. Simulate a plain restart (registry reset + fresh ``create_app``
over the same workspace, package bytes UNTOUCHED, NO activate/backend-activate
call in the restarted app) -- and the map-points route serves 200 again.

Guards that must hold in BOTH worlds (green at admission, must survive the
rehydration work):
- A trusted-local plugin whose ORIGINAL backend activation set
  ``executableHandlersAllowed: false`` never gets executable bindings, restart
  or not: rehydration restores a previously-consented grant, it must never
  widen one the operator never gave. This project's map-points route already
  refuses 409 before any restart (backend/activate with the flag false
  registers no executable runtime bindings --
  ``activate_workbench_plugin_backend_contributions``,
  plugin_runtime.py:868-895) and keeps refusing 409 after a restart. The
  assertion is unconditional 409 in both the pre-restart and post-restart
  client, deliberately -- it is a real (if today-trivial) guard against a
  rehydration implementation that reads "enabled + trusted_local" as license
  to blanket-grant executable handlers rather than reading a persisted
  per-install grant.
- A trusted-local plugin the operator DISABLED before restart does not
  rehydrate: after restart the map-points route refuses 409
  ``map_points_binding_missing`` ("disabled geo plugin means no map", spec
  disabled-plugin contract), mirroring the bundled check's disabled guard.

Fixture idiom: ``tests/conftest.py``'s suite-wide ``hermetic_bundled_plugins_root``
autouse fixture already pins the bundled-plugins root to an empty tmp dir for
this whole suite (no ``real_bundled_plugins`` marker here, and this file never
touches ``plugin_runtime._bundled_plugins_root``) -- so there is no bundled
``frisket.geo`` competing for the ``frisket.geo`` plugin-id slot, and every
install in this file is unambiguously ``install_source.kind == "localPath"``.
The plugin PACKAGE itself is a copy of the real shipped
``src/frisket/authoring/bundled_plugins/frisket.geo`` (its ``map_points`` projection and
``plugin:trusted_local_backend`` capability requirement give the identical
observable route the bundled check uses), copied into a ``tmp_path`` directory
that is never inside the bundled root -- this file never mutates the package,
so there is no drift, exactly like the bundled precedent.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from frisket.authoring.plugin_registry import _reset_default_registry_for_tests
from frisket.server.app import create_app


ROOT = Path(__file__).resolve().parents[2]
REAL_GEO_PACKAGE_ROOT = (
    ROOT / "src" / "frisket" / "authoring" / "bundled_plugins" / "frisket.geo"
)
GEO_PLUGIN_ID = "frisket.geo"
TRUSTED_LOCAL_BACKEND_CAPABILITY = "plugin:trusted_local_backend"


@pytest.fixture(autouse=True)
def reset_registry() -> None:
    _reset_default_registry_for_tests()
    yield
    _reset_default_registry_for_tests()


@pytest.fixture()
def geo_package_copy(tmp_path: Path) -> Path:
    """A copy of the real shipped frisket.geo package living OUTSIDE the
    bundled-plugins root (which the suite-wide conftest fixture pins to an
    empty tmp dir anyway) so installing it is unambiguously a ``localPath``
    trusted-local install, never a ``bundled`` one. Nothing in this file
    mutates the copy -- zero package drift, same idiom as the bundled
    restart-rehydration check."""
    package_root = tmp_path / "external_plugins" / GEO_PLUGIN_ID
    shutil.copytree(
        REAL_GEO_PACKAGE_ROOT,
        package_root,
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    return package_root


def _client(workspace: Path) -> TestClient:
    return TestClient(create_app(workspace))


def _restarted_client(workspace: Path) -> TestClient:
    """Simulate a PLAIN app restart over the same on-disk workspace: the
    process registry is empty again and a fresh app (fresh WorkbenchService,
    so a fresh runtime-index bootstrap pass) serves the same projects. No
    package bytes changed -- this is every ordinary backend restart."""
    _reset_default_registry_for_tests()
    return _client(workspace)


def _create_project(client: TestClient, name: str) -> str:
    response = client.post("/api/projects", json={"name": name})
    assert response.status_code == 200, response.text
    return response.json()["id"]


def _geo_index_entry(client: TestClient, project_id: str) -> dict[str, Any]:
    index = client.get(f"/api/projects/{project_id}/workbench/plugins")
    assert index.status_code == 200, index.text
    plugins = {item["pluginId"]: item for item in index.json()["plugins"]}
    assert GEO_PLUGIN_ID in plugins, "install-local must register frisket.geo"
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


def _install_trusted_local_geo(
    client: TestClient, project_id: str, plugin_root: Path
) -> str:
    """Install (localPath) + activate the manifest. Stops short of
    /backend/activate so callers control the executableHandlersAllowed grant
    explicitly. Returns the receiptId."""
    installed = client.post(
        f"/api/projects/{project_id}/workbench/plugins/{GEO_PLUGIN_ID}/install-local",
        json={
            "source": {"kind": "localPath", "value": str(plugin_root)},
            "arbitraryPackageLoadAllowed": False,
        },
    )
    assert installed.status_code == 200, installed.text
    payload = installed.json()
    assert payload["source"]["kind"] == "localPath", (
        "this file installs a genuine trusted-local plugin, not a bundled "
        f"one: got install_source {payload['source']!r}"
    )
    receipt_id = str(payload["receiptId"])
    assert receipt_id

    activated = client.post(
        f"/api/projects/{project_id}/workbench/plugins/{GEO_PLUGIN_ID}/activate",
        json={
            "receiptId": receipt_id,
            "trustAcknowledged": True,
            "permissionsAccepted": [TRUSTED_LOCAL_BACKEND_CAPABILITY],
            "arbitraryPackageLoadAllowed": False,
        },
    )
    assert activated.status_code == 200, activated.text
    return receipt_id


def _backend_activate(
    client: TestClient, project_id: str, *, executable_handlers_allowed: bool
) -> None:
    backend = client.post(
        f"/api/projects/{project_id}/workbench/plugins/{GEO_PLUGIN_ID}/backend/activate",
        json={
            "trustAcknowledged": True,
            "arbitraryPackageLoadAllowed": False,
            "executableHandlersAllowed": executable_handlers_allowed,
        },
    )
    assert backend.status_code == 200, backend.text


def test_enabled_trusted_local_plugin_projection_binding_rehydrates_on_plain_restart(
    tmp_path: Path, geo_package_copy: Path
) -> None:
    """RED CORE: a NON-bundled (trusted-local) install's executable
    projection runtime binding must rehydrate on a plain no-drift restart,
    exactly like the bundled case. Baseline map-points 200 in the first app
    (after a real backend/activate with executableHandlersAllowed:true); then
    a restart with NOTHING changed on disk and NO activate/backend-activate
    call anywhere in the restarted app must serve map-points 200 again."""
    workspace = tmp_path / "workspace"
    client = _client(workspace)
    project_id = _create_project(client, "nonbundled restart rehydration")

    _install_trusted_local_geo(client, project_id, geo_package_copy)
    _backend_activate(client, project_id, executable_handlers_allowed=True)

    geo_before = _geo_index_entry(client, project_id)
    assert geo_before["installState"] == "enabled"
    assert geo_before["source"]["kind"] == "localPath"
    map_points_url = _seed_geo_sheet(client, project_id)
    baseline = client.get(map_points_url)
    assert baseline.status_code == 200, baseline.text

    # The incident: a plain restart. No package drift, no manual pokes.
    restarted = _restarted_client(workspace)

    geo_after = _geo_index_entry(restarted, project_id)
    assert geo_after["installState"] == "enabled", (
        "a plain restart must not change an enabled trusted-local plugin's "
        f"install state: got {geo_after['installState']!r}"
    )

    # ...and the projection-backed surface must serve WITHOUT a manual
    # backend/activate poke. Today this is the red: every restart 409s here
    # because _rehydrate_enabled_bundled_plugin_registrations gates hard on
    # install_source.kind == "bundled" (plugin_runtime.py:604) and the
    # per-request executableHandlersAllowed grant from the ORIGINAL
    # backend/activate call above was never persisted anywhere to rehydrate
    # from even if the gate were removed.
    rehydrated = restarted.get(map_points_url)
    assert rehydrated.status_code == 200, (
        "after a plain backend restart (NO package drift) an ENABLED "
        "non-bundled trusted-local plugin's executable projection runtime "
        "binding must be rehydrated automatically so the map-points route "
        "serves again -- today every restart refuses 409 "
        "map_points_binding_missing ('no plugin owns the map_points "
        "projection role for this project') until someone manually POSTs "
        "backend/activate with executableHandlersAllowed:true: got "
        f"{rehydrated.status_code}: {rehydrated.text}"
    )


def test_trusted_local_plugin_backend_activated_without_executable_handlers_stays_dark(
    tmp_path: Path, geo_package_copy: Path
) -> None:
    """GUARD (both worlds, unconditional): a trusted-local plugin whose
    ORIGINAL backend activation set executableHandlersAllowed:false must
    never get executable bindings, restart or not -- rehydration restores a
    previously-consented grant, it must never widen one the operator never
    gave. This pins the negative space a rehydration fix must respect: it may
    NOT read "enabled + trusted_local" as license to blanket-grant executable
    handlers; it must read a persisted per-install grant that, here, was
    never given."""
    workspace = tmp_path / "workspace"
    client = _client(workspace)
    project_id = _create_project(client, "nonbundled restart no-grant guard")

    _install_trusted_local_geo(client, project_id, geo_package_copy)
    _backend_activate(client, project_id, executable_handlers_allowed=False)

    assert _geo_index_entry(client, project_id)["installState"] == "enabled"
    map_points_url = _seed_geo_sheet(client, project_id)

    before_restart = client.get(map_points_url)
    assert before_restart.status_code == 409, (
        "backend/activate with executableHandlersAllowed:false must not "
        f"register the map_points executable binding: got {before_restart.status_code}: "
        f"{before_restart.text}"
    )
    assert before_restart.json()["detail"]["code"] == "map_points_binding_missing"

    restarted = _restarted_client(workspace)

    geo_after = _geo_index_entry(restarted, project_id)
    assert geo_after["installState"] == "enabled"

    after_restart = restarted.get(map_points_url)
    assert after_restart.status_code == 409, (
        "a trusted-local plugin never granted executableHandlersAllowed must "
        "NOT come back with executable bindings after a restart -- "
        "rehydration must restore the recorded grant, never widen it: got "
        f"{after_restart.status_code}: {after_restart.text}"
    )
    assert after_restart.json()["detail"]["code"] == "map_points_binding_missing"


def test_disabled_trusted_local_plugin_does_not_rehydrate_on_plain_restart(
    tmp_path: Path, geo_package_copy: Path
) -> None:
    """GUARD (both worlds): rehydration is for ENABLED plugins only. A
    trusted-local plugin the operator disabled before the restart must NOT
    come back -- the map-points route keeps refusing 409
    map_points_binding_missing after the restart ('disabled geo plugin means
    no map'), mirroring the bundled check's disabled guard."""
    workspace = tmp_path / "workspace"
    client = _client(workspace)
    project_id = _create_project(client, "nonbundled restart disabled stays dark")

    _install_trusted_local_geo(client, project_id, geo_package_copy)
    _backend_activate(client, project_id, executable_handlers_allowed=True)

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
        "a DISABLED trusted-local plugin's projection binding must NOT be "
        f"rehydrated on restart: got {refused.status_code}: {refused.text}"
    )
    assert refused.json()["detail"]["code"] == "map_points_binding_missing"
