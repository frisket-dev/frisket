from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from frisket.contracts.action import ActionResult
from frisket.actions.system import validate_root_action
from frisket.engine.executor.actions import run_action_spec
from frisket.authoring.plugin_registry import (
    _reset_default_registry_for_tests,
    default_registry,
    register_trusted_backend_handler,
    unregister_trusted_backend_handler,
)
from frisket.engine.store import Project
from workbench_runtime_test_helpers import activate_runtime_plugin_for_project


PLUGIN_ID = "frisket-runtime-demo"
IMPORTER_KIND = "frisket_runtime_demo_import"
HANDLER_KEY = f"{PLUGIN_ID}:importer"


def _runtime_import_action(
    *,
    importer_kind: str = IMPORTER_KIND,
    key: str = "runtime-import@sha256:v1",
) -> dict[str, Any]:
    return {
        "action_id": "import.runtime",
        "scope": {"kind": "project"},
        "sheet_name": "Runtime Import",
        "params": {
            "importer_kind": importer_kind,
            "source": {
                "kind": "runtime",
                "label": "runtime-demo.fixture",
                "fingerprint": "sha256:runtime-demo",
            },
            "handler_params": {"city": "New York"},
        },
        "idempotency_key": key,
    }


def test_trusted_importer_runtime_binding_validates_materializes_and_replays(
    tmp_path: Path,
) -> None:
    _reset_default_registry_for_tests()
    calls: list[dict[str, Any]] = []

    def trusted_importer_handler(payload: dict[str, Any]) -> dict[str, Any]:
        calls.append(payload)
        return {
            "schemaVersion": "frisket.runtime_importer_plan.v1",
            "columns": [
                {"name": "name", "type": "text"},
                {"name": "score", "type": "integer"},
            ],
            "rows": [
                {"name": payload["handlerParams"]["city"], "score": 9},
                {"name": "Boston", "score": 7},
            ],
            "warnings": ["runtime importer handled by trusted host binding"],
        }

    register_trusted_backend_handler(HANDLER_KEY, trusted_importer_handler)
    try:
        default_registry().register_runtime_binding(
            "importers",
            IMPORTER_KIND,
            handler_key=HANDLER_KEY,
            handler=trusted_importer_handler,
            plugin=PLUGIN_ID,
        )
        project = Project.create(tmp_path / "runtime-import.frisket", name="Runtime")
        activate_runtime_plugin_for_project(
            project,
            tmp_path,
            plugin_id=PLUGIN_ID,
            runtime_bindings={"importers": {IMPORTER_KIND: HANDLER_KEY}},
            project_id="runtime-import-project",
        )
        action = _runtime_import_action()

        validation = validate_root_action(action)
        assert validation.ok is True
        assert validation.action is not None
        assert validation.action.action_id == "import.runtime"

        result = run_action_spec(project, action, project_id="runtime-import-project")

        assert isinstance(result, ActionResult)
        assert result.status == "completed"
        assert result.action.kind == "import.runtime"
        assert result.receipt_id is not None
        assert len(calls) == 1
        assert calls[0]["schemaVersion"] == "frisket.runtime_importer_request.v1"
        assert calls[0]["projectId"] == "runtime-import-project"
        assert calls[0]["pluginId"] == PLUGIN_ID
        assert calls[0]["handlerKey"] == HANDLER_KEY
        assert calls[0]["importerKind"] == IMPORTER_KIND
        assert calls[0]["sheetName"] == "Runtime Import"
        assert calls[0]["source"]["fingerprint"] == "sha256:runtime-demo"
        assert calls[0]["handlerParams"] == {"city": "New York"}

        sheet = project.db.execute(
            "SELECT id, name FROM sheets WHERE name='Runtime Import'"
        ).fetchone()
        assert sheet is not None
        values = project.db.execute(
            """
            SELECT c.name AS column_name, cells.value
            FROM cells
            JOIN columns c ON c.id = cells.column_id
            JOIN rows r ON r.id = cells.row_id
            WHERE r.sheet_id=?
            ORDER BY r.position, c.position
            """,
            (sheet["id"],),
        ).fetchall()
        assert [(row["column_name"], json.loads(row["value"])) for row in values] == [
            ("name", "New York"),
            ("score", 9),
            ("name", "Boston"),
            ("score", 7),
        ]

        row = project.db.execute(
            "SELECT action_kind, status, body FROM receipts WHERE id=?",
            (result.receipt_id,),
        ).fetchone()
        assert row is not None
        assert row["action_kind"] == "import.runtime"
        assert row["status"] == "completed"
        receipt = json.loads(row["body"])
        evidence_refs = [item["ref"] for item in receipt["inputs"]]
        runtime_ref = next(
            ref for ref in evidence_refs if ref["kind"] == "workbench_runtime_binding"
        )
        assert {
            key: runtime_ref[key]
            for key in (
                "kind",
                "schema_version",
                "binding_type",
                "binding_kind",
                "plugin",
                "handler_key",
            )
        } == {
            "kind": "workbench_runtime_binding",
            "schema_version": "frisket.workbench_runtime_binding_evidence.v1",
            "binding_type": "importers",
            "binding_kind": IMPORTER_KIND,
            "plugin": PLUGIN_ID,
            "handler_key": HANDLER_KEY,
        }

        replay = run_action_spec(project, action, project_id="runtime-import-project")
        assert replay.status == "completed"
        assert replay.receipt_id == result.receipt_id
        assert len(calls) == 1
    finally:
        unregister_trusted_backend_handler(HANDLER_KEY)
        _reset_default_registry_for_tests()


