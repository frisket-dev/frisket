from __future__ import annotations

import pytest

from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.types import ActionRequest, SheetRows
from frisket.actions.system import BoundTypedActionRequest
from frisket.ai.llm import ModelRouter
from frisket.engine.executor.map_rows_action import run_typed_map_rows_action
from frisket.engine.projections.point_backend import (
    GEO_SIDECAR_FILENAME,
    GeoProjectionBackend,
    GeoProjectionError,
)
from frisket.engine.projections.point_wire import (
    MAP_POINTS_ARROW_SCHEMA,
    MapPoint,
    serialize_map_points_arrow,
)
from frisket.engine.runner.map_runner import MapRunner
from frisket.engine.store import Project
from frisket.engine.store.receipts import ReceiptStore
from frisket.execution.attempt_authority import UnroutedOnlyAuthority


@pytest.fixture
def project(tmp_path):
    p = Project.create(tmp_path / "geo.frisket", name="geo")
    yield p
    p.close()


def _seed(project: Project):
    """A sheet with a source geo_point column: 2 valid, 1 out-of-range, 1 empty."""
    sheet = project.add_sheet("places")
    name_col = project.add_column(sheet, "name", type="text")
    geo_col = project.add_column(sheet, "location", type="geo_point")
    cols = {"name": name_col, "location": geo_col}
    rows = project.add_rows(
        sheet,
        [
            {"name": "NYC", "location": {"lat": 40.7128, "lon": -74.0060}},
            {"name": "Tokyo", "location": {"lat": 35.6764, "lon": 139.65}},
            {"name": "BadLat", "location": {"lat": 91, "lon": 0}},  # out of range
            {"name": "Empty", "location": None},  # missing
        ],
        cols,
    )
    return sheet, geo_col, name_col, rows


def test_source_cell_points_materialize(project):
    sheet, geo_col, _name, rows = _seed(project)
    backend = GeoProjectionBackend(project)
    status = backend.materialize_geo_column(sheet, geo_col)

    assert status["status"] == "ready"
    assert status["valid_points"] == 2
    assert status["invalid_points"] == 2
    assert status["total_rows"] == 4

    points, _ = backend.query_points(sheet, geo_col)
    assert {p.row_id for p in points} == {rows[0], rows[1]}
    # deck.gl getPosition order is [lon, lat]
    nyc = next(p for p in points if p.row_id == rows[0])
    assert nyc.lon == pytest.approx(-74.0060)
    assert nyc.lat == pytest.approx(40.7128)


def test_sidecar_file_created_outside_project_db(project):
    sheet, geo_col, _name, _rows = _seed(project)
    backend = GeoProjectionBackend(project)
    backend.materialize_geo_column(sheet, geo_col)
    assert (project.path / GEO_SIDECAR_FILENAME).exists()
    assert backend.db_path != project.db_path


def test_backend_id_and_version_exposed(project):
    sheet, geo_col, _name, _rows = _seed(project)
    backend = GeoProjectionBackend(project)
    status = backend.materialize_geo_column(sheet, geo_col)
    assert status["backend_id"] == "sqlite-rtree"
    assert status["backend_version"] == "1"
    assert GeoProjectionBackend.BACKEND_ID == "sqlite-rtree"


def test_bbox_returns_only_points_inside(project):
    sheet, geo_col, _name, rows = _seed(project)
    backend = GeoProjectionBackend(project)
    # Box around NYC only; Tokyo (lon 139) is far outside.
    points, _ = backend.query_points(sheet, geo_col, bbox=(-75.0, 40.0, -73.0, 41.0))
    assert {p.row_id for p in points} == {rows[0]}


def test_row_ids_restriction(project):
    sheet, geo_col, _name, rows = _seed(project)
    backend = GeoProjectionBackend(project)
    points, _ = backend.query_points(sheet, geo_col, row_ids=[rows[1]])
    assert {p.row_id for p in points} == {rows[1]}
    # empty allow-list yields nothing
    points, _ = backend.query_points(sheet, geo_col, row_ids=[])
    assert points == []


def test_deleting_sidecar_never_corrupts_project_and_rebuilds(project):
    sheet, geo_col, name_col, rows = _seed(project)
    backend = GeoProjectionBackend(project)
    backend.materialize_geo_column(sheet, geo_col)
    backend.close()

    (project.path / GEO_SIDECAR_FILENAME).unlink()

    # project.db is still fully intact after losing the sidecar
    assert project.get_values(sheet, name_col)[rows[0]] == "NYC"

    rebuilt = GeoProjectionBackend(project)
    status = rebuilt.materialize_geo_column(sheet, geo_col)
    assert status["valid_points"] == 2
    rebuilt.close()


