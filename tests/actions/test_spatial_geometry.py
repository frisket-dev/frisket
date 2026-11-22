"""Unit tests for the pure-stdlib point-in-polygon spatial geometry module."""

from __future__ import annotations

import json
import math
from typing import Any

import pytest

from frisket.actions.spatial_geometry import (
    parse_geo_point,
    point_in_prepared_shape,
    prepare_shape,
)


def point_in_shape(lon: float, lat: float, shape: Any) -> bool:
    prepared = prepare_shape(shape)
    if prepared is None:
        return False
    return point_in_prepared_shape(lon, lat, prepared)


SQUARE = {
    "type": "Polygon",
    "coordinates": [[[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0], [0.0, 0.0]]],
}

DIAMOND = {
    "type": "Polygon",
    "coordinates": [[[0.0, 5.0], [5.0, 0.0], [10.0, 5.0], [5.0, 10.0], [0.0, 5.0]]],
}

CONCAVE_U = {
    "type": "Polygon",
    "coordinates": [
        [
            [0.0, 0.0],
            [10.0, 0.0],
            [10.0, 10.0],
            [7.0, 10.0],
            [7.0, 3.0],
            [3.0, 3.0],
            [3.0, 10.0],
            [0.0, 10.0],
            [0.0, 0.0],
        ]
    ],
}

DONUT = {
    "type": "Polygon",
    "coordinates": [
        [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0], [0.0, 0.0]],
        [[3.0, 3.0], [7.0, 3.0], [7.0, 7.0], [3.0, 7.0], [3.0, 3.0]],
    ],
}

MULTIPOLYGON = {
    "type": "MultiPolygon",
    "coordinates": [
        [[[0.0, 0.0], [4.0, 0.0], [4.0, 4.0], [0.0, 4.0], [0.0, 0.0]]],
        [
            [[10.0, 10.0], [20.0, 10.0], [20.0, 20.0], [10.0, 20.0], [10.0, 10.0]],
            [[13.0, 13.0], [17.0, 13.0], [17.0, 17.0], [13.0, 17.0], [13.0, 13.0]],
        ],
    ],
}


def test_parse_geo_point_valid_and_invalid() -> None:
    assert parse_geo_point({"lat": 40.7128, "lon": -74.006}) == (-74.006, 40.7128)
    assert parse_geo_point('{"lat": 0, "lon": 0}') == (0.0, 0.0)
    assert parse_geo_point({"lat": -90, "lon": 180}) == (180.0, -90.0)
    assert parse_geo_point({"lat": 90, "lon": -180}) == (-180.0, 90.0)

    invalid_values: list[Any] = [
        None,
        "",
        "   ",
        "{not json",
        [],
        {"lat": 91.0, "lon": 0.0},
        {"lat": -90.1, "lon": 0.0},
        {"lat": 0.0, "lon": 181.0},
        {"lat": 0.0, "lon": -180.1},
        {"lat": True, "lon": 0.0},
        {"lat": 0.0, "lon": False},
        {"lat": float("nan"), "lon": 0.0},
        {"lat": 0.0, "lon": float("inf")},
        {"lat": "40.7", "lon": "-74.0"},
        {"lat": 40.7},
        {"lon": -74.0},
    ]
    for value in invalid_values:
        assert parse_geo_point(value) is None, f"expected None for {value!r}"


def test_square_inside_outside_vertices_and_edges() -> None:
    # Strictly inside
    assert point_in_shape(5.0, 5.0, SQUARE) is True
    assert point_in_shape(0.0001, 9.9999, SQUARE) is True

    # Strictly outside
    assert point_in_shape(-0.001, 5.0, SQUARE) is False
    assert point_in_shape(10.001, 5.0, SQUARE) is False
    assert point_in_shape(5.0, -0.001, SQUARE) is False
    assert point_in_shape(5.0, 10.001, SQUARE) is False

    # Exact vertices
    for vx, vy in [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]:
        assert point_in_shape(vx, vy, SQUARE) is True

    # Horizontal & vertical edges
    assert point_in_shape(5.0, 0.0, SQUARE) is True
    assert point_in_shape(5.0, 10.0, SQUARE) is True
    assert point_in_shape(0.0, 5.0, SQUARE) is True
    assert point_in_shape(10.0, 5.0, SQUARE) is True

    # Epsilon boundary tolerance vs clearly outside
    assert point_in_shape(5.0, 10.0 + 1e-13, SQUARE) is True
    assert point_in_shape(5.0, 10.0 + 1e-6, SQUARE) is False