def test_unbound_runtime_importer_kind_stays_unsupported(tmp_path: Path) -> None:
    _reset_default_registry_for_tests()
    try:
        action = _runtime_import_action(importer_kind="frisket_runtime_demo_unbound")
        validation = validate_root_action(action)
        assert (
            validation.ok is True
        )  # Binding trust is checked with the target project.

        project = Project.create(tmp_path / "unbound-runtime-import.frisket")
        result = run_action_spec(project, action, project_id="runtime-import-project")
        assert result.status == "failed"
        assert result.errors[0].code == "unsupported_runtime_importer"
    finally:
        _reset_default_registry_for_tests()


def test_runtime_importer_invalid_plan_does_not_write_rows(tmp_path: Path) -> None:
    _reset_default_registry_for_tests()

    def invalid_handler(_payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "schemaVersion": "frisket.runtime_importer_plan.v1",
            "columns": [{"name": "name", "type": "text"}],
            "rows": [{"other": "bad"}],
        }

    register_trusted_backend_handler(HANDLER_KEY, invalid_handler)
    try:
        default_registry().register_runtime_binding(
            "importers",
            IMPORTER_KIND,
            handler_key=HANDLER_KEY,
            handler=invalid_handler,
            plugin=PLUGIN_ID,
        )
        project = Project.create(tmp_path / "bad-runtime-import.frisket")
        activate_runtime_plugin_for_project(
            project,
            tmp_path,
            plugin_id=PLUGIN_ID,
            runtime_bindings={"importers": {IMPORTER_KIND: HANDLER_KEY}},
            project_id="runtime-import-project",
        )

        result = run_action_spec(
            project,
            _runtime_import_action(key="runtime-import-bad@sha256:v1"),
            project_id="runtime-import-project",
        )

        assert result.status == "failed"
        assert result.errors[0].code == "plugin_importer_row_invalid"
        assert project.db.execute("SELECT COUNT(*) FROM sheets").fetchone()[0] == 0
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM receipts WHERE action_kind='import.runtime'"
            ).fetchone()[0]
            == 0
        )
    finally:
        unregister_trusted_backend_handler(HANDLER_KEY)
        _reset_default_registry_for_tests()