def test_hidden_rows_excluded(project):
    sheet, geo_col, _name, rows = _seed(project)
    # Hide a valid row BEFORE the first build so no cached projection masks it.
    project.db.execute("UPDATE rows SET hidden=1 WHERE id=?", (rows[0],))
    project.db.commit()
    backend = GeoProjectionBackend(project)
    points, status = backend.query_points(sheet, geo_col)
    assert {p.row_id for p in points} == {rows[1]}
    assert status["valid_points"] == 1


def test_hidden_column_raises_typed_error(project):
    sheet, geo_col, _name, _rows = _seed(project)
    project.db.execute("UPDATE columns SET hidden=1 WHERE id=?", (geo_col,))
    project.db.commit()
    backend = GeoProjectionBackend(project)
    with pytest.raises(GeoProjectionError) as exc:
        backend.materialize_geo_column(sheet, geo_col)
    assert exc.value.code == "column_not_found"


def test_hidden_sheet_raises_typed_error(project):
    sheet, geo_col, _name, _rows = _seed(project)
    project.db.execute("UPDATE sheets SET hidden=1 WHERE id=?", (sheet,))
    project.db.commit()
    backend = GeoProjectionBackend(project)
    with pytest.raises(GeoProjectionError) as exc:
        backend.materialize_geo_column(sheet, geo_col)
    assert exc.value.code == "sheet_not_found"


def test_non_geo_point_column_raises_typed_error(project):
    sheet, _geo_col, name_col, _rows = _seed(project)
    backend = GeoProjectionBackend(project)
    with pytest.raises(GeoProjectionError) as exc:
        backend.materialize_geo_column(sheet, name_col)
    assert exc.value.code == "not_geo_point"


def test_invalid_geo_point_values_are_excluded_not_errored(project):
    """A malformed stored value must be counted invalid and skipped, never crash
    materialization (handoff: never weaken validation, never 500)."""
    sheet = project.add_sheet("p")
    geo_col = project.add_column(sheet, "loc", type="geo_point")
    name_col = project.add_column(sheet, "n", type="text")
    rows = project.add_rows(
        sheet,
        [
            {"n": "ok", "loc": {"lat": 1.0, "lon": 2.0}},
            {"n": "garbage", "loc": "not-json"},
            {"n": "wrongshape", "loc": {"latitude": 1, "longitude": 2}},
        ],
        {"n": name_col, "loc": geo_col},
    )
    backend = GeoProjectionBackend(project)
    status = backend.materialize_geo_column(sheet, geo_col)
    assert status["valid_points"] == 1
    assert status["invalid_points"] == 2
    points, _ = backend.query_points(sheet, geo_col)
    assert {p.row_id for p in points} == {rows[0]}


def test_arrow_payload_roundtrip():
    import pyarrow as pa

    points = [
        MapPoint(row_id=10, lon=-74.0, lat=40.7),
        MapPoint(row_id=20, lon=139.65, lat=35.6),
    ]
    buf = serialize_map_points_arrow(points, transient=True)
    with pa.ipc.open_stream(buf) as reader:
        table = reader.read_all()
    assert table.schema.names == ["row_id", "lon", "lat"]
    assert table.schema.metadata[b"frisket_schema"] == MAP_POINTS_ARROW_SCHEMA.encode()
    assert table.schema.metadata[b"frisket_transient"] == b"1"
    assert table.column("row_id").to_pylist() == [10, 20]
    assert table.column("lon").to_pylist()[0] == pytest.approx(-74.0, abs=1e-4)
    assert table.column("lat").to_pylist()[1] == pytest.approx(35.6, abs=1e-4)


def test_default_export_excludes_geo_sidecar(project, tmp_path):
    """Item 7 (regression guard): the rebuildable project.geo.db sidecar must
    never be written into the default project export.
    Export is allowlist-based, so this locks that behavior against a future
    allowlist→denylist regression."""
    import zipfile

    sheet, geo_col, _name, _rows = _seed(project)
    backend = GeoProjectionBackend(project)
    backend.materialize_geo_column(sheet, geo_col)
    backend.close()
    assert (project.path / GEO_SIDECAR_FILENAME).exists()  # sidecar exists on disk

    out = tmp_path / "export.frisket.zip"
    project.export(out)
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
    assert "project.db" in names
    assert GEO_SIDECAR_FILENAME not in names
    assert not any(n.endswith(GEO_SIDECAR_FILENAME) for n in names)


