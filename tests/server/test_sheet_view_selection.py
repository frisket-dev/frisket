"""Grid paging and row location use the full saved/draft View selection."""

import csv
import io
import json
from types import SimpleNamespace

import pytest

from frisket.engine.store import Project
from frisket.server.services.sheet_grid import SheetGridService, SheetGridRouteError


def test_view_selection_filters_before_paging_and_locates_in_top_n(tmp_path):
    project = Project.create(tmp_path / "selected.frisket")
    try:
        sheet = project.add_sheet("Rows")
        score = project.add_column(sheet, "score", type="integer")
        rows = project.add_rows(
            sheet, [{"score": i} for i in range(701)], {"score": score}
        )
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
            service.sheet_data(
                "p", sheet, selection=json.dumps(selection), view_id=view_id
            )
    finally:
        project.close()
