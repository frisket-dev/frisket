"""Frontend module bindings are namespaced under the owning plugin
(plugin-binding-namespace-hardening-v1): a binding serves the plugin's own
code, so a foreign contribution_id is rejected at manifest load (the single
gate behind validate, install-local, and serve). Manifest `contributes`
lists remain free to reference core surface ids — the cross-surface pattern
frisket_geo_smoke uses (pinned by test_workbench_plugin_contract_fixtures).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from frisket.contracts.plugin import PluginManifest

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "local_plugins"


def _manifest(fixture: str) -> dict:
    return json.loads((FIXTURES / fixture / "plugin.json").read_text(encoding="utf-8"))


def test_foreign_binding_contribution_id_is_rejected() -> None:
    manifest = _manifest("demo_peek_panel")
    manifest["runtime"]["workbench_components"][0]["contribution_id"] = (
        "frisket.core.view.evidence"
    )
    with pytest.raises(ValueError, match="must be namespaced under the plugin id"):
        PluginManifest.model_validate(manifest)


def test_cross_surface_contributes_declaration_stays_legal() -> None:
    # The geo plugin declares a CORE panel id in contributes (not a binding):
    # legal, and its own bindings remain namespaced.
    loaded = PluginManifest.model_validate(_manifest("frisket_geo_smoke"))
    assert "frisket.core.panel.projection_status" in (
        loaded.contributes.workbench_panels
    )
    for binding in loaded.runtime.workbench_components:
        assert binding.contribution_id.startswith("frisket.geosmoke.")