def test_to_geo_point_conversion_feeds_the_map(tmp_path):
    """End-to-end data-layer chain: lat/lon source columns -> the real
    typed to_geo_point action -> the generated geo_point column -> the map
    projection. Proves the map reads exactly what the conversion writes (the
    geo_point column is run-backed via current_run_id)."""
    project = Project.create(tmp_path / "geo.frisket", name="geo")
    sheet = project.add_sheet("places")
    cols = {
        "name": project.add_column(sheet, "name", type="text"),
        "latitude": project.add_column(sheet, "latitude", type="number"),
        "longitude": project.add_column(sheet, "longitude", type="number"),
    }
    rows = project.add_rows(
        sheet,
        [
            {"name": "Tokyo", "latitude": 35.6764, "longitude": 139.65},
            {"name": "NYC", "latitude": 40.7128, "longitude": -74.006},
            {"name": "Bad", "latitude": 91, "longitude": 0},  # out of range
        ],
        cols,
    )

    request = ActionRequest(
        action_id="map.to_geo_point",
        scope=SheetRows(sheet_id=sheet, row_ids=tuple(rows)),
        params={
            "latitude_column": "latitude",
            "longitude_column": "longitude",
        },
        output_names={"geo_point": "location"},
        idempotency_key="geo-projection@1",
    )

    def runner_factory(current: Project, router: ModelRouter | None) -> MapRunner:
        return MapRunner(
            current,
            router or ModelRouter(cache=None, cache_mode="off"),
            authority=UnroutedOnlyAuthority(current),
        )

    result = run_typed_map_rows_action(
        project,
        "geo-project",
        BoundTypedActionRequest.bind(ACTION_REGISTRY.get(request.action_id), request),
        ModelRouter(cache=None, cache_mode="off"),
        runner_factory,
    )

    assert result.status == "partial", result.errors
    assert result.run_id is not None
    assert result.receipt_id is not None
    assert [(output.kind, output.name) for output in result.outputs] == [
        ("column", "location")
    ]
    geo_col = next(c for c in project.columns(sheet) if c["name"] == "location")
    assert geo_col["type"] == "geo_point"
    assert project.get_values(sheet, geo_col["id"], row_ids=rows) == {
        rows[0]: {"lat": 35.6764, "lon": 139.65},
        rows[1]: {"lat": 40.7128, "lon": -74.006},
        rows[2]: None,
    }

    receipt = ReceiptStore(project).parsed_by_id(str(result.receipt_id))
    assert receipt is not None
    assert [(item.ref["name"], item.ref["type"]) for item in receipt.inputs] == [
        ("latitude", "number"),
        ("longitude", "number"),
    ]
    assert receipt.outputs[0].ref["kind"] == "map_result_column"
    assert receipt.outputs[0].ref["type"] == "geo_point"

    # The map projection reads the run-backed geo_point values (2 valid, 1 bad).
    backend = GeoProjectionBackend(project)
    points, status = backend.query_points(sheet, geo_col["id"])
    assert status["valid_points"] == 2
    assert status["invalid_points"] == 1
    assert {p.row_id for p in points} == {rows[0], rows[1]}
    tokyo = next(p for p in points if p.row_id == rows[0])
    assert tokyo.lon == pytest.approx(139.65, abs=1e-3)
    assert tokyo.lat == pytest.approx(35.6764, abs=1e-3)
    backend.close()
    project.close()


def test_concurrent_first_requests_do_not_race_schema(tmp_path):
    """Concurrent first map requests must not race sidecar schema creation (the
    rtree virtual table + shadow tables). FastAPI sync routes run in a
    threadpool, so first-ever requests across columns hit ensure_schema()
    simultaneously on separate sidecar connections."""
    import concurrent.futures as cf

    project = Project.create(tmp_path / "geo.frisket", name="geo")
    sheet = project.add_sheet("places")
    name_col = project.add_column(sheet, "name", type="text")
    geo_cols = [
        project.add_column(sheet, f"loc{i}", type="geo_point") for i in range(4)
    ]
    column_ids = {"name": name_col, **{f"loc{i}": geo_cols[i] for i in range(4)}}
    project.add_rows(
        sheet,
        [
            {
                "name": "a",
                **{f"loc{i}": {"lat": 1.0 + i, "lon": 2.0 + i} for i in range(4)},
            },
            {
                "name": "b",
                **{f"loc{i}": {"lat": 3.0 + i, "lon": 4.0 + i} for i in range(4)},
            },
        ],
        column_ids,
    )

    def run(col_id: int) -> int:
        backend = GeoProjectionBackend(project)
        try:
            _points, status = backend.query_points(sheet, col_id)
            return status["valid_points"]
        finally:
            backend.close()

    # 16 concurrent first-time requests spread over 4 columns: exercises both
    # same-(sheet,col) lock contention and cross-column schema creation.
    targets = [geo_cols[i % len(geo_cols)] for i in range(16)]
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(run, targets))
    assert results == [2] * 16
    project.close()
