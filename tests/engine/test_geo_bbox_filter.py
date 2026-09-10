"""Canonical geo-bbox filter operator (map "filter to viewport").

`{geo_col: {bbox: {min_lon, min_lat, max_lon, max_lat}}}` filters a geo_point
column by bounding box through the shared sheet-filter evaluator
(`frisket.querysets`), so /data, saved views, and the map all honor it. geo_point
values are `{lat, lon}` JSON, so the operator extracts $.lon/$.lat.
"""

from __future__ import annotations

import json

import pytest

from frisket.querysets import SheetRowSetError, resolve_sheet_filter_rows
from frisket.engine.store import Project
from frisket.engine.store.runs import RunResultStore
from helpers import write_claimed_test_results


@pytest.fixture
def project(tmp_path):
    p = Project.create(tmp_path / "geo.frisket", name="geo")
    yield p
    p.close()


def _seed(project: Project):
    sheet = project.add_sheet("places")
    name_col = project.add_column(sheet, "name", type="text")
    geo_col = project.add_column(sheet, "loc", type="geo_point")
    rows = project.add_rows(
        sheet,
        [
            {"name": "NYC", "loc": {"lat": 40.7128, "lon": -74.006}},
            {"name": "LA", "loc": {"lat": 34.0522, "lon": -118.2437}},
            {"name": "London", "loc": {"lat": 51.5074, "lon": -0.1278}},
        ],
        {"name": name_col, "loc": geo_col},
    )
    return sheet, geo_col, name_col, rows


def _bbox_filter(geo_col_name, min_lon, min_lat, max_lon, max_lat):
    return json.dumps(
        {
            geo_col_name: {
                "bbox": {
                    "min_lon": min_lon,
                    "min_lat": min_lat,
                    "max_lon": max_lon,
                    "max_lat": max_lat,
                }
            }
        }
    )


def test_bbox_filter_selects_points_inside(project):
    sheet, _geo, _name, rows = _seed(project)
    # Continental-US box → NYC + LA, not London.
    rowset = resolve_sheet_filter_rows(
        project,
        sheet,
        filter_=_bbox_filter("loc", -125.0, 24.0, -66.0, 50.0),
    )
    assert set(rowset.row_ids) == {rows[0], rows[1]}
    assert rowset.total == 2


def test_bbox_filter_excludes_points_outside(project):
    sheet, _geo, _name, rows = _seed(project)
    # Tight box around London only.
    rowset = resolve_sheet_filter_rows(
        project,
        sheet,
        filter_=_bbox_filter("loc", -1.0, 51.0, 0.5, 52.0),
    )
    assert set(rowset.row_ids) == {rows[2]}


def test_bbox_filter_requires_geo_point_column(project):
    sheet, _geo, _name, _rows = _seed(project)
    with pytest.raises(SheetRowSetError, match="geo_point"):
        resolve_sheet_filter_rows(
            project, sheet, filter_=_bbox_filter("name", -1, -1, 1, 1)
        )


def test_bbox_filter_rejects_malformed_bbox(project):
    sheet, _geo, _name, _rows = _seed(project)
    bad = json.dumps({"loc": {"bbox": {"min_lon": "x", "min_lat": 0}}})
    with pytest.raises(SheetRowSetError):
        resolve_sheet_filter_rows(project, sheet, filter_=bad)


def test_bbox_filter_rejects_reversed_bounds(project):
    sheet, _geo, _name, _rows = _seed(project)
    with pytest.raises(SheetRowSetError, match="reversed"):
        resolve_sheet_filter_rows(
            project,
            sheet,
            filter_=_bbox_filter("loc", -66.0, 24.0, -125.0, 50.0),
        )


def test_bbox_filter_works_for_run_backed_geo_point(project):
    """Locks the SQL placeholder order for generation-backed columns.

    bbox repeats the live-value SQL fragment four times and must interleave each
    fragment's params with its comparison bound in SQLite placeholder order.
    """
    sheet = project.add_sheet("generated")
    name_col = project.add_column(sheet, "name", type="text")
    geo_col = project.add_column(sheet, "loc", type="geo_point", ai_generated=True)
    rows = project.add_rows(
        sheet,
        [{"name": "inside"}, {"name": "outside"}],
        {"name": name_col},
    )
    op = project.append_op("map", {"action_kind": "map.to_geo_point"})
    run = RunResultStore(project).start_run(
        op, sheet, "map.to_geo_point", total_rows=len(rows)
    )
    write_claimed_test_results(
        project,
        run,
        [
            {
                "row_id": rows[0],
                "column_id": geo_col,
                "value": {"lat": 40.7128, "lon": -74.006},
            },
            {
                "row_id": rows[1],
                "column_id": geo_col,
                "value": {"lat": 51.5074, "lon": -0.1278},
            },
        ],
    )
    RunResultStore(project).finish_run(run)

    rowset = resolve_sheet_filter_rows(
        project,
        sheet,
        filter_=_bbox_filter("loc", -75.0, 40.0, -73.0, 41.0),
    )
    assert rowset.row_ids == [rows[0]]
    assert rowset.total == 1
