"""Checks for bundled-plugin auto-enable opt-in.

A bundled first-party plugin that many operators do not want (the forcing
example is FollowTheMoney) should be shippable DISABLED. Per-project
enable/disable already exists (the plugin-manager lifecycle); the required
capability is an opt-in seam so a bundled package can ship un-enabled by
default while every other bundled
plugin (frisket.geo / frisket.media) stays auto-enabled with no manifest edit.

This suite freezes the SEAM, not one implementation:

- the light manifest gains an honest ``auto_enable`` field (default True) so a
  manifest MAY declare ``auto_enable: false``;
- project bootstrap (plugin_runtime.bootstrap_project_bundled_plugins, which
  auto-enables every packaged bundled plugin for out-of-box parity) HONORS the
  opt-out: an ``auto_enable: false`` plugin is still installed+loaded (so it is
  discoverable and one lifecycle activation away) but rests un-enabled, its
  actions absent from the project catalog;
- the normal lifecycle still turns it on: activating the manifest + backend
  makes its action binding dispatchable, and disabling removes it again;
- frisket.geo does NOT opt out (frisket.ftm deliberately does, gated on the
  `entities` extra), so geo stays auto-enabled.

Before the seam exists every red is a semantic AssertionError (a defensive
``model_fields`` probe guards the manifest field; the bootstrap/binding asserts
compare install states), never a collection/import failure, and no forbidden
classifier token appears in output.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from frisket.contracts.plugin import PluginManifest
from frisket.authoring.plugin_registry import _reset_default_registry_for_tests
from frisket.server.app import create_app
from frisket.authoring.workbench import plugin_runtime, plugin_runtime_status


ROOT = Path(__file__).resolve().parents[2]
BUNDLED_ROOT = ROOT / "src" / "frisket" / "authoring" / "bundled_plugins"

TRUSTED_LOCAL_BACKEND_CAPABILITY = "plugin:trusted_local_backend"


# ---------------------------------------------------------------------------
# Fixture bundled packages (an opt-out one + a default one) in a tmp root.
# ---------------------------------------------------------------------------


_FIXTURE_SOURCE = """from __future__ import annotations

from pydantic import BaseModel

from frisket.actions.core import ActionCategory, action, map_rows
from frisket.actions.types import ActionParams, ColumnRef, Row, RowResult
from frisket.plugins.sdk import Plugin


class Params(ActionParams):
    name: ColumnRef[str]


class Output(BaseModel):
    bundled_stamp: str | None


def stamp(params: Params, row: Row) -> RowResult[Output]:
    del row
    return RowResult(output=Output(bundled_stamp="bundled"))


STAMP = action(
    name="stamp",
    title="Stamp opt-in fixture",
    description=(
        "Writes a constant marker column to prove the opt-in bundled plugin "
        "routes through the real native runtime once enabled."
    ),
    category=ActionCategory.TEXT,
    run=map_rows(stamp),
)

