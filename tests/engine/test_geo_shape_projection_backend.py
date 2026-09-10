from __future__ import annotations

import zipfile

import pytest

# The sidecar computes shape bboxes with shapely; shapely is a
# declared core dep, but skip clearly if a host lacks it rather than pass silently.
pytest.importorskip("shapely")

from frisket.engine.projections.point_backend import (  # noqa: E402
    GEO_SIDECAR_FILENAME,
    GeoProjectionBackend,
    GeoProjectionError,
)
from frisket.engine.store import Project  # noqa: E402


def _polygon(lon0: float, lat0: float, size: float = 0.02) -> dict:
    return {
        "type": "Polygon",
        "coordinates": [
            [
                [lon0, lat0],
                [lon0 + size, lat0],
                [lon0 + size, lat0 + size],
                [lon0, lat0 + size],
                [lon0, lat0],
            ]
        ],
    }


@pytest.fixture
def project(tmp_path):
    p = Project.create(tmp_path / "geo.frisket", name="geo")
    yield p
    p.close()


def _seed(project: Project):
    """A sheet with a geo_shape column: 2 polygons + 1 point + 1 empty."""
    sheet = project.add_sheet("regions")
    name_col = project.add_column(sheet, "name", type="text")
    geo_col = project.add_column(sheet, "shape", type="geo_shape")
    cols = {"name": name_col, "shape": geo_col}
    rows = project.add_rows(
        sheet,
        [
            {"name": "NYC", "shape": _polygon(-74.01, 40.70)},  # near lon -74
            {"name": "Tokyo", "shape": _polygon(139.00, 35.60)},  # near lon 139
            {"name": "Paris", "shape": {"type": "Point", "coordinates": [2.35, 48.85]}},
            {"name": "Empty", "shape": None},
        ],
        cols,
    )
    return sheet, geo_col, name_col, rows


def test_query_shapes_bbox_prefilter_and_rebuild(project, tmp_path):
    sheet, geo_col, name_col, rows = _seed(project)
    backend = GeoProjectionBackend(project)

    status = backend.materialize_geo_shape_column(sheet, geo_col)
    assert status["status"] == "ready"
    assert status["valid_points"] == 3  # 2 polygons + 1 point
    assert status["invalid_points"] == 1  # the empty cell
    assert status["total_rows"] == 4

    # No bbox: every valid shape comes back as a (row_id, geometry) candidate.
    shapes, _ = backend.query_shapes(sheet, geo_col)
    assert {c.row_id for c in shapes} == {rows[0], rows[1], rows[2]}
    nyc = next(c for c in shapes if c.row_id == rows[0])
    assert nyc.geometry["type"] == "Polygon"

    # bbox prefilter around NYC returns only the NYC polygon (Tokyo/Paris outside).
    nyc_box = (-75.0, 40.0, -73.0, 41.0)  # (min_lon, min_lat, max_lon, max_lat)
    shapes, _ = backend.query_shapes(sheet, geo_col, bbox=nyc_box)
    assert {c.row_id for c in shapes} == {rows[0]}

    tokyo_box = (138.0, 35.0, 140.0, 36.0)
    shapes, _ = backend.query_shapes(sheet, geo_col, bbox=tokyo_box)
    assert {c.row_id for c in shapes} == {rows[1]}

    # row_ids restriction, incl. empty allow-list.
    shapes, _ = backend.query_shapes(sheet, geo_col, row_ids=[rows[2]])
    assert {c.row_id for c in shapes} == {rows[2]}
    shapes, _ = backend.query_shapes(sheet, geo_col, row_ids=[])
    assert shapes == []

    # Sidecar lives outside project.db and is safe to delete + rebuild.
    assert (project.path / GEO_SIDECAR_FILENAME).exists()
    assert backend.db_path != project.db_path
    backend.close()
    (project.path / GEO_SIDECAR_FILENAME).unlink()
    assert project.get_values(sheet, name_col)[rows[0]] == "NYC"  # project.db intact

    rebuilt = GeoProjectionBackend(project)
    status = rebuilt.materialize_geo_shape_column(sheet, geo_col)
    assert status["valid_points"] == 3
    shapes, _ = rebuilt.query_shapes(sheet, geo_col, bbox=nyc_box)
    assert {c.row_id for c in shapes} == {rows[0]}
    rebuilt.close()

    # A non-geo_shape column raises a typed error, never a 500.
    other = GeoProjectionBackend(project)
    with pytest.raises(GeoProjectionError) as exc:
        other.materialize_geo_shape_column(sheet, name_col)
    assert exc.value.code == "not_geo_shape"
    other.close()

    # The rebuildable sidecar is excluded from the default project export.
    out = tmp_path / "export.frisket.zip"
    project.export(out)
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
    assert "project.db" in names
    assert not any(n.endswith(GEO_SIDECAR_FILENAME) for n in names)
