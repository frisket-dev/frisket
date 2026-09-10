"""Typed domain values shared by geospatial actions and admitted capabilities."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, Mapping, Protocol

from pydantic import BaseModel

from frisket.actions.types import GeoPoint, Outcome, RowError

if TYPE_CHECKING:
    from frisket.actions.census_types import CensusRecord


class GeocodedAddress(BaseModel):
    geo_point: Outcome[GeoPoint | None]
    formatted_address: str | None


class Geocoder(Protocol):
    async def lookup(self, address: str) -> GeocodedAddress: ...


class CensusDemographics(Protocol):
    async def lookup_many(
        self,
        points: Mapping[int, GeoPoint],
        *,
        geography: Literal["tract", "block_group"],
        include_moe: bool,
    ) -> Mapping[int, CensusRecord | RowError]: ...