def test_diagonal_edges_and_diamond() -> None:
    assert point_in_shape(5.0, 5.0, DIAMOND) is True
    # Midpoints of all 4 diagonal edges
    assert point_in_shape(2.5, 2.5, DIAMOND) is True
    assert point_in_shape(7.5, 2.5, DIAMOND) is True
    assert point_in_shape(7.5, 7.5, DIAMOND) is True
    assert point_in_shape(2.5, 7.5, DIAMOND) is True
    # Points outside diagonal edges but inside bounding box
    assert point_in_shape(1.0, 1.0, DIAMOND) is False
    assert point_in_shape(9.0, 9.0, DIAMOND) is False
    assert point_in_shape(1.0, 9.0, DIAMOND) is False
    assert point_in_shape(9.0, 1.0, DIAMOND) is False


def test_concave_polygon_and_horizontal_ray_through_vertices() -> None:
    # Inside left arm, right arm, and bottom bridge
    assert point_in_shape(1.5, 8.0, CONCAVE_U) is True
    assert point_in_shape(8.5, 8.0, CONCAVE_U) is True
    assert point_in_shape(5.0, 1.5, CONCAVE_U) is True

    # Inside the concave notch (must be False!)
    assert point_in_shape(5.0, 5.0, CONCAVE_U) is False
    assert point_in_shape(5.0, 9.5, CONCAVE_U) is False

    # Horizontal ray aligned exactly with inner reflex vertices (y = 3.0)
    assert point_in_shape(1.5, 3.0, CONCAVE_U) is True
    assert point_in_shape(5.0, 3.0, CONCAVE_U) is True  # on inner horizontal edge
    assert point_in_shape(8.5, 3.0, CONCAVE_U) is True
    assert point_in_shape(-1.0, 3.0, CONCAVE_U) is False
    assert point_in_shape(11.0, 3.0, CONCAVE_U) is False


def test_polygon_with_interior_hole() -> None:
    # In the solid ring outside the hole
    assert point_in_shape(1.0, 5.0, DONUT) is True
    assert point_in_shape(9.0, 5.0, DONUT) is True
    assert point_in_shape(5.0, 1.0, DONUT) is True
    assert point_in_shape(5.0, 9.0, DONUT) is True

    # Strictly inside the hole -> False
    assert point_in_shape(5.0, 5.0, DONUT) is False
    assert point_in_shape(3.01, 3.01, DONUT) is False

    # On the hole boundary (edge & vertex) -> deterministic True (polygon boundary)
    assert point_in_shape(3.0, 3.0, DONUT) is True
    assert point_in_shape(5.0, 3.0, DONUT) is True
    assert point_in_shape(7.0, 5.0, DONUT) is True


def test_multipolygon_feature_wrapper_json_string_and_3d_coords() -> None:
    # MultiPolygon constituent polygons, gap, and hole
    assert point_in_shape(2.0, 2.0, MULTIPOLYGON) is True
    assert point_in_shape(11.0, 15.0, MULTIPOLYGON) is True
    assert point_in_shape(7.0, 7.0, MULTIPOLYGON) is False
    assert (
        point_in_shape(15.0, 15.0, MULTIPOLYGON) is False
    )  # inside second poly's hole

    # GeoJSON Feature wrapper
    feature = {
        "type": "Feature",
        "properties": {"name": "Zone A"},
        "geometry": MULTIPOLYGON,
    }
    assert point_in_shape(2.0, 2.0, feature) is True
    assert point_in_shape(15.0, 15.0, feature) is False

    # JSON-encoded string
    assert point_in_shape(2.0, 2.0, json.dumps(feature)) is True

    # Unclosed ring with 3D altitude coordinates
    unclosed_3d = {
        "type": "Polygon",
        "coordinates": [[[0.0, 0.0, 100.0], [4.0, 0.0, 100.0], [2.0, 4.0, 100.0]]],
    }
    assert point_in_shape(2.0, 1.0, unclosed_3d) is True
    assert point_in_shape(3.9, 3.9, unclosed_3d) is False

    # GeometryCollection containing a Point and a Polygon
    collection = {
        "type": "GeometryCollection",
        "geometries": [
            {"type": "Point", "coordinates": [99.0, 99.0]},
            SQUARE,
        ],
    }
    assert point_in_shape(5.0, 5.0, collection) is True
    assert point_in_shape(99.0, 99.0, collection) is False


@pytest.mark.parametrize(
    "bad_shape",
    [
        None,
        "",
        "{invalid json",
        42,
        [],
        {"type": "Point", "coordinates": [5.0, 5.0]},
        {"type": "LineString", "coordinates": [[0.0, 0.0], [10.0, 10.0]]},
        {"type": "MultiPoint", "coordinates": [[5.0, 5.0]]},
        {"type": "MultiLineString", "coordinates": [[[0.0, 0.0], [10.0, 10.0]]]},
        {"type": "Polygon", "coordinates": []},
        {"type": "Polygon", "coordinates": [[]]},
        {"type": "Polygon", "coordinates": [[[0.0, 0.0], [10.0, 0.0], [0.0, 0.0]]]},
        {
            "type": "Polygon",
            "coordinates": [
                [[0.0, 0.0], [10.0, 0.0], [10.0, float("nan")], [0.0, 10.0], [0.0, 0.0]]
            ],
        },
        {
            "type": "Polygon",
            "coordinates": [
                [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0], [0.0, 0.0]],
                [[3.0, 3.0], [7.0, True], [7.0, 7.0], [3.0, 3.0]],
            ],
        },
        {"type": "MultiPolygon", "coordinates": []},
        {"type": "Feature", "geometry": None},
        {
            "type": "GeometryCollection",
            "geometries": [{"type": "Point", "coordinates": [5.0, 5.0]}],
        },
    ],
)
def test_malformed_and_non_polygon_shapes_return_false(bad_shape: Any) -> None:
    assert prepare_shape(bad_shape) is None
    assert point_in_shape(5.0, 5.0, bad_shape) is False


