"""Grid paging and row location use the full saved/draft View selection."""

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
        with pytest.raises(SheetGridRouteError, match="not both"):
            service.sheet_data(
                "p", sheet, selection=json.dumps(selection), view_id=view_id
            )
    finally:
        project.close()
