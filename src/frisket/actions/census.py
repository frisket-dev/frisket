"""US Census ACS demographic enrichment over one admitted row scope."""

from __future__ import annotations

from typing import Literal

from frisket.actions.census_types import CensusRecord
from frisket.actions.core import ActionCategory, action, map_batch
from frisket.actions.geospatial_types import CensusDemographics
from frisket.actions.types import (
    ActionParams,
    ColumnRef,
    GeoPoint,
    RowError,
    RowResult,
    Rows,
)


class CensusParams(ActionParams):
    source: ColumnRef[GeoPoint]
    geography: Literal["tract", "block_group"] = "tract"
    include_moe: bool = False


def census_outputs(params: CensusParams) -> set[str]:
    return {
        name
        for name in CensusRecord.model_fields
        if params.include_moe or not name.endswith("_moe")
    }


async def demographics(
    params: CensusParams,
    rows: Rows,
    census: CensusDemographics,
) -> dict[int, RowResult[CensusRecord] | RowError]:
    points: dict[int, GeoPoint] = {}
    result: dict[int, RowResult[CensusRecord] | RowError] = {}
    for row_id, row in rows.items():
        value = params.source.read(row)
        if isinstance(value, GeoPoint):
            points[row_id] = value
            continue
        if isinstance(value, dict):
            lat, lon = value.get("lat"), value.get("lon")
            if (
                isinstance(lat, (int, float))
                and not isinstance(lat, bool)
                and isinstance(lon, (int, float))
                and not isinstance(lon, bool)
                and -90 <= lat <= 90
                and -180 <= lon <= 180
            ):
                points[row_id] = GeoPoint(lat=lat, lon=lon)
                continue
        result[row_id] = RowError(
            "invalid_geo_point",
            "census_demographics requires a geo_point source column",
        )
    if points:
        records = await census.lookup_many(
            points, geography=params.geography, include_moe=params.include_moe
        )
        for row_id, record in records.items():
            result[row_id] = (
                record if isinstance(record, RowError) else RowResult(output=record)
            )
    return result


CENSUS_DEMOGRAPHICS = action(
    name="census_demographics",
    title="Census Demographics",
    description=(
        "Append US Census ACS demographic fields to rows with a geographic point."
    ),
    category=ActionCategory.EXTRACT,
    run=map_batch(demographics, active_outputs=census_outputs),
    examples=(CensusParams(source="location"),),
)
