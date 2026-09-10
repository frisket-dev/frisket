from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from frisket.ai.llm import ModelRouter
from frisket.authoring.plugin_registry import (
    _reset_default_registry_for_tests,
    default_registry,
    register_trusted_backend_handler,
    unregister_trusted_backend_handler,
)
from frisket.engine.projections.point_backend import MAP_POINTS_PROJECTION_KIND
from frisket.engine.projections.point_wire import (
    MAP_POINTS_ARROW_MEDIA_TYPE,
    MAP_POINTS_ARROW_SCHEMA,
    MapPoint,
)
from frisket.server.app import create_app
from frisket.engine.store import Project
from helpers import initialize_test_source_cells
from workbench_runtime_test_helpers import activate_runtime_plugin_for_project


MAP_RUNTIME_PLUGIN_ID = "frisket.geo.test"


@pytest.fixture(autouse=True)
def _registry_hygiene():
    """Every test here registers a role-map_points binding (directly or via
    the env fixture); reset the process-global registry afterwards so the
    binding never leaks into other modules."""
    yield
    _reset_default_registry_for_tests()


@pytest.fixture
def env(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    p = Project.create(ws / "proj1.frisket", name="proj1")
    sheet = p.add_sheet("places")
    name_col = p.add_column(sheet, "name", type="text")
    geo_col = p.add_column(sheet, "location", type="geo_point")
    rows = p.add_rows(
        sheet,
        [
            {"name": "NYC", "location": {"lat": 40.7128, "lon": -74.0060}},
            {"name": "Tokyo", "location": {"lat": 35.6764, "lon": 139.65}},
            {"name": "Bad", "location": {"lat": 91, "lon": 0}},
        ],
        {"name": name_col, "location": geo_col},
    )
    p.close()
    client = TestClient(
        create_app(ws, router=ModelRouter(cache=None, cache_mode="off"))
    )
    env = {
        "client": client,
        "pid": "proj1",
        "sheet": sheet,
        "geo_col": geo_col,
        "name_col": name_col,
        "rows": rows,
        "tmp_path": tmp_path,
    }
    # Plugin-owned map-points world: give the project its role binding (the
    # dispatch-focused tests reset the registry and install their own).
    _install_default_map_points_binding(client, tmp_path, "proj1")
    return env


def _get_points(env, **params):
    r = env["client"].get(
        f"/api/projects/{env['pid']}/sheets/{env['sheet']}/map/points",
        params=params,
    )
    return r


def _parse_arrow_points(content: bytes) -> tuple[dict, list[MapPoint]]:
    import pyarrow as pa

    with pa.ipc.open_stream(content) as reader:
        table = reader.read_all()
    meta = {
        "schema": table.schema.metadata.get(b"frisket_schema", b"").decode("utf-8"),
        "version": table.schema.metadata.get(b"frisket_map_points_version", b"").decode(
            "utf-8"
        ),
        "transient": table.schema.metadata.get(b"frisket_transient") == b"1",
        "count": table.num_rows,
    }
    points = [
        MapPoint(row_id=int(row_id), lon=float(lon), lat=float(lat))
        for row_id, lon, lat in zip(
            table.column("row_id").to_pylist(),
            table.column("lon").to_pylist(),
            table.column("lat").to_pylist(),
            strict=True,
        )
    ]
    return meta, points


def _activate_map_runtime(env, handler_key: str) -> None:
    project = env["client"].app.state.workspace.get(env["pid"])
    activate_runtime_plugin_for_project(
        project,
        env["tmp_path"],
        plugin_id=MAP_RUNTIME_PLUGIN_ID,
        runtime_bindings={"projections": {MAP_POINTS_PROJECTION_KIND: handler_key}},
        project_id=env["pid"],
    )


def _default_map_points_handler(payload: dict) -> dict:
    """Minimal HONEST role-map_points handler: plans from the real engine
    state the service passes through (params.pointBackend), never canned
    generations."""
    engine = (payload.get("params") or {}).get("pointBackend") or {}
    generation = str(engine.get("generationHash") or "") or None
    transient = bool(engine.get("transient"))
    if payload["schemaVersion"] == "frisket.runtime_projection_status_request.v1":
        return {
            "schemaVersion": "frisket.runtime_projection_status.v1",
            "status": "stale" if transient else "ready",
            "freshness": {
                "state": "transient" if transient else "fresh",
                "generation": generation,
                "transient": transient,
            },
        }
    return {
        "schemaVersion": "frisket.runtime_projection_build_plan.v1",
        "status": "accepted",
        "build": {
            "operation": "refresh",
            "idempotencyKey": f"map-points@{generation or 'unknown'}",
        },
    }


def _install_default_map_points_binding(client, tmp_path, project_id: str) -> None:
    """The map-points world is plugin-owned
    (MAP_POINTS_DESCRIPTOR_WORLD_IS_PLUGIN_OWNED=True, geo-bundled-plugin-v1):
    the route refuses with 409 map_points_binding_missing unless the project
    has an ACTIVE role="map_points" projection binding. These transport-focused
    tests supply the minimal honest one (the bundled geo plugin plays this
    role in the product; tests/test_workbench_plugin_geo_bundled.py covers
    that end to end)."""
    default_registry().register_runtime_binding(
        "projections",
        MAP_POINTS_PROJECTION_KIND,
        handler_key=f"{MAP_RUNTIME_PLUGIN_ID}:default-map-points",
        handler=_default_map_points_handler,
        plugin=MAP_RUNTIME_PLUGIN_ID,
        metadata={"execution": {"mode": "runtime_plan", "role": "map_points"}},
        replace=True,
    )
    project = client.app.state.workspace.get(project_id)
    activate_runtime_plugin_for_project(
        project,
        tmp_path,
        plugin_id=MAP_RUNTIME_PLUGIN_ID,
        runtime_bindings={
            "projections": {
                MAP_POINTS_PROJECTION_KIND: f"{MAP_RUNTIME_PLUGIN_ID}:default-map-points"
            }
        },
        project_id=project_id,
    )


def test_geo_point_column_returns_arrow_with_positions_and_row_ids(env):
    r = _get_points(env, column_id=env["geo_col"])
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == MAP_POINTS_ARROW_MEDIA_TYPE
    header, points = _parse_arrow_points(r.content)
    # only the 2 valid points; the out-of-range row is excluded
    assert header["count"] == 2
    assert {p.row_id for p in points} == {env["rows"][0], env["rows"][1]}
    nyc = next(p for p in points if p.row_id == env["rows"][0])
    assert nyc.lon == pytest.approx(-74.0060, abs=1e-4)
    assert nyc.lat == pytest.approx(40.7128, abs=1e-4)


def test_response_carries_schema_backend_generation_headers(env):
    r = _get_points(env, column_id=env["geo_col"])
    assert r.status_code == 200
    assert r.headers["x-frisket-map-schema"] == MAP_POINTS_ARROW_SCHEMA
    assert r.headers["x-frisket-map-backend"] == "sqlite-rtree"
    assert r.headers["x-frisket-map-backend-version"] == "1"
    assert r.headers["x-frisket-map-format"] == "arrow"
    assert r.headers["x-frisket-map-generation"].startswith("sha256:")
    assert r.headers["x-frisket-map-transient"] == "0"


def test_payload_is_not_grid_rows_or_cell_metadata(env):
    """The body must be the Arrow point payload, never JSON rows/cells."""
    r = _get_points(env, column_id=env["geo_col"])
    assert r.headers["content-type"] == MAP_POINTS_ARROW_MEDIA_TYPE
    # Arrow payload is not decodable as JSON (JSONDecodeError/UnicodeDecodeError
    # are both ValueError subclasses)
    with pytest.raises(ValueError):
        json.loads(r.content)


def test_non_geo_point_column_returns_typed_error(env):
    r = _get_points(env, column_id=env["name_col"])
    assert r.status_code == 422
    body = r.json()
    detail = body["detail"]
    assert detail["code"] == "not_geo_point"
    assert "geo_point" in detail["message"]


def test_bbox_filters_through_rtree(env):
    # Box around NYC only — Tokyo is far outside.
    r = _get_points(env, column_id=env["geo_col"], bbox="-75,40,-73,41")
    assert r.status_code == 200
    _header, points = _parse_arrow_points(r.content)
    assert {p.row_id for p in points} == {env["rows"][0]}


def test_invalid_bbox_returns_typed_400(env):
    r = _get_points(env, column_id=env["geo_col"], bbox="1,2,3")
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "invalid_bbox"


def test_reversed_bbox_returns_typed_400(env):
    r = _get_points(env, column_id=env["geo_col"], bbox="-73,40,-75,41")
    assert r.status_code == 400
    body = r.json()
    assert body["detail"]["code"] == "invalid_bbox"
    assert "min values" in body["detail"]["message"]


def test_filter_reuses_data_filter_semantics(env):
    # Same JSON filter shape as /data: {column: {op: value}}
    r = _get_points(
        env,
        column_id=env["geo_col"],
        filter=json.dumps({"name": {"eq": "NYC"}}),
    )
    assert r.status_code == 200
    _header, points = _parse_arrow_points(r.content)
    assert {p.row_id for p in points} == {env["rows"][0]}


def test_missing_column_id_is_rejected(env):
    r = env["client"].get(
        f"/api/projects/{env['pid']}/sheets/{env['sheet']}/map/points"
    )
    assert r.status_code == 422  # required query param


def test_unknown_project_is_404(env):
    r = env["client"].get(
        f"/api/projects/nope/sheets/{env['sheet']}/map/points",
        params={"column_id": env["geo_col"]},
    )
    assert r.status_code == 404


def test_arrow_format_returns_arrow_ipc_with_points(env):
    import pyarrow as pa

    r = _get_points(env, column_id=env["geo_col"], format="arrow")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == MAP_POINTS_ARROW_MEDIA_TYPE
    assert r.headers["x-frisket-map-schema"] == MAP_POINTS_ARROW_SCHEMA

    with pa.ipc.open_stream(r.content) as reader:
        table = reader.read_all()

    assert table.schema.names == ["row_id", "lon", "lat"]
    assert table.schema.metadata[b"frisket_schema"] == b"frisket.map_points.arrow.v1"
    assert table.schema.metadata[b"frisket_map_points_version"] == b"1"
    assert {int(x) for x in table.column("row_id").to_pylist()} == {
        env["rows"][0],
        env["rows"][1],
    }
    assert table.column("lon").type == pa.float32()
    assert table.column("lat").type == pa.float32()


def test_binary_format_is_not_a_fallback(env):
    r = _get_points(env, column_id=env["geo_col"], format="binary")
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "unsupported_format"


def test_attrs_param_adds_attribute_columns_aligned_to_points(env):
    """Feature #1 (color/size by column): `attrs=<col_id>` adds that column's live
    values as an extra Arrow column `attr:<col_id>`, aligned to the points, so the
    client can color/size markers. Attributes are resolved at query time, not
    materialized into the projection."""
    import pyarrow as pa

    r = _get_points(env, column_id=env["geo_col"], attrs=str(env["name_col"]))
    assert r.status_code == 200, r.text
    with pa.ipc.open_stream(r.content) as reader:
        table = reader.read_all()
    attr_col = f"attr:{env['name_col']}"
    assert attr_col in table.schema.names
    by_row = dict(
        zip(
            table.column("row_id").to_pylist(),
            table.column(attr_col).to_pylist(),
            strict=True,
        )
    )
    # NYC + Tokyo are the valid points; their name attribute rides along.
    assert by_row[env["rows"][0]] == "NYC"
    assert by_row[env["rows"][1]] == "Tokyo"


def test_attrs_param_ignores_unknown_columns(env):
    # A stale/unknown attr column id must not error the whole request, and must
    # not produce a phantom attribute column.
    import pyarrow as pa

    r = _get_points(env, column_id=env["geo_col"], attrs="999999")
    assert r.status_code == 200, r.text
    with pa.ipc.open_stream(r.content) as reader:
        table = reader.read_all()
    assert "attr:999999" not in table.schema.names
    assert not any(n.startswith("attr:") for n in table.schema.names)


def test_map_points_dispatches_trusted_runtime_projection_status_and_build(env):
    _reset_default_registry_for_tests()
    handler_key = f"{MAP_RUNTIME_PLUGIN_ID}:map-points"
    calls: list[dict] = []

    def trusted_projection_handler(payload: dict) -> dict:
        calls.append(payload)
        if payload["schemaVersion"] == "frisket.runtime_projection_status_request.v1":
            return {
                "schemaVersion": "frisket.runtime_projection_status.v1",
                "status": "stale",
                "freshness": {
                    "state": "stale",
                    "generation": "geo-gen-1",
                    "transient": False,
                },
                "outputs": {
                    "artifactRefs": [
                        {
                            "kind": "projection_artifact",
                            "projectionKind": MAP_POINTS_PROJECTION_KIND,
                            "artifactId": "projection://map-points/geo-gen-1",
                        }
                    ],
                    "metrics": {"candidateRows": 3},
                },
            }
        return {
            "schemaVersion": "frisket.runtime_projection_build_plan.v1",
            "status": "accepted",
            "build": {
                "operation": "refresh",
                "idempotencyKey": "map-points@geo-gen-2",
            },
            "outputs": {
                "artifactRefs": [
                    {
                        "kind": "projection_artifact",
                        "projectionKind": MAP_POINTS_PROJECTION_KIND,
                        "artifactId": "projection://map-points/geo-gen-2",
                    }
                ],
                "metrics": {"candidateRows": 2},
            },
        }

    register_trusted_backend_handler(handler_key, trusted_projection_handler)
    try:
        default_registry().register_runtime_binding(
            "projections",
            MAP_POINTS_PROJECTION_KIND,
            handler_key=handler_key,
            handler=trusted_projection_handler,
            plugin=MAP_RUNTIME_PLUGIN_ID,
            metadata={"execution": {"role": "map_points"}},
        )
        _activate_map_runtime(env, handler_key)

        r = _get_points(
            env,
            column_id=env["geo_col"],
            bbox="-75,40,-73,41",
            attrs=str(env["name_col"]),
        )

        assert r.status_code == 200, r.text
        assert (
            r.headers["x-frisket-runtime-projection-kind"] == MAP_POINTS_PROJECTION_KIND
        )
        assert r.headers["x-frisket-runtime-projection-status"] == "stale"
        assert r.headers["x-frisket-runtime-projection-generation"] == "geo-gen-1"
        assert r.headers["x-frisket-runtime-projection-build-status"] == "accepted"
        assert (
            r.headers["x-frisket-runtime-projection-build-idempotency-key"]
            == "map-points@geo-gen-2"
        )
        assert r.headers["x-frisket-runtime-projection-artifacts"] == (
            "projection://map-points/geo-gen-1"
        )

        _header, points = _parse_arrow_points(r.content)
        assert {p.row_id for p in points} == {env["rows"][0]}
        assert [call["schemaVersion"] for call in calls] == [
            "frisket.runtime_projection_status_request.v1",
            "frisket.runtime_projection_build_request.v1",
        ]
        assert calls[0]["projectId"] == env["pid"]
        assert calls[0]["pluginId"] == MAP_RUNTIME_PLUGIN_ID
        assert calls[0]["handlerKey"] == handler_key
        assert calls[0]["projectionKind"] == MAP_POINTS_PROJECTION_KIND
        assert calls[0]["target"] == {
            "sheetId": env["sheet"],
            "columnId": env["geo_col"],
        }
        # geo-bundled-plugin-v1: the service hands the REAL engine state to
        # the projection handler (params.pointBackend) so plugin planning
        # derives from real freshness. Recorded pin revision — the exact
        # params equality below now includes it.
        point_backend = calls[0]["params"].pop("pointBackend")
        assert point_backend == {
            "generationHash": r.headers["x-frisket-map-generation"],
            "transient": False,
            "validPoints": 2,
            "status": "ready",
        }
        assert calls[0]["params"] == {
            "format": "arrow",
            "bbox": [-75.0, 40.0, -73.0, 41.0],
            "attrs": str(env["name_col"]),
        }
        assert calls[1]["mode"] == "refresh"
    finally:
        unregister_trusted_backend_handler(handler_key)
        _reset_default_registry_for_tests()


def test_map_points_runtime_projection_invalid_response_fails_closed(env):
    _reset_default_registry_for_tests()
    handler_key = f"{MAP_RUNTIME_PLUGIN_ID}:bad-map-points"

    def invalid_projection_handler(_payload: dict) -> dict:
        return {
            "schemaVersion": "frisket.runtime_projection_status.v1",
            "status": "ready",
            "outputs": {"rawSql": "SELECT row_id, lon, lat FROM geo_points"},
        }

    register_trusted_backend_handler(handler_key, invalid_projection_handler)
    try:
        default_registry().register_runtime_binding(
            "projections",
            MAP_POINTS_PROJECTION_KIND,
            handler_key=handler_key,
            handler=invalid_projection_handler,
            plugin=MAP_RUNTIME_PLUGIN_ID,
            metadata={"execution": {"role": "map_points"}},
        )
        _activate_map_runtime(env, handler_key)

        r = _get_points(env, column_id=env["geo_col"])

        assert r.status_code == 502
        body = r.json()
        assert body["detail"]["code"] == "invalid_runtime_projection_status"
        assert body["detail"]["projection_kind"] == MAP_POINTS_PROJECTION_KIND
    finally:
        unregister_trusted_backend_handler(handler_key)
        _reset_default_registry_for_tests()


def test_map_points_validates_geo_column_before_runtime_projection_dispatch(env):
    _reset_default_registry_for_tests()
    handler_key = f"{MAP_RUNTIME_PLUGIN_ID}:must-not-run"
    calls: list[dict] = []

    def trusted_projection_handler(payload: dict) -> dict:
        calls.append(payload)
        return {
            "schemaVersion": "frisket.runtime_projection_status.v1",
            "status": "ready",
            "freshness": {"state": "fresh", "transient": False},
        }

    register_trusted_backend_handler(handler_key, trusted_projection_handler)
    try:
        default_registry().register_runtime_binding(
            "projections",
            MAP_POINTS_PROJECTION_KIND,
            handler_key=handler_key,
            handler=trusted_projection_handler,
            plugin=MAP_RUNTIME_PLUGIN_ID,
            metadata={"execution": {"role": "map_points"}},
        )
        _activate_map_runtime(env, handler_key)

        r = _get_points(env, column_id=env["name_col"])

        assert r.status_code == 422
        assert r.json()["detail"]["code"] == "not_geo_point"
        assert calls == []
    finally:
        unregister_trusted_backend_handler(handler_key)
        _reset_default_registry_for_tests()


# ---------------------------------------------------------------------------
# Hardening pass (red-first)


def _make_geo_env(tmp_path, rows):
    """Build a workspace + project with a source geo_point column and return
    (ws_path, project, sheet_id, geo_col, name_col, row_ids). Caller closes."""
    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    p = Project.create(ws / "proj1.frisket", name="proj1")
    sheet = p.add_sheet("places")
    name_col = p.add_column(sheet, "name", type="text")
    geo_col = p.add_column(sheet, "location", type="geo_point")
    row_ids = p.add_rows(sheet, rows, {"name": name_col, "location": geo_col})
    return ws, p, sheet, geo_col, name_col, row_ids


def test_edit_undo_redo_refreshes_map_payload(tmp_path):
    """Item 2: a manual geo_point edit (and its undo/redo) changes the map
    payload generation and visible point count served by the endpoint."""
    ws, p, sheet, geo_col, _name, rows = _make_geo_env(
        tmp_path,
        [
            {"name": "NYC", "location": {"lat": 40.7128, "lon": -74.0060}},
            {"name": "Bad", "location": {"lat": 91, "lon": 0}},  # invalid → excluded
        ],
    )
    client = TestClient(
        create_app(ws, router=ModelRouter(cache=None, cache_mode="off"))
    )
    _install_default_map_points_binding(client, tmp_path, "proj1")

    def fetch():
        r = client.get(
            f"/api/projects/proj1/sheets/{sheet}/map/points",
            params={"column_id": geo_col},
        )
        assert r.status_code == 200, r.text
        header, points = _parse_arrow_points(r.content)
        return (
            r.headers["x-frisket-map-generation"],
            header["count"],
            {pt.row_id for pt in points},
        )

    gen0, count0, ids0 = fetch()
    assert count0 == 1 and ids0 == {rows[0]}

    # Edit the invalid row into a valid point.
    p.apply_edits(
        [{"row_id": rows[1], "column_id": geo_col, "value": {"lat": 1.0, "lon": 2.0}}]
    )
    gen1, count1, ids1 = fetch()
    assert count1 == 2 and ids1 == {rows[0], rows[1]}
    assert gen1 != gen0  # generation refreshed

    # Undo restores the pre-edit projection.
    p.undo()
    gen2, count2, _ = fetch()
    assert count2 == 1 and gen2 == gen0

    # Redo restores the edited projection.
    p.redo()
    gen3, count3, _ = fetch()
    assert count3 == 2 and gen3 == gen1
    p.close()


def test_filter_row_cap_is_not_silent(tmp_path, monkeypatch):
    """Item 3: when the filtered rowset exceeds MAP_FILTER_ROW_CAP the response
    must SIGNAL the truncation, not silently drop rows."""
    ws, p, sheet, geo_col, _name, _rows = _make_geo_env(
        tmp_path,
        [
            {"name": "open", "location": {"lat": 1.0, "lon": 2.0}},
            {"name": "open", "location": {"lat": 3.0, "lon": 4.0}},
        ],
    )
    p.close()
    monkeypatch.setenv("FRISKET_MAP_FILTER_ROW_CAP", "1")
    client = TestClient(
        create_app(ws, router=ModelRouter(cache=None, cache_mode="off"))
    )
    _install_default_map_points_binding(client, tmp_path, "proj1")
    r = client.get(
        f"/api/projects/proj1/sheets/{sheet}/map/points",
        params={"column_id": geo_col, "filter": json.dumps({"name": {"eq": "open"}})},
    )
    assert r.status_code == 200, r.text
    # 2 rows match but cap=1 → truncated must be signalled, with the true total.
    assert r.headers["x-frisket-map-filter-truncated"] == "1"
    assert r.headers["x-frisket-map-filter-total"] == "2"


def test_filter_not_truncated_signals_zero(tmp_path):
    ws, p, sheet, geo_col, _name, _rows = _make_geo_env(
        tmp_path,
        [
            {"name": "open", "location": {"lat": 1.0, "lon": 2.0}},
            {"name": "open", "location": {"lat": 3.0, "lon": 4.0}},
        ],
    )
    p.close()
    client = TestClient(
        create_app(ws, router=ModelRouter(cache=None, cache_mode="off"))
    )
    _install_default_map_points_binding(client, tmp_path, "proj1")
    r = client.get(
        f"/api/projects/proj1/sheets/{sheet}/map/points",
        params={"column_id": geo_col, "filter": json.dumps({"name": {"eq": "open"}})},
    )
    assert r.status_code == 200
    assert r.headers["x-frisket-map-filter-truncated"] == "0"


def test_default_filtered_map_returns_every_point_beyond_legacy_cap(
    tmp_path, monkeypatch
):
    """Solo's default filter is complete; 100k remains a batch, not a total."""
    monkeypatch.delenv("FRISKET_MAP_FILTER_ROW_CAP", raising=False)
    total = 100_001
    ws, project, sheet, geo_col, name_col, _rows = _make_geo_env(tmp_path, [])
    project.db.execute(
        """
        WITH RECURSIVE sequence(value) AS (
          SELECT 1
          UNION ALL
          SELECT value + 1 FROM sequence WHERE value < ?
        )
        INSERT INTO rows (id, sheet_id, position, parent_row_id, hidden)
        SELECT value, ?, value, NULL, 0 FROM sequence
        """,
        (total, sheet),
    )
    initialize_test_source_cells(
        project,
        (
            (row_id, column_id, value)
            for row_id in range(1, total + 1)
            for column_id, value in (
                (name_col, "open"),
                (geo_col, {"lat": 1.0, "lon": 2.0}),
            )
        ),
    )
    project.close()

    client = TestClient(
        create_app(ws, router=ModelRouter(cache=None, cache_mode="off"))
    )
    _install_default_map_points_binding(client, tmp_path, "proj1")
    response = client.get(
        f"/api/projects/proj1/sheets/{sheet}/map/points",
        params={
            "column_id": geo_col,
            "filter": json.dumps({"name": {"eq": "open"}}),
        },
    )
    assert response.status_code == 200, response.text

    import pyarrow as pa

    with pa.ipc.open_stream(response.content) as reader:
        table = reader.read_all()
    assert table.num_rows == total
    row_ids = table.column("row_id")
    assert int(row_ids[0].as_py()) == 1
    assert int(row_ids[-1].as_py()) == total
    assert response.headers.get("x-frisket-map-filter-truncated") in {None, "0"}


def test_sort_param_does_not_reorder_payload(tmp_path):
    """Item 5: sort is accepted but the map payload order is deterministic
    (sheet position), not grid-sort order. We do NOT claim sort parity."""
    ws, p, sheet, geo_col, _name, rows = _make_geo_env(
        tmp_path,
        [
            {"name": "alpha", "location": {"lat": 1.0, "lon": 1.0}},
            {"name": "bravo", "location": {"lat": 2.0, "lon": 2.0}},
            {"name": "charlie", "location": {"lat": 3.0, "lon": 3.0}},
        ],
    )
    p.close()
    client = TestClient(
        create_app(ws, router=ModelRouter(cache=None, cache_mode="off"))
    )
    _install_default_map_points_binding(client, tmp_path, "proj1")

    def order(sort_dir):
        r = client.get(
            f"/api/projects/proj1/sheets/{sheet}/map/points",
            params={
                "column_id": geo_col,
                "sort": json.dumps([{"column": "name", "dir": sort_dir}]),
            },
        )
        assert r.status_code == 200, r.text
        _h, points = _parse_arrow_points(r.content)
        return [pt.row_id for pt in points]

    asc = order("asc")
    desc = order("desc")
    # Deterministic, sort-independent payload order (sheet position).
    assert asc == desc == rows
