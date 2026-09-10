"""Address geocoding authored as a typed per-row transformation."""

from __future__ import annotations

from frisket.actions.core import ActionCategory, action, map_rows
from frisket.actions.geospatial_types import GeocodedAddress, Geocoder
from frisket.actions.types import (
    ActionParams,
    ColumnRef,
    EngineRef,
    Row,
    RowResult,
    Template,
    TextLike,
)


class GeocodeParams(ActionParams):
    source: ColumnRef[TextLike] | Template[TextLike]
    engine: EngineRef[Geocoder] = EngineRef[Geocoder]("auto")
    include_lat_lon: bool = False


class GeocodeOutput(GeocodedAddress):
    latitude: float | None = None
    longitude: float | None = None


def geocode_outputs(params: GeocodeParams) -> tuple[str, ...]:
    return ("geo_point", "formatted_address") + (
        ("latitude", "longitude") if params.include_lat_lon else ()
    )


async def geocode(
    params: GeocodeParams, row: Row, geocoder: Geocoder
) -> RowResult[GeocodeOutput]:
    source = params.source
    address = source.render(row) if isinstance(source, Template) else source.read(row)
    result = await geocoder.lookup("" if address is None else str(address))
    point = result.geo_point.value
    return RowResult(
        output=GeocodeOutput(
            geo_point=result.geo_point,
            formatted_address=result.formatted_address,
            latitude=point.lat if point is not None else None,
            longitude=point.lon if point is not None else None,
        )
    )


GEOCODE = action(
    name="geocode",
    title="Geocode addresses into geo_point columns",
    description="Geocode an address column or composed address with the selected provider.",
    category=ActionCategory.EXTRACT,
    run=map_rows(geocode, active_outputs=geocode_outputs),
    examples=(GeocodeParams(source="address"),),
)
