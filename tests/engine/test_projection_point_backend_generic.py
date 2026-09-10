from __future__ import annotations

import hashlib

import pytest
from fastapi.testclient import TestClient

import frisket.server.services.map_points as map_points_service
from frisket.ai.llm import ModelRouter
from frisket.authoring.plugin_registry import (
    _reset_default_registry_for_tests,
    default_registry,
    register_trusted_backend_handler,
    unregister_trusted_backend_handler,
)
from frisket.engine.projections.point_backend import GeoProjectionBackend
from frisket.engine.projections.point_wire import (
    MapPoint,
    serialize_map_points_arrow,
)
from frisket.server.app import create_app
from frisket.engine.store import Project
from workbench_runtime_test_helpers import activate_runtime_plugin_for_project


MAP_RUNTIME_PLUGIN_ID = "frisket.projection.point_backend.test"


# ---------------------------------------------------------------------------
# (b) golden byte compare.
#
# Provenance: these sha256 hashes were captured 2026-07-02, BEFORE the
# geo-projection-host-rehome-v1 move, by calling then-live
# `frisket.geo.GeoProjectionBackend` / `frisket.geo.serialize_map_points_arrow`
# (module deleted by this task) with the fixtures below. The move is required
# to reproduce them exactly through the new `frisket.projections.point_backend`
# / `frisket.projections.point_wire` modules -- any drift means the rehome
# changed wire bytes, which the task note pins as NOT done.


WIRE_ONLY_GOLDEN_SHA256 = (
    "8e3e6a5f74f61dfbf87df16a6c682da53e3f3e5522a7bb61282622df558762d7"
)
EMPTY_TRANSIENT_GOLDEN_SHA256 = (
    "49359e42c7c62da61d8c09f9721922a786e48d0d8eb095e0c0e1c1609a2ad1d8"
)
FULL_BACKEND_GOLDEN_SHA256 = (
    "c619d99d6d17b38f256d225adf36e500919c07cf5fb25338038cd236f01fcd11"
)


def test_wire_format_golden_bytes_are_unchanged() -> None:
    points = [
        MapPoint(row_id=1, lon=-74.0060, lat=40.7128),
        MapPoint(row_id=2, lon=139.65, lat=35.6764),
    ]
    payload = serialize_map_points_arrow(
        points, transient=False, attributes={"attr:1": ["NYC", "Tokyo"]}
    )
    assert hashlib.sha256(payload).hexdigest() == WIRE_ONLY_GOLDEN_SHA256


def test_wire_format_empty_transient_golden_bytes_are_unchanged() -> None:
    payload = serialize_map_points_arrow([], transient=True)
    assert hashlib.sha256(payload).hexdigest() == EMPTY_TRANSIENT_GOLDEN_SHA256


def test_full_backend_golden_bytes_are_unchanged(tmp_path) -> None:
    """Same fixture shape as tests/test_geo_projection_backend.py::_seed,
    through the re-homed backend end to end (materialize -> query -> wire)."""
    project = Project.create(tmp_path / "golden.frisket", name="golden")
    try:
        sheet = project.add_sheet("places")
        name_col = project.add_column(sheet, "name", type="text")
        geo_col = project.add_column(sheet, "location", type="geo_point")
        project.add_rows(
            sheet,
            [
                {"name": "NYC", "location": {"lat": 40.7128, "lon": -74.0060}},
                {"name": "Tokyo", "location": {"lat": 35.6764, "lon": 139.65}},
                {"name": "BadLat", "location": {"lat": 91, "lon": 0}},
                {"name": "Empty", "location": None},
            ],
            {"name": name_col, "location": geo_col},
        )
        backend = GeoProjectionBackend(project)
        try:
            backend.materialize_geo_column(sheet, geo_col)
            points, status = backend.query_points(sheet, geo_col)
            payload = serialize_map_points_arrow(points, transient=status["transient"])
        finally:
            backend.close()
    finally:
        project.close()

    assert hashlib.sha256(payload).hexdigest() == FULL_BACKEND_GOLDEN_SHA256


