from __future__ import annotations

import math
from typing import Any, Self

from pydantic import BaseModel, model_validator

from frisket.actions.core import ActionCategory, action, map_rows
from frisket.actions.types import (
    ActionParams,
    ColumnRef,
    GeoPoint,
    Outcome,
    Row,
    RowResult,
)


class ToGeoPointParams(ActionParams):
    latitude_column: ColumnRef[int | float | str]
    longitude_column: ColumnRef[int | float | str]

    @model_validator(mode="after")
    def _different_columns(self) -> Self:
        if self.latitude_column.name == self.longitude_column.name:
            raise ValueError("latitude and longitude must come from different columns")
        return self


class ToGeoPointOutput(BaseModel):
    geo_point: Outcome[GeoPoint]


def _coordinate(value: Any, label: str, lower: float, upper: float) -> float:
    if value is None or value == "":
        raise ValueError(f"{label} is empty")
    if isinstance(value, bool):
        raise ValueError(f"{label} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label} must be numeric") from error
    if not math.isfinite(number):
        raise ValueError(f"{label} must be finite")
    if not lower <= number <= upper:
        raise ValueError(f"{label} must be between {lower:g} and {upper:g}")
    return number


def to_geo_point(
    params: ToGeoPointParams,
    row: Row,
) -> RowResult[ToGeoPointOutput]:
    latitude = _coordinate(params.latitude_column.read(row), "latitude", -90.0, 90.0)
    longitude = _coordinate(
        params.longitude_column.read(row), "longitude", -180.0, 180.0
    )
    return RowResult(
        output=ToGeoPointOutput(
            geo_point=Outcome.ok(
                GeoPoint(lat=latitude, lon=longitude),
                confidence=1.0,
                justification=(
                    f"converted from {params.latitude_column.name} "
                    f"and {params.longitude_column.name}"
                ),
            )
        )
    )


TO_GEO_POINT = action(
    examples=(
        ToGeoPointParams(latitude_column="latitude", longitude_column="longitude"),
    ),
    name="to_geo_point",
    title="Convert latitude/longitude to geo_point",
    description=(
        "Build a geo_point from existing latitude and longitude columns. "
        "Validates ranges locally and does not call a geocoder."
    ),
    category=ActionCategory.CONVERT,
    run=map_rows(to_geo_point),
)
