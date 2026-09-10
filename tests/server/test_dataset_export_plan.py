"""The shared dataset-export plan resolves typed row batches.

The plan is the single source/columns/value core every dataset destination
consumes. These assert typed (non-stringified) row batches, canonical value
precedence, current_view query resolution, and the all_visible-only contract.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from frisket.server.exports.plan import (
    ExportColumns,
    build_sheet_export_plan,
    iter_export_row_batches,
)
from frisket.server.exports.rowset import ExportError
from frisket.server.app import create_app


def _project(tmp_path: Path):
    client = TestClient(create_app(tmp_path / "ws"))
    pid = client.post("/api/projects", json={"name": "Plan"}).json()["id"]
    return client.app.state.workspace.get(pid)


def _seed(project) -> tuple[int, dict[str, int], list[int]]:
    sheet_id = project.add_sheet("tasks")
    columns = {
        "title": project.add_column(sheet_id, "title"),
        "status": project.add_column(sheet_id, "status"),
        "count": project.add_column(sheet_id, "count", type="integer"),
        "published": project.add_column(sheet_id, "published", type="date"),
    }
    rows = project.add_rows(
        sheet_id,
        [
            {
                "title": "A start",
                "status": "todo",
                "count": 2,
                "published": "2026-01-01",
            },
            {
                "title": "B done",
                "status": "done",
                "count": 5,
                "published": "2026-02-01",
            },
            {
                "title": "C done",
                "status": "done",
                "count": 9,
                "published": "2026-03-01",
            },
        ],
        columns,
    )
    return sheet_id, columns, rows


def _query(sheet_id: int) -> dict[str, Any]:
    return {
        "schema_version": "frisket.query.v1",
        "kind": "sheet.filter",
        "scope": {"kind": "sheet", "sheet_id": sheet_id},
        "filter": {"status": {"eq": "done"}},
        "sort": [{"column": "published", "dir": "desc"}],
    }


def _collect(project, plan) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for batch in iter_export_row_batches(project, plan):
        assert len(batch.row_ids) == len(batch.rows)
        for row in batch.rows:
            assert len(row) == len(plan.columns)
            out.append(dict(zip(plan.column_names, row)))
    return out


def test_current_sheet_plan_yields_typed_values(tmp_path: Path) -> None:
    project = _project(tmp_path)
    sheet_id, _columns, _rows = _seed(project)

    plan = build_sheet_export_plan(project, sheet_id)
    assert plan.column_names == ["title", "status", "count", "published"]
    assert plan.row_ids is None  # all visible rows, streamed in position order

    rows = _collect(project, plan)
    assert [row["title"] for row in rows] == ["A start", "B done", "C done"]
    # typed, NOT stringified: an integer cell stays an int
    assert rows[0]["count"] == 2
    assert isinstance(rows[0]["count"], int)
    assert isinstance(rows[2]["count"], int)


def test_plan_preserves_manual_edit_precedence(tmp_path: Path) -> None:
    project = _project(tmp_path)
    sheet_id, columns, rows = _seed(project)

    project.apply_edits(
        [{"row_id": rows[0], "column_id": columns["title"], "value": "A edited"}],
        label="manual edit",
    )
    plan = build_sheet_export_plan(project, sheet_id)
    collected = _collect(project, plan)
    assert collected[0]["title"] == "A edited"  # manual edit > source cell


def test_current_view_plan_resolves_filtered_sorted_rows(tmp_path: Path) -> None:
    project = _project(tmp_path)
    sheet_id, _columns, rows = _seed(project)

    plan = build_sheet_export_plan(project, sheet_id, query=_query(sheet_id))
    assert plan.row_ids == [rows[2], rows[1]]  # done rows, published desc
    assert plan.query_hash is not None and plan.query_hash.startswith("sha256:")
    assert plan.query_total == 2

    collected = _collect(project, plan)
    assert [row["title"] for row in collected] == ["C done", "B done"]


def test_selected_columns_mode_is_rejected(tmp_path: Path) -> None:
    project = _project(tmp_path)
    sheet_id, columns, _rows = _seed(project)
    with pytest.raises(ExportError) as excinfo:
        build_sheet_export_plan(
            project,
            sheet_id,
            columns=ExportColumns(mode="selected", column_ids=(columns["title"],)),
        )
    assert excinfo.value.code == "unsupported_export_columns"


def test_invalid_sheet_ref_raises_export_error(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _seed(project)
    with pytest.raises(ExportError) as excinfo:
        build_sheet_export_plan(project, 9999)
    assert excinfo.value.code == "invalid_sheet_ref"
