"""Invocation-local Census GeoLookup and ACS capability.

The provider algorithm retains whole-scope deduplication, coordinate chunks,
geography-grouped ACS requests and bounded caches. The surrounding row host
owns routing, admission, materialization and receipts.
"""

from __future__ import annotations

import csv
import io
import json
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx

from frisket.actions.census_types import CensusRecord
from frisket.actions.types import GeoPoint, RowError
from frisket.ai.models.metadata import ModelCallMeta, model_calls_cost_actual
from frisket.credentials import ResolvedCredential, resolve_credential_for_use
from frisket.execution.attempt import routed_admission_in_scope
from frisket.execution.credential_use import (
    CredentialUseRefusal,
    require_consented_credential,
)
from frisket.execution.runtime_binding import ROUTE_OBSERVATION_KEY, bind_fact_to_route
from frisket.ops.base import OpContext, RecipeInvocationHalt
from frisket.ops.cost_source import free_public_api_estimate

CENSUS_GEOLOOKUP_BATCH_URL = (
    "https://geocoding.geo.census.gov/geocoder/geographies/coordinatesbatch"
)
CENSUS_ACS_URL_TEMPLATE = "https://api.census.gov/data/{vintage}/acs/acs5"
CENSUS_GEOLOOKUP_BATCH_SIZE = 10_000

DEFAULT_ACS_VINTAGE = "2024"
DEFAULT_GEOLOOKUP_BENCHMARK = "Public_AR_Current"
DEFAULT_GEOLOOKUP_VINTAGE = "ACS2024_Current"
DEFAULT_BLOCK_GEOLOOKUP_VINTAGE = "Current_Current"
MAX_GEO_CACHE_ENTRIES = 50_000
MAX_ACS_CACHE_ENTRIES = 2_000

CORE_VARIABLES = {
    "population": "B01003_001E",
    "median_age": "B01002_001E",
    "median_household_income": "B19013_001E",
    "poverty_total": "B17001_001E",
    "poverty_count": "B17001_002E",
    "education_total": "B15003_001E",
    "bachelors": "B15003_022E",
    "masters": "B15003_023E",
    "professional": "B15003_024E",
    "doctorate": "B15003_025E",
    "race_total": "B03002_001E",
    "white_non_hispanic": "B03002_003E",
    "black_non_hispanic": "B03002_004E",
    "asian_non_hispanic": "B03002_006E",
    "hispanic": "B03002_012E",
    "housing_total": "B25003_001E",
    "owner_occupied": "B25003_002E",
}

MOE_VARIABLES = {
    "population": "B01003_001M",
    "median_age": "B01002_001M",
    "median_household_income": "B19013_001M",
}


def _pct(
    numerator: float | int | None, denominator: float | int | None
) -> float | None:
    if numerator is None or denominator in (None, 0):
        return None
    return round((float(numerator) / float(denominator)) * 100, 2)


def _num(value: Any) -> float | int | None:
    if value in (None, "", "null"):
        return None
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    # Census missing-data sentinels are large negative values.
    if n <= -1_000_000:
        return None
    return int(n) if n.is_integer() else n


def _cancelled(ctx: OpContext) -> bool:
    cb = (ctx.extras or {}).get("cancelled")
    return bool(cb()) if callable(cb) else False


def _cache_put(cache: dict, key: Any, value: Any, max_entries: int) -> None:
    cache[key] = value
    if len(cache) > max_entries:
        cache.pop(next(iter(cache)))


def _response_detail(resp: Any, secret: str | None = None) -> str:
    detail = getattr(resp, "text", "") or ""
    if not detail:
        detail = str(getattr(resp, "headers", {}).get("location", ""))
    if secret:
        detail = detail.replace(secret, "[redacted]")
    detail = detail[:200]
    low = detail.lower()
    if "invalid key" in low:
        detail = "Invalid Census API key (check CENSUS_API_KEY)"
    elif "missing_key" in low or "api key" in low:
        detail = "Missing Census API key (set CENSUS_API_KEY for Census data access)"
    return detail


def _find_key(headers: list[str], *needles: str) -> str | None:
    lowered = {h.lower().strip(): h for h in headers}
    for needle in needles:
        if needle in lowered:
            return lowered[needle]
    for h in headers:
        low = h.lower().strip()
        if all(n in low for n in needles):
            return h
    return None


