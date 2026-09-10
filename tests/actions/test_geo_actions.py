from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from frisket.actions.geo import TO_GEO_POINT, ToGeoPointParams, to_geo_point
from frisket.actions.types import GeoPoint, Outcome, Row
from frisket.actions.registry import ACTION_REGISTRY


def _params() -> ToGeoPointParams:
    return ToGeoPointParams(
        latitude_column="latitude",
        longitude_column="longitude",
    )


@pytest.mark.parametrize(
    ("latitude", "longitude", "expected"),
    [
        ("35.6764", "139.65", GeoPoint(lat=35.6764, lon=139.65)),
        (-90, -180.0, GeoPoint(lat=-90.0, lon=-180.0)),
        (90.0, 180, GeoPoint(lat=90.0, lon=180.0)),
    ],
)
def test_to_geo_point_converts_numeric_values_and_inclusive_bounds(
    latitude: object,
    longitude: object,
    expected: GeoPoint,
) -> None:
    result = to_geo_point(
        _params(),
        Row({"latitude": latitude, "longitude": longitude}),
    )

    assert result.output.geo_point == Outcome.ok(
        expected,
        confidence=1.0,
        justification="converted from latitude and longitude",
    )


@pytest.mark.parametrize(
    ("latitude", "longitude", "message"),
    [
        (None, 0, "latitude is empty"),
        ("", 0, "latitude is empty"),
        (True, 0, "latitude must be numeric"),
        ("north", 0, "latitude must be numeric"),
        (math.nan, 0, "latitude must be finite"),
        (math.inf, 0, "latitude must be finite"),
        (90.01, 0, "latitude must be between -90 and 90"),
        (0, False, "longitude must be numeric"),
        (0, "east", "longitude must be numeric"),
        (0, -math.inf, "longitude must be finite"),
        (0, -180.01, "longitude must be between -180 and 180"),
    ],
)
def test_to_geo_point_rejects_invalid_coordinates(
    latitude: object,
    longitude: object,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        to_geo_point(
            _params(),
            Row({"latitude": latitude, "longitude": longitude}),
        )


def test_to_geo_point_requires_distinct_source_columns() -> None:
    with pytest.raises(
        ValidationError,
        match="latitude and longitude must come from different columns",
    ):
        ToGeoPointParams(
            latitude_column="coordinate",
            longitude_column="coordinate",
        )


def test_to_geo_point_catalog_derives_sources_and_geo_point_output() -> None:
    registered = ACTION_REGISTRY.get("map.to_geo_point")
    entry = registered.catalog_entry()

    assert registered.definition is TO_GEO_POINT
    assert entry["ui_hints"]["category"] == "convert"
    assert entry["ui_hints"]["source_requirements"] == [
        {
            "id": "latitude_column",
            "param": "latitude_column",
            "label": "Latitude column",
            "mode": "column",
            "min": 1,
            "accepted_column_types": ["integer", "number", "text"],
        },
        {
            "id": "longitude_column",
            "param": "longitude_column",
            "label": "Longitude column",
            "mode": "column",
            "min": 1,
            "accepted_column_types": ["integer", "number", "text"],
        },
    ]
    assert entry["ui_hints"]["logical_outputs"] == [
        {"key": "geo_point", "column_type": "geo_point"}
    ]
    assert TO_GEO_POINT.run.output_fields[0].column_type == "geo_point"
    schema = TO_GEO_POINT.run.output_fields[0].schema
    assert schema["properties"]["lat"]["minimum"] == -90
    assert schema["properties"]["lat"]["maximum"] == 90
    assert schema["properties"]["lon"]["minimum"] == -180
    assert schema["properties"]["lon"]["maximum"] == 180
