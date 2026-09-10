"""Real installed FtM Actions use the ordinary native callable/domain host."""

import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from frisket.actions.entity_package_types import ImportedEntityDataset
from frisket.authoring.plugin_registry import _reset_default_registry_for_tests
from frisket.authoring.workbench import plugin_runtime, plugin_runtime_status
from frisket.authoring.workbench.installed_actions import resolve_installed_action
from frisket.engine.executor.actions import run_action_spec
from frisket.server.app import create_app


def test_installed_ftm_import_export_and_replay(tmp_path, monkeypatch):
    pytest.importorskip("followthemoney")
    from frisket.features.followthemoney.import_planner import (
        plan_followthemoney_import,
    )
    from frisket.features.followthemoney.migration_harness import (
        _canonical_export_inputs,
    )

    repo = Path(__file__).resolve().parents[2]
    root = tmp_path / "bundled"
    package = root / "frisket.ftm"
    shutil.copytree(repo / "src/frisket/authoring/bundled_plugins/frisket.ftm", package)
    monkeypatch.setattr(plugin_runtime, "_bundled_plugins_root", lambda: root)
    monkeypatch.setattr(plugin_runtime_status, "_bundled_plugins_root", lambda: root)
    _reset_default_registry_for_tests()
    app = create_app(
        tmp_path / "workspace",
        enable_provider_config=False,
        edition="team",
        serve_spa=False,
    )
    try:
        with TestClient(app) as client:
            pid = app.state.workspace.create("FtM installed")["id"]
            project = app.state.workspace.get(pid)
            base = f"/api/projects/{pid}/workbench/plugins/frisket.ftm"
            installed = client.post(
                base + "/install-local",
                json={
                    "source": {
                        "kind": "localPath",
                        "value": str(package / "plugin.json"),
                    },
                    "arbitraryPackageLoadAllowed": False,
                },
            )
            assert installed.status_code == 200, installed.text
            manifest = json.loads((package / "plugin.json").read_text())
            enabled = client.post(
                base + "/activate",
                json={
                    "receiptId": installed.json()["receiptId"],
                    "trustAcknowledged": True,
                    "executableHandlersAllowed": True,
                    "permissionsAccepted": manifest["requires"]["capabilities"],
                    "arbitraryPackageLoadAllowed": False,
                },
            )
            assert enabled.status_code == 200, enabled.text
            backend = client.post(
                base + "/backend/activate",
                json={
                    "trustAcknowledged": True,
                    "arbitraryPackageLoadAllowed": False,
                    "executableHandlersAllowed": True,
                },
            )
            assert backend.status_code == 200, backend.text
            assert resolve_installed_action(project, "frisket.ftm.ftm_import")
            source = repo / "tests/goldens/ftm_bundled_plugin/case.ftm.json"
            request = {
                "action_id": "frisket.ftm.ftm_import",
                "scope": {"kind": "project"},
                "params": {
                    "source_path": str(source),
                    "dataset_name": "Installed case",
                },
                "idempotency_key": "import-once",
            }
            imported = run_action_spec(project, request, project_id=pid)
            assert imported.status == "completed", imported.errors
            assert (
                ImportedEntityDataset.model_validate(imported.value).dataset_name
                == "Installed case"
            )
            assert run_action_spec(project, request, project_id=pid) == imported
            plan = plan_followthemoney_import(
                source.read_bytes(), dataset_name="Installed case"
            )
            selected, mappings, _ = _canonical_export_inputs(project, plan)
            exported_request = {
                "action_id": "frisket.ftm.ftm_export",
                "scope": {"kind": "project"},
                "params": {
                    "rowsets": selected["rowsets"],
                    "mappings": mappings,
                    "filename": "installed.zip",
                    "validate_entities": True,
                },
                "idempotency_key": "export-once",
            }
            exported = run_action_spec(project, exported_request, project_id=pid)
            assert exported.status == "completed", exported.errors
            assert exported.value["filename"] == "installed.zip"
            assert (
                run_action_spec(project, exported_request, project_id=pid) == exported
            )
            assert exported.receipt_id != imported.receipt_id
            assert {output.kind for output in exported.outputs} == {"export"}
            assert exported.outputs[0].name == "installed.zip"
    finally:
        _reset_default_registry_for_tests()