def _provider_accounting(
    ctx: OpContext, credential_source: str, *, units: dict[str, int | str]
) -> dict[str, Any]:
    """One host-observed row or HTTP invocation's known-zero provider fact.

    Census GeoLookup and ACS are free public APIs.  The call remains a durable
    external-provider fact, but it carries no pricing key or unit rate and its
    provider spend is known to be zero.

    Bind the observed credential and zero spend to the admitted route before
    the generic result store persists the fact and its route observation.
    """
    per_row = free_public_api_estimate(quantity=1)
    fact = ModelCallMeta.provider_call(
        capability="census_demographics",
        engine="us_census_acs",
        provider="us_census_acs",
        provider_kind="platform_api",
        credential_source=credential_source,
        provider_reported_cost_usd=None,
        provider_cost_usd=per_row["cost"],
        cost_source=str(per_row.get("cost_source") or "unknown"),
        units=units,
        duration_ms=None,
    ).as_dict()
    admission = routed_admission_in_scope(ctx.extras)
    if admission is not None:
        fact = bind_fact_to_route(admission.route, fact)
        if not fact.get(ROUTE_OBSERVATION_KEY):
            raise RuntimeError(
                "census_demographics fact built under a route carries no "
                "binding observation required by the route-epoch invariant"
            )
    return {
        **per_row,
        "cost": model_calls_cost_actual([fact]),
        "model_calls": [fact],
    }


def _clean_code(value: Any, width: int) -> str | None:
    s = str(value or "").strip()
    if not s:
        return None
    if s.endswith(".0"):
        s = s[:-2]
    if not s.isdigit():
        return None
    return s.zfill(width)


@dataclass(frozen=True)
class CensusGeo:
    state: str
    county: str
    tract: str
    block: str | None = None

    @property
    def tract_geoid(self) -> str:
        return f"{self.state}{self.county}{self.tract}"

    @property
    def block_group_geoid(self) -> str | None:
        if not self.block:
            return None
        return f"{self.tract_geoid}{self.block[0]}"