def test_non_finite_or_bool_point_coordinates_return_false() -> None:
    prepared = prepare_shape(SQUARE)
    assert prepared is not None
    assert point_in_prepared_shape(math.nan, 5.0, prepared) is False
    assert point_in_prepared_shape(5.0, math.inf, prepared) is False
    assert point_in_prepared_shape(-math.inf, 5.0, prepared) is False
    assert point_in_shape(math.nan, 5.0, SQUARE) is False
    bad_bool: Any = True
    assert point_in_shape(bad_bool, 5.0, SQUARE) is False


def test_prepared_shape_bbox_prefilter_accuracy() -> None:
    prepared = prepare_shape(MULTIPOLYGON)
    assert prepared is not None
    assert prepared.bbox == (0.0, 0.0, 20.0, 20.0)
    assert prepared.polygons[0].bbox == (0.0, 0.0, 4.0, 4.0)
    assert prepared.polygons[1].bbox == (10.0, 10.0, 20.0, 20.0)

    # Fast grid sanity check: 25 non-overlapping polygons x 100 points
    grid_polys = []
    for gx in range(5):
        for gy in range(5):
            x0, y0 = float(gx * 2), float(gy * 2)
            shape = prepare_shape(
                {
                    "type": "Polygon",
                    "coordinates": [
                        [
                            [x0, y0],
                            [x0 + 1.0, y0],
                            [x0 + 1.0, y0 + 1.0],
                            [x0, y0 + 1.0],
                            [x0, y0],
                        ]
                    ],
                }
            )
            assert shape is not None
            grid_polys.append((gx, gy, shape))

    matches = 0
    for gx in range(5):
        for gy in range(5):
            px, py = gx * 2 + 0.5, gy * 2 + 0.5
            hit = [
                (pgx, pgy)
                for pgx, pgy, poly in grid_polys
                if point_in_prepared_shape(px, py, poly)
            ]
            assert hit == [(gx, gy)]
            matches += len(hit)
    assert matches == 25


def test_huge_integer_coordinates_never_raise_overflow_error() -> None:
    huge = 10**400
    assert parse_geo_point('{"lat": ' + "9" * 400 + ', "lon": 0}') is None
    assert parse_geo_point({"lat": huge, "lon": 0}) is None
    assert parse_geo_point({"lat": 0, "lon": -huge}) is None

    huge_poly = {
        "type": "Polygon",
        "coordinates": [[[0, 0], [huge, 0], [huge, huge], [0, huge], [0, 0]]],
    }
    assert prepare_shape(huge_poly) is None
    assert point_in_shape(0.0, 0.0, huge_poly) is False

    prepared = prepare_shape(SQUARE)
    assert prepared is not None
    assert point_in_prepared_shape(huge, 5.0, prepared) is False
    assert point_in_shape(5.0, -huge, SQUARE) is False


@pytest.mark.parametrize(
    "collinear_ring",
    [
        [[0.0, 0.0], [5.0, 0.0], [10.0, 0.0], [0.0, 0.0]],
        [[0.0, 0.0], [2.0, 2.0], [4.0, 4.0], [0.0, 0.0]],
        [[0.0, 0.0], [0.1, 0.2], [0.3, 0.6], [0.0, 0.0]],
    ],
)
def test_zero_area_collinear_rings_are_rejected(
    collinear_ring: list[list[float]],
) -> None:
    shape = {"type": "Polygon", "coordinates": [collinear_ring]}
    assert prepare_shape(shape) is None
    assert point_in_shape(collinear_ring[1][0], collinear_ring[1][1], shape) is False


def test_extreme_float_coordinates_do_not_overflow_segment_or_ray_math() -> None:
    big = 1e200
    big_square = {
        "type": "Polygon",
        "coordinates": [[[0.0, 0.0], [big, 0.0], [big, big], [0.0, big], [0.0, 0.0]]],
    }
    assert point_in_shape(big * 0.5, big * 0.5, big_square) is True
    assert point_in_shape(big * 0.5, 0.0, big_square) is True
    assert point_in_shape(-big * 0.5, big * 0.5, big_square) is False