# ---------------------------------------------------------------------------
# (c) backend selection honors the role=`map_points` binding and refuses
# without one -- exercised through the real service function by flipping the
# documented bridge flag, not a parallel throwaway code path.


@pytest.fixture
def env(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    project = Project.create(ws / "proj1.frisket", name="proj1")
    sheet = project.add_sheet("places")
    geo_col = project.add_column(sheet, "location", type="geo_point")
    rows = project.add_rows(
        sheet,
        [{"location": {"lat": 40.7128, "lon": -74.0060}}],
        {"location": geo_col},
    )
    project.close()
    client = TestClient(
        create_app(ws, router=ModelRouter(cache=None, cache_mode="off"))
    )
    return {
        "client": client,
        "pid": "proj1",
        "sheet": sheet,
        "geo_col": geo_col,
        "rows": rows,
        "tmp_path": tmp_path,
    }


def _get_points(env, **params):
    return env["client"].get(
        f"/api/projects/{env['pid']}/sheets/{env['sheet']}/map/points",
        params=params,
    )


def test_backend_selection_serves_unconditionally_in_first_party_world(
    env, monkeypatch
) -> None:
    """Today's bridge state: the map view's descriptor is
    still first-party, so no binding is not a refusal."""
    monkeypatch.setattr(
        map_points_service, "MAP_POINTS_DESCRIPTOR_WORLD_IS_PLUGIN_OWNED", False
    )
    r = _get_points(env, column_id=env["geo_col"])
    assert r.status_code == 200, r.text


def test_backend_selection_refuses_without_binding_in_plugin_owned_world(
    env, monkeypatch
) -> None:
    """The disabled-geo semantic: once the descriptor
    world is plugin-owned, no active role=`map_points` binding means no map
    -- a typed refusal, not a 500 and not silently-served first-party data."""
    monkeypatch.setattr(
        map_points_service, "MAP_POINTS_DESCRIPTOR_WORLD_IS_PLUGIN_OWNED", True
    )
    r = _get_points(env, column_id=env["geo_col"])
    assert r.status_code == 409, r.text
    body = r.json()
    assert body["detail"]["code"] == "map_points_binding_missing"


def test_backend_selection_serves_when_plugin_owned_and_binding_active(
    env, monkeypatch
) -> None:
    """Once a plugin contributes + activates the role=`map_points` binding,
    the plugin-owned world serves again -- the seam geo-bundled-plugin-v1
    flips by CONTRIBUTING the binding, not by editing the service."""
    monkeypatch.setattr(
        map_points_service, "MAP_POINTS_DESCRIPTOR_WORLD_IS_PLUGIN_OWNED", True
    )
    _reset_default_registry_for_tests()
    handler_key = f"{MAP_RUNTIME_PLUGIN_ID}:map-points"
    projection_kind = "frisket.projection.point_backend.test.map_points"

    def trusted_projection_handler(payload: dict) -> dict:
        return {
            "schemaVersion": "frisket.runtime_projection_status.v1",
            "status": "ready",
            "freshness": {"state": "fresh", "transient": False},
        }

    register_trusted_backend_handler(handler_key, trusted_projection_handler)
    try:
        default_registry().register_runtime_binding(
            "projections",
            projection_kind,
            handler_key=handler_key,
            handler=trusted_projection_handler,
            plugin=MAP_RUNTIME_PLUGIN_ID,
            metadata={"execution": {"role": "map_points"}},
        )
        project = env["client"].app.state.workspace.get(env["pid"])
        activate_runtime_plugin_for_project(
            project,
            env["tmp_path"],
            plugin_id=MAP_RUNTIME_PLUGIN_ID,
            runtime_bindings={"projections": {projection_kind: handler_key}},
            project_id=env["pid"],
        )

        r = _get_points(env, column_id=env["geo_col"])
        assert r.status_code == 200, r.text
        assert r.headers["x-frisket-runtime-projection-kind"] == projection_kind
    finally:
        unregister_trusted_backend_handler(handler_key)
        _reset_default_registry_for_tests()
