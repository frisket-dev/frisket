"""Grid paging and row location use the full saved/draft View selection."""

import csv
import io
import json
from types import SimpleNamespace

import pytest

from frisket.server.services.sheet_grid import SheetGridService, SheetGridRouteError


def test_view_selection_filters_before_paging_and_locates_in_top_n(make_project):
    project = make_project("selected")
    sheet = project.add_sheet("Rows")
    score = project.add_column(sheet, "score", type="integer")
    rows = project.add_rows(sheet, [{"score": i} for i in range(701)], {"score": score})
    selection = {
        "scope": {"kind": "sheet", "sheet_id": sheet},
        "filters": [{"column_id": score, "condition": {"gte": "100"}}],
        "order": [{"column_id": score, "dir": "desc"}],
        "limit": 600,
    }
    service = SheetGridService(SimpleNamespace(get=lambda _: project))
    result = service.sheet_data(
        "p", sheet, offset=500, limit=2, selection=json.dumps(selection)
    )
    assert result["total"] == 600
    assert [row["id"] for row in result["rows"]] == [rows[200], rows[199]]
    located = service.locate_sheet_row(
        "p", sheet, rows[199], selection=json.dumps(selection)
    )
    assert located["index"] == 501
    outside = service.locate_sheet_row(
        "p", sheet, rows[100], selection=json.dumps(selection)
    )
    assert outside["found"] is False
    view_id = project.add_view(
        "Top rows", {"selection": selection, "presentation": {}}, sheet_id=sheet
    )
    saved = service.sheet_data("p", sheet, offset=500, limit=2, view_id=view_id)
    assert saved == result
    stats = service.column_stats("p", sheet, score, selection=json.dumps(selection))
    assert stats["row_count"] == 600
    assert stats["numeric"]["min"] == 101
    assert stats["numeric"]["max"] == 700
    from frisket.server.services.sheet_export import SheetDatasetExportService
    from openpyxl import load_workbook

    exporter = SheetDatasetExportService(SimpleNamespace(get=lambda _: project))
    stream = exporter.stream_csv(
        "p",
        [sheet],
        filter_=None,
        sort=None,
        formula_policy="escape",
        selection=json.dumps(selection),
    )
    try:
        exported = list(
            csv.reader(io.StringIO(b"".join(stream.chunks).decode("utf-8-sig")))
        )
    finally:
        stream.close()
    assert len(exported) == 601
    assert exported[1] == ["700"]
    assert exported[-1] == ["101"]
    workbook = exporter.export(
        "p",
        [sheet],
        format_="xlsx",
        filter_=None,
        sort=None,
        formula_policy="escape",
        view_id=view_id,
    )
    excel = load_workbook(io.BytesIO(workbook.content), read_only=True)
    assert list(excel.active.values) == [("score",)] + [
        (i,) for i in range(700, 100, -1)
    ]
    excel.close()
    from frisket.actions.google_sheets_types import GoogleSheetsExportRequest
    from frisket.engine.executor.action_families.exports import (
        _google_sheets_tabs_for_source,
    )

    google = GoogleSheetsExportRequest(
        connection_id="test-connection",
        source={"kind": "current_view", "sheet_id": sheet, "selection": selection},
        destination={"kind": "google_sheets", "mode": "new_spreadsheet"},
    )
    tabs = _google_sheets_tabs_for_source(
        project, SimpleNamespace(kind="export.google_sheets"), google
    )
    assert isinstance(tabs, list), tabs
    assert tabs[0]["row_count"] == 600
    assert tabs[0]["row_ids"] == list(reversed(rows[101:]))
    assert tabs[0]["rows"][0] == [700]
    with pytest.raises(SheetGridRouteError, match="not both"):
        service.sheet_data("p", sheet, selection=json.dumps(selection), view_id=view_id)