plugin = Plugin(
    id={plugin_id!r},
    version="0.1.0",
    capabilities=[{capability!r}],
    auto_enable={auto_enable!r},
    actions=(STAMP,),
)
"""


def _write_fixture_package(
    root: Path, *, plugin_id: str, auto_enable: bool | None
) -> Path:
    """Write a real bundled fixture package whose plugin.json is GENERATED
    from the declaration, exactly as `frisket plugin build` would emit it, so
    the fixture cannot drift from what the loader and the native runtime
    accept."""
    from frisket.plugins.manifest_generate import (
        load_generation_source,
        manifest_dict_from_plugin,
        render_manifest_json,
    )

    package_root = root / plugin_id
    package_root.mkdir(parents=True, exist_ok=True)
    (package_root / "plugin.py").write_text(
        _FIXTURE_SOURCE.format(
            plugin_id=plugin_id,
            capability=TRUSTED_LOCAL_BACKEND_CAPABILITY,
            auto_enable=True if auto_enable is None else auto_enable,
        ),
        encoding="utf-8",
    )
    manifest = manifest_dict_from_plugin(load_generation_source(package_root))
    if auto_enable is None:
        # ``auto_enable is None`` is the "field absent entirely" fixture: an
        # existing bundled manifest that predates the opt-in seam must still
        # bootstrap auto-enabled with no edit.
        manifest.pop("auto_enable")
    (package_root / "plugin.json").write_text(
        render_manifest_json(manifest),
        encoding="utf-8",
    )
    return package_root


OPTOUT_ID = "demo.optout_fixture"
# Registration namespaces every Action id under the plugin id.
OPTOUT_KIND = f"{OPTOUT_ID}.stamp"
OPTIN_ID = "demo.optin_fixture"
OPTIN_KIND = f"{OPTIN_ID}.stamp"


@pytest.fixture()
def tmp_bundled_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "bundled_root"
    root.mkdir()
    _write_fixture_package(root, plugin_id=OPTOUT_ID, auto_enable=False)
    _write_fixture_package(root, plugin_id=OPTIN_ID, auto_enable=None)
    # Override the suite-wide hermetic empty root (conftest autouse fixture).
    monkeypatch.setattr(plugin_runtime, "_bundled_plugins_root", lambda: root)
    monkeypatch.setattr(plugin_runtime_status, "_bundled_plugins_root", lambda: root)
    _reset_default_registry_for_tests()
    yield root
    _reset_default_registry_for_tests()


def _client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(tmp_path / "workspace"))


def _project(client: TestClient, name: str):
    response = client.post("/api/projects", json={"name": name})
    assert response.status_code == 200, response.text
    project_id = response.json()["id"]
    return project_id, client.app.state.workspace.get(project_id)


# ---------------------------------------------------------------------------
# 1. The light manifest carries an honest auto_enable field (default True).
# ---------------------------------------------------------------------------


def test_manifest_declares_auto_enable_default_true():
    assert "auto_enable" in PluginManifest.model_fields, (
        "the light plugin manifest must gain an ``auto_enable`` field so a "
        "bundled package can ship DISABLED (bundled-plugin-auto-enable-optin-v1)"
    )
    base = {
        "schema_version": "frisket.plugin.v1",
        "id": "demo.manifest_probe",
        "version": "0.1.0",
        "contributes": {
            "actions": ["demo.manifest_probe.op.stamp"],
            "importers": [],
            "operators": [],
            "projections": [],
            "column_types": [],
            "job_handlers": [],
        },
    }
    # Absent -> defaults to True (every existing bundled manifest stays
    # auto-enabled with no edit).
    assert PluginManifest.model_validate(base).auto_enable is True
    # Explicit opt-out parses honestly.
    opted_out = PluginManifest.model_validate({**base, "auto_enable": False})
    assert opted_out.auto_enable is False


# ---------------------------------------------------------------------------
# 2. Bootstrap HONORS the opt-out; a default bundled plugin is still enabled.
# ---------------------------------------------------------------------------


def test_bootstrap_leaves_opt_out_plugin_un_enabled(
    tmp_path: Path, tmp_bundled_root: Path
):
    client = _client(tmp_path)
    project_id, _ = _project(client, "auto-enable opt-out bootstrap")

    index = client.get(f"/api/projects/{project_id}/workbench/plugins")
    assert index.status_code == 200, index.text
    plugins = {item["pluginId"]: item for item in index.json()["plugins"]}

    assert OPTIN_ID in plugins and OPTOUT_ID in plugins, (
        "bootstrap must still SEED both bundled packages so the opt-out one is "
        "discoverable and one activation away"
    )
    # The default (auto_enable absent) plugin is auto-enabled for out-of-box
    # parity — unchanged behavior.
    assert plugins[OPTIN_ID]["installState"] == "enabled", (
        "a bundled plugin without auto_enable:false must stay auto-enabled"
    )
    # The opt-out plugin ships installed-but-not-enabled: honored, not ignored.
    assert plugins[OPTOUT_ID]["installState"] == "installed", (
        "auto_enable:false must leave the plugin un-enabled at bootstrap "
        f"(got {plugins[OPTOUT_ID]['installState']!r})"
    )
    assert plugins[OPTOUT_ID].get("registryActivated") is not True, (
        "an opt-out bundled plugin must not be registry-activated at bootstrap"
    )


def test_opt_out_actions_absent_until_enabled_then_toggle_with_lifecycle(
    tmp_path: Path, tmp_bundled_root: Path
):
    from frisket.authoring.workbench.plugin_runtime import (
        activate_workbench_plugin_backend_contributions,
        activate_workbench_plugin_manifest,
        disable_workbench_plugin,
    )
    from frisket.authoring.workbench.plugin_runtime_capabilities import (
        project_runtime_binding,
    )

    client = _client(tmp_path)
    project_id, project = _project(client, "auto-enable opt-out lifecycle")

    # Opt-out plugin: installed, so its action is NOT dispatchable in the
    # project catalog; the default plugin's action IS.
    assert (
        project_runtime_binding(project, binding_type="actions", kind=OPTOUT_KIND)
        is None
    ), "an un-enabled opt-out plugin must not expose its actions to the project"
    assert (
        project_runtime_binding(project, binding_type="actions", kind=OPTIN_KIND)
        is not None
    ), "the auto-enabled default plugin's action must be dispatchable"

    # Enable the opt-out plugin through the normal lifecycle (activate manifest +
    # backend) — this is the existing per-project enable path. Its enablement
    # row is the project's; its package identity (the receipt id activation
    # binds to) is the workspace catalog's.
    state = plugin_runtime._workbench_plugin_install_state(project, plugin_id=OPTOUT_ID)
    assert state is not None
    catalog_entry = client.app.state.workspace.plugin_package_catalog.get(OPTOUT_ID)
    assert catalog_entry is not None
    receipt_id = str(catalog_entry.get("receipt_id") or "")
    assert receipt_id, "an installed opt-out plugin must retain its package identity"
    activate_workbench_plugin_manifest(
        project,
        project_id=project_id,
        plugin_id=OPTOUT_ID,
        receipt_id=receipt_id,
        trust_acknowledged=True,
        permissions_accepted=[TRUSTED_LOCAL_BACKEND_CAPABILITY],
        arbitrary_package_load_allowed=False,
    )
    activate_workbench_plugin_backend_contributions(
        project,
        project_id=project_id,
        plugin_id=OPTOUT_ID,
        trust_acknowledged=True,
        arbitrary_package_load_allowed=False,
        executable_handlers_allowed=True,
    )
    assert (
        project_runtime_binding(project, binding_type="actions", kind=OPTOUT_KIND)
        is not None
    ), "enabling via the lifecycle must make the opt-out plugin's actions appear"

    # Disabling removes them again.
    disable_workbench_plugin(project, project_id=project_id, plugin_id=OPTOUT_ID)
    assert (
        project_runtime_binding(project, binding_type="actions", kind=OPTOUT_KIND)
        is None
    ), "disabling must remove the plugin's actions from the project catalog"


# ---------------------------------------------------------------------------
# 3. The shipped bundled manifests do NOT opt out (geo stays auto-enabled).
# ---------------------------------------------------------------------------


# frisket.geo is the auto-enabled pin. frisket.ftm ships dormant because its
# surfaces need the `entities` extra; the opt-out seam exists for that shape.
@pytest.mark.parametrize("plugin_id", ["frisket.geo"])
def test_shipped_bundled_plugins_do_not_opt_out(plugin_id: str):
    manifest_path = BUNDLED_ROOT / plugin_id / "plugin.json"
    assert manifest_path.is_file(), f"missing shipped manifest for {plugin_id}"
    # rule19: executes: manifest round-trips through the real PluginManifest.model_validate
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    # Either absent (defaults True) or explicitly True — never opted out.
    assert raw.get("auto_enable", True) is True, (
        f"{plugin_id} must stay auto-enabled (auto_enable must not be false); the "
        "opt-in seam only changes plugins that explicitly declare auto_enable:false"
    )
    # Durable guardrail (green before and after the seam): the shipped manifest
    # parses and, once the field exists, resolves to auto-enabled.
    manifest = PluginManifest.model_validate(raw)
    assert getattr(manifest, "auto_enable", True) is True