@dataclass
class AdmittedCensusDemographics:
    ctx: OpContext = field(repr=False)
    accounting_by_row: dict[int, dict[str, Any]] = field(
        default_factory=dict, init=False
    )
    request_accounting: dict[str, Any] = field(default_factory=dict, init=False)
    request_count: int = field(default=0, init=False)
    provider_details: dict[str, str] = field(default_factory=dict, init=False)
    _row_ids: frozenset[int] | None = field(default=None, init=False)
    _called: bool = field(default=False, init=False)
    _geo_cache: dict[tuple[str, str, float, float], CensusGeo] = field(
        default_factory=dict, init=False
    )
    _acs_cache: dict[
        tuple[str, str, str, tuple[str, ...], bool], dict[str, dict[str, Any]]
    ] = field(default_factory=dict, init=False)

    def bind_rows(self, row_ids: tuple[int, ...]) -> AdmittedCensusDemographics:
        if self._row_ids is not None:
            raise RecipeInvocationHalt(
                "promise_violation", "Census rows are already bound"
            )
        self._row_ids = frozenset(row_ids)
        return self

    async def aclose(self) -> None:
        pass

    def _record_request(self, ctx: OpContext, credential_source: str) -> None:
        accounting = _provider_accounting(
            ctx,
            credential_source,
            units={"requests": 1, **self.provider_details},
        )
        self.request_accounting.setdefault("model_calls", []).extend(
            accounting["model_calls"]
        )
        self.request_accounting["cost"] = 0.0
        self.request_count += 1

    async def lookup_many(
        self,
        points: Mapping[int, GeoPoint],
        *,
        geography: Literal["tract", "block_group"],
        include_moe: bool,
    ) -> Mapping[int, CensusRecord | RowError]:
        if self._row_ids is None or not set(points).issubset(self._row_ids):
            raise RecipeInvocationHalt(
                "promise_violation", "Census lookup contains unadmitted rows"
            )
        if self._called:
            raise RecipeInvocationHalt(
                "promise_violation",
                "Census supports one whole-scope lookup per invocation",
            )
        if geography not in {"tract", "block_group"} or not isinstance(
            include_moe, bool
        ):
            raise RowError(
                "invalid_census_options",
                "Invalid Census geography or margin-of-error option",
            )
        if any(not isinstance(point, GeoPoint) for point in points.values()):
            raise RowError(
                "invalid_geo_point", "Census lookup requires typed geographic points"
            )
        self._called = True
        self.provider_details = {
            "dataset": "acs/acs5",
            "vintage": DEFAULT_ACS_VINTAGE,
            "geography": geography,
        }
        ctx = self.ctx
        if ctx.http is None:
            raise RuntimeError("census demographics needs an http client")
        out: dict[int, CensusRecord | RowError] = {
            row_id: RowError("cancelled", "Census lookup was cancelled")
            for row_id in points
        }
        if _cancelled(ctx) or not points:
            return out
        admission = routed_admission_in_scope(ctx.extras)
        if (
            admission is None
            or admission.route.engine != "us_census_acs"
            or admission.route.target_snapshot.get("capability")
            != "census_demographics"
            or admission.route.target_snapshot.get("transport") != "census.acs5"
        ):
            raise RecipeInvocationHalt(
                "promise_violation",
                "Census lookup requires its admitted provider route",
            )
        credential = resolve_credential_for_use(
            getattr(ctx, "project", None),
            "CENSUS_API_KEY",
            context=ctx.credential_use_context,
        )
        if credential is None:
            raise RecipeInvocationHalt(
                "promise_violation", "Set CENSUS_API_KEY for Census data access"
            )
        cost_posture = admission.route.cost_posture
        try:
            require_consented_credential(
                cost_posture=cost_posture,
                selected_source=credential.source,
                selected_owner=credential.owner,
                context=ctx.credential_use_context,
                effect="the US Census API call",
            )
        except CredentialUseRefusal as exc:
            raise RecipeInvocationHalt(
                "promise_violation",
                f"{exc}; review the claims and re-consent to resume",
            ) from exc
        coordinates = {
            row_id: (point.lat, point.lon) for row_id, point in points.items()
        }
        geos = await self._lookup_geographies(coordinates, geography, ctx)
        if _cancelled(ctx):
            return out
        acs = await self._fetch_acs(
            geos.values(),
            geography,
            DEFAULT_ACS_VINTAGE,
            include_moe,
            ctx,
            credential=credential,
        )
        for row_id, point in coordinates.items():
            if _cancelled(ctx):
                break
            geo = geos.get(row_id)
            if geo is None:
                out[row_id] = RowError(
                    "census_geography_missing", f"no Census geography found for {point}"
                )
                continue
            area_id = (
                geo.block_group_geoid if geography == "block_group" else geo.tract_geoid
            )
            if area_id is None:
                out[row_id] = RowError(
                    "census_block_missing",
                    "Census GeoLookup did not return a block code",
                )
                continue
            out[row_id] = self._format_row(
                geo,
                acs.get(area_id),
                geography=geography,
                vintage=DEFAULT_ACS_VINTAGE,
                include_moe=include_moe,
            )
            self.accounting_by_row[row_id] = _provider_accounting(
                ctx,
                credential.source,
                units={"rows": 1, **self.provider_details},
            )
        return out

    async def _lookup_geographies(
        self,
        points: dict[int, tuple[float, float]],
        geography: str,
        ctx: OpContext,
    ) -> dict[int, CensusGeo]:
        benchmark = DEFAULT_GEOLOOKUP_BENCHMARK
        default_vintage = (
            DEFAULT_BLOCK_GEOLOOKUP_VINTAGE
            if geography == "block_group"
            else DEFAULT_GEOLOOKUP_VINTAGE
        )
        vintage = default_vintage
        resolved: dict[int, CensusGeo] = {}
        missing_by_key: dict[tuple[str, str, float, float], list[int]] = {}
        representative_points: dict[
            tuple[str, str, float, float], tuple[float, float]
        ] = {}
        for row_id, (lat, lon) in points.items():
            key = (benchmark, vintage, round(lat, 6), round(lon, 6))
            cached = self._geo_cache.get(key)
            if cached is not None:
                resolved[row_id] = cached
            else:
                missing_by_key.setdefault(key, []).append(row_id)
                representative_points.setdefault(key, (lat, lon))

        missing = [
            (row_ids[0], *representative_points[key], key)
            for key, row_ids in missing_by_key.items()
        ]
        for i in range(0, len(missing), CENSUS_GEOLOOKUP_BATCH_SIZE):
            if _cancelled(ctx):
                break
            chunk = missing[i : i + CENSUS_GEOLOOKUP_BATCH_SIZE]
            if not chunk:
                continue
            csv_text = self._points_csv(
                [(row_id, lat, lon) for row_id, lat, lon, _key in chunk]
            )
            self._record_request(ctx, "none")
            resp = await ctx.http.post(
                CENSUS_GEOLOOKUP_BATCH_URL,
                data={"benchmark": benchmark, "vintage": vintage},
                files={"coordinatesFile": ("points.csv", csv_text, "text/csv")},
            )
            if resp.status_code >= 300:
                raise RuntimeError(
                    f"Census GeoLookup request failed {resp.status_code}: "
                    f"{_response_detail(resp)}"
                )
            parsed = self._parse_geo_lookup_csv(resp.text)
            for row_id, lat, lon, key in chunk:
                geo = parsed.get(str(row_id))
                if geo is None:
                    continue
                for actual_row_id in missing_by_key.get(key, []):
                    resolved[actual_row_id] = geo
                _cache_put(self._geo_cache, key, geo, MAX_GEO_CACHE_ENTRIES)
        return resolved

    @staticmethod
    def _points_csv(points: list[tuple[int, float, float]]) -> str:
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        for row_id, lat, lon in points:
            writer.writerow([row_id, lon, lat])
        return buf.getvalue()

    @staticmethod
    def _parse_geo_lookup_csv(text: str) -> dict[str, CensusGeo]:
        rows = [r for r in csv.reader(io.StringIO(text)) if r]
        if not rows:
            return {}
        headers = [c.strip() for c in rows[0]]
        header_names = {h.lower().strip() for h in headers}
        has_header = bool(
            header_names & {"id", "unique id", "state", "county", "tract", "block"}
        )
        if has_header:
            id_key = _find_key(headers, "id", "unique id") or headers[0]
            state_key = _find_key(headers, "state")
            county_key = _find_key(headers, "county")
            tract_key = _find_key(headers, "tract")
            block_key = _find_key(headers, "block")
            out: dict[str, CensusGeo] = {}
            for rec in csv.DictReader(io.StringIO(text)):
                if state_key is None or county_key is None or tract_key is None:
                    continue
                state = _clean_code(rec.get(state_key), 2)
                county = _clean_code(rec.get(county_key), 3)
                tract = _clean_code(rec.get(tract_key), 6)
                block = _clean_code(rec.get(block_key), 4) if block_key else None
                if state and county and tract:
                    out[str(rec.get(id_key, "")).strip()] = CensusGeo(
                        state=state,
                        county=county,
                        tract=tract,
                        block=block,
                    )
            return out

        out = {}
        for row in rows:
            if len(row) < 7:
                continue
            state = _clean_code(row[4], 2)
            county = _clean_code(row[5], 3)
            tract = _clean_code(row[6], 6)
            block = _clean_code(row[7], 4) if len(row) > 7 else None
            if state and county and tract:
                out[row[0].strip()] = CensusGeo(state, county, tract, block)
        return out

    async def _fetch_acs(
        self,
        geos: Any,
        geography: str,
        vintage: str,
        include_moe: bool,
        ctx: OpContext,
        *,
        credential: ResolvedCredential | None = None,
    ) -> dict[str, dict[str, Any]]:
        groups: dict[tuple[str, ...], set[str]] = defaultdict(set)
        for geo in geos:
            if geography == "block_group":
                if geo.block_group_geoid:
                    groups[(geo.state, geo.county, geo.tract)].add(
                        geo.block_group_geoid
                    )
            else:
                groups[(geo.state, geo.county)].add(geo.tract_geoid)

        out: dict[str, dict[str, Any]] = {}
        variable_signature = tuple(
            list(CORE_VARIABLES.values())
            + (list(MOE_VARIABLES.values()) if include_moe else [])
        )
        for parent, wanted in groups.items():
            if _cancelled(ctx):
                break
            cache_key = (
                vintage,
                geography,
                "|".join(parent),
                variable_signature,
                include_moe,
            )
            records = self._acs_cache.get(cache_key)
            if records is None:
                records = await self._fetch_acs_group(
                    parent,
                    geography,
                    vintage,
                    include_moe,
                    ctx,
                    credential=credential,
                )
                _cache_put(self._acs_cache, cache_key, records, MAX_ACS_CACHE_ENTRIES)
            for geoid in wanted:
                if geoid in records:
                    out[geoid] = records[geoid]
        return out

    async def _fetch_acs_group(
        self,
        parent: tuple[str, ...],
        geography: str,
        vintage: str,
        include_moe: bool,
        ctx: OpContext,
        *,
        credential: ResolvedCredential | None = None,
    ) -> dict[str, dict[str, Any]]:
        variables = list(CORE_VARIABLES.values())
        if include_moe:
            variables += list(MOE_VARIABLES.values())
        params: dict[str, Any] = {"get": ",".join(["NAME", *variables])}
        if geography == "block_group":
            state, county, tract = parent
            params["for"] = "block group:*"
            params["in"] = f"state:{state} county:{county} tract:{tract}"
        else:
            state, county = parent
            params["for"] = "tract:*"
            params["in"] = f"state:{state} county:{county}"
        api_key = credential.value if credential is not None else None
        if api_key:
            params["key"] = api_key
        url = CENSUS_ACS_URL_TEMPLATE.format(vintage=vintage)
        self._record_request(ctx, credential.source if credential else "none")
        try:
            resp = await ctx.http.get(url, params=params)
        except httpx.HTTPError:
            # HTTP client errors may include the ACS URL and its credential.
            raise RuntimeError("Census ACS request failed before a response") from None
        if resp.status_code >= 300:
            raise RuntimeError(
                f"Census ACS request failed {resp.status_code}: {_response_detail(resp, credential.value if credential else None)}"
            )
        try:
            data = resp.json()
        except (json.JSONDecodeError, ValueError):
            raise RuntimeError(
                f"Census ACS request returned non-JSON: {_response_detail(resp, credential.value if credential else None)}"
            ) from None
        if not data:
            return {}
        headers = data[0]
        records: dict[str, dict[str, Any]] = {}
        for row in data[1:]:
            rec = dict(zip(headers, row))
            state = _clean_code(rec.get("state"), 2)
            county = _clean_code(rec.get("county"), 3)
            tract = _clean_code(rec.get("tract"), 6)
            if not state or not county or not tract:
                continue
            if geography == "block_group":
                bg = _clean_code(rec.get("block group"), 1)
                if not bg:
                    continue
                geoid = f"{state}{county}{tract}{bg}"
            else:
                geoid = f"{state}{county}{tract}"
            records[geoid] = rec
        return records

    def _format_row(
        self,
        geo: CensusGeo,
        record: dict[str, Any] | None,
        *,
        geography: str,
        vintage: str,
        include_moe: bool,
    ) -> CensusRecord:
        area_id = (
            geo.block_group_geoid if geography == "block_group" else geo.tract_geoid
        )
        data = CensusRecord().model_dump()
        data.update(
            {
                "demo_provider": "US Census ACS",
                "demo_dataset": "acs/acs5",
                "demo_vintage": vintage,
                "demo_area_level": geography,
                "demo_area_id": area_id,
                "us_census_state_fips": geo.state,
                "us_census_county_fips": geo.county,
                "us_census_tract_geoid": geo.tract_geoid,
                "us_census_block_group_geoid": geo.block_group_geoid,
            }
        )
        if not record:
            return CensusRecord.model_validate(data)
        vals = {name: _num(record.get(var)) for name, var in CORE_VARIABLES.items()}
        degree_counts = [
            vals.get(k) for k in ("bachelors", "masters", "professional", "doctorate")
        ]
        bachelors_plus = (
            None
            if all(v is None for v in degree_counts)
            else sum(v or 0 for v in degree_counts)
        )
        data.update(
            {
                "demo_population": vals["population"],
                "demo_median_age": vals["median_age"],
                "demo_median_household_income": vals["median_household_income"],
                "demo_poverty_rate": _pct(vals["poverty_count"], vals["poverty_total"]),
                "demo_bachelors_plus_rate": _pct(
                    bachelors_plus, vals["education_total"]
                ),
                "demo_white_non_hispanic_pct": _pct(
                    vals["white_non_hispanic"], vals["race_total"]
                ),
                "demo_black_pct": _pct(vals["black_non_hispanic"], vals["race_total"]),
                "demo_hispanic_pct": _pct(vals["hispanic"], vals["race_total"]),
                "demo_asian_pct": _pct(vals["asian_non_hispanic"], vals["race_total"]),
                "demo_owner_occupied_pct": _pct(
                    vals["owner_occupied"], vals["housing_total"]
                ),
            }
        )
        if include_moe:
            data.update(
                {
                    "demo_population_moe": _num(
                        record.get(MOE_VARIABLES["population"])
                    ),
                    "demo_median_age_moe": _num(
                        record.get(MOE_VARIABLES["median_age"])
                    ),
                    "demo_median_household_income_moe": _num(
                        record.get(MOE_VARIABLES["median_household_income"])
                    ),
                }
            )
        return CensusRecord.model_validate(data)
