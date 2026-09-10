from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from frisket.authoring.plugin_registry import (
    _reset_default_registry_for_tests,
    default_registry,
    register_trusted_backend_handler,
    unregister_trusted_backend_handler,
)
from frisket.querysets import SheetRowSetError, resolve_sheet_filter_rows
from frisket.server.app import create_app
from frisket.engine.store import Project
from workbench_runtime_test_helpers import activate_runtime_plugin_for_project


PLUGIN_ID = "frisket-runtime-demo"
OPERATOR_KIND = "frisket.runtime_demo.operator"
HANDLER_KEY = f"{PLUGIN_ID}:operator"


def _filter(kind: str = OPERATOR_KIND, value: Any | None = None) -> str:
    return json.dumps({"status": {kind: value if value is not None else "done"}})


def _seed_project(project: Project) -> tuple[int, dict[str, int], dict[str, int]]:
    sheet_id = project.add_sheet("tasks")
    columns = {
        "title": project.add_column(sheet_id, "title"),
        "status": project.add_column(sheet_id, "status"),
    }
    row_ids = project.add_rows(
        sheet_id,
        [
            {"title": "A start", "status": "todo"},
            {"title": "B done", "status": "done"},
            {"title": "C done", "status": "done"},
        ],
        columns,
    )
    return (
        sheet_id,
        {"a": row_ids[0], "b": row_ids[1], "c": row_ids[2]},
        columns,
    )


def test_trusted_operator_runtime_binding_filters_rowset_and_grid(
    tmp_path: Path,
) -> None:
    _reset_default_registry_for_tests()
    calls: list[dict[str, Any]] = []

    def trusted_operator_handler(payload: dict[str, Any]) -> dict[str, Any]:
        calls.append(payload)
        project = payload["project"]
        values = project.get_values(
            payload["sheetId"],
            payload["column"]["id"],
            row_ids=payload["candidateRowIds"],
        )
        matched = [
            row_id
            for row_id, value in values.items()
            if str(value).lower() == str(payload["value"]).lower()
        ]
        return {
            "schemaVersion": "frisket.runtime_operator_plan.v1",
            "rowIds": matched,
        }

    register_trusted_backend_handler(HANDLER_KEY, trusted_operator_handler)
    try:
        default_registry().register_runtime_binding(
            "operators",
            OPERATOR_KIND,
            handler_key=HANDLER_KEY,
            handler=trusted_operator_handler,
            plugin=PLUGIN_ID,
        )
        client = TestClient(create_app(tmp_path / "ws"))
        pid = client.post("/api/projects", json={"name": "Runtime"}).json()["id"]
        project = client.app.state.workspace.get(pid)
        activate_runtime_plugin_for_project(
            project,
            tmp_path,
            plugin_id=PLUGIN_ID,
            runtime_bindings={"operators": {OPERATOR_KIND: HANDLER_KEY}},
            project_id=pid,
        )
        sheet_id, rows, columns = _seed_project(project)

        rowset = resolve_sheet_filter_rows(
            project,
            sheet_id,
            filter_=_filter(),
            limit=50,
        )

        assert rowset.row_ids == [rows["b"], rows["c"]]
        assert rowset.total == 2
        assert len(calls) == 1
        assert calls[0]["schemaVersion"] == "frisket.runtime_operator_request.v1"
        assert calls[0]["pluginId"] == PLUGIN_ID
        assert calls[0]["handlerKey"] == HANDLER_KEY
        assert calls[0]["operatorKind"] == OPERATOR_KIND
        assert calls[0]["sheetId"] == sheet_id
        assert calls[0]["column"]["id"] == columns["status"]
        assert calls[0]["column"]["name"] == "status"
        assert calls[0]["column"]["type"] == "text"
        assert calls[0]["value"] == "done"
        assert calls[0]["candidateRowIds"] == [rows["a"], rows["b"], rows["c"]]

        grid = client.get(
            f"/api/projects/{pid}/sheets/{sheet_id}/data",
            params={"filter": _filter()},
        )

        assert grid.status_code == 200, grid.text
        assert [row["id"] for row in grid.json()["rows"]] == rowset.row_ids
    finally:
        unregister_trusted_backend_handler(HANDLER_KEY)
        _reset_default_registry_for_tests()


def test_unbound_runtime_operator_stays_unsupported(tmp_path: Path) -> None:
    _reset_default_registry_for_tests()
    try:
        project = Project.create(tmp_path / "unbound-operator.frisket", name="Runtime")
        sheet_id, _rows, _columns = _seed_project(project)

        with pytest.raises(SheetRowSetError, match="unsupported filter"):
            resolve_sheet_filter_rows(
                project,
                sheet_id,
                filter_=_filter(kind="frisket.runtime_demo.unbound"),
            )
    finally:
        _reset_default_registry_for_tests()


def test_runtime_operator_handler_failures_fail_closed(tmp_path: Path) -> None:
    _reset_default_registry_for_tests()

    def invalid_handler(_payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "schemaVersion": "frisket.runtime_operator_plan.v1",
            "whereSql": "1=1",
        }

    register_trusted_backend_handler(HANDLER_KEY, invalid_handler)
    try:
        default_registry().register_runtime_binding(
            "operators",
            OPERATOR_KIND,
            handler_key=HANDLER_KEY,
            handler=invalid_handler,
            plugin=PLUGIN_ID,
        )
        project = Project.create(tmp_path / "bad-operator.frisket", name="Runtime")
        activate_runtime_plugin_for_project(
            project,
            tmp_path,
            plugin_id=PLUGIN_ID,
            runtime_bindings={"operators": {OPERATOR_KIND: HANDLER_KEY}},
            project_id="bad-runtime-operator",
        )
        sheet_id, _rows, _columns = _seed_project(project)

        with pytest.raises(SheetRowSetError, match="invalid runtime operator plan"):
            resolve_sheet_filter_rows(project, sheet_id, filter_=_filter())
    finally:
        unregister_trusted_backend_handler(HANDLER_KEY)
        _reset_default_registry_for_tests()