def test_large_explicit_view_scope_uses_bounded_sql_parameters(make_project):
    import sqlite3
    from frisket.engine.views.evaluate import open_selection

    project = make_project("large-scope")
    sheet = project.add_sheet("Rows")
    col = project.add_column(sheet, "value", "integer")
    rows = project.add_rows(sheet, [{"value": i} for i in range(1501)], {"value": col})
    definition = {
        "selection": {
            "scope": {"kind": "sheet", "sheet_id": sheet, "row_ids": rows[1:]},
            "filters": [],
            "order": [],
            "limit": None,
        },
        "presentation": {},
    }
    # Lower SQLite's bind budget so the test proves shape, not the host's
    # unusually generous build-time limit. Large selections stay supported.
    with project.read_snapshot() as snapshot:
        snapshot.db.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 50)
        with open_selection(snapshot, definition["selection"]) as selection:
            assert selection.total == 1500
            assert [
                hit["row_id"] for hit in selection.hits(offset=1498, limit=2)
            ] == rows[-2:]
            sql, params = selection.ordered_rows_sql()
            assert len(params) < 10
            assert len(snapshot.db.execute(sql, params).fetchall()) == 1500


def test_view_grid_reads_generated_cells_and_reuses_owned_snapshot(make_project):
    from frisket.engine.executor import run_action_spec

    project = make_project("generated-view")
    sheet = project.add_sheet("Stories")
    source = project.add_column(sheet, "source")
    rows = project.add_rows(sheet, [{"source": "one"}], {"source": source})
    result = run_action_spec(
        project,
        {
            "action_id": "map.template",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet},
            "params": {"template": {"text": "{{source}}"}},
            "output_names": {"rendered": "note"},
            "idempotency_key": "view-grid-generated",
        },
        project_id="p",
    )
    assert result.status == "completed", result.errors
    note = next(c["id"] for c in project.columns(sheet) if c["name"] == "note")
    project.apply_edits([{"row_id": rows[0], "column_id": note, "value": "corrected"}])
    selection = {
        "scope": {"kind": "sheet", "sheet_id": sheet},
        "filters": [],
        "order": [],
        "limit": None,
    }
    service = SheetGridService(SimpleNamespace(get=lambda _: project))
    grid = service.sheet_data("p", sheet, selection=json.dumps(selection))
    assert len(grid["rows"]) == 1
    from frisket.engine.store.cells import bounded_pending_replay_values

    with project.read_snapshot() as snapshot:
        with snapshot.read_snapshot() as borrowed:
            assert borrowed is snapshot
            bounded_pending_replay_values(
                borrowed, sheet, note, rows, include_count=True, byte_limit=1024
            )
        # Leaving the nested helper must not close the surrounding View read.
        assert snapshot.get_values(sheet, note, rows) == {rows[0]: "corrected"}


def test_view_export_uses_one_ordered_cursor_across_batches(make_project):
    from frisket.engine.views.evaluate import open_selection
    from frisket.server.exports.plan import (
        build_sheet_export_plan,
        iter_export_row_batches,
    )

    project = make_project("export-cursor")
    sheet = project.add_sheet("Rows")
    score = project.add_column(sheet, "score", type="integer")
    project.add_rows(sheet, [{"score": i} for i in range(60)], {"score": score})
    with open_selection(
        project,
        {
            "scope": {"kind": "sheet", "sheet_id": sheet},
            "order": [{"column_id": score, "dir": "desc"}],
            "limit": 51,
        },
    ) as selected:
        plan = build_sheet_export_plan(
            selected.snapshot, sheet, selection_read=selected
        )
        statements = []
        selected.snapshot.db.set_trace_callback(statements.append)
        try:
            batches = list(
                iter_export_row_batches(selected.snapshot, plan, batch_size=7)
            )
        finally:
            selected.snapshot.db.set_trace_callback(None)
    assert [row[0] for batch in batches for row in batch.rows] == list(range(59, 8, -1))
    selection_reads = [
        s for s in statements if s.startswith("SELECT * FROM ") and "ORDER BY" in s
    ]
    assert len(selection_reads) == 1
    assert "OFFSET" not in selection_reads[0]
