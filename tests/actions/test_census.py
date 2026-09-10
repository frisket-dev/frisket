from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from frisket.actions.census import (
    CENSUS_DEMOGRAPHICS,
    CensusParams,
    census_outputs,
    demographics,
)
from frisket.actions.census_types import CensusRecord
from frisket.actions.types import GeoPoint, Row, RowError, RowResult, Rows


def test_params_preserve_semantic_source_and_public_options():
    params = CensusParams(source="point")
    assert params.geography == "tract"
    assert params.include_moe is False
    assert set(CensusParams.model_fields) == {
        "source",
        "geography",
        "include_moe",
    }
    for extra in (
        "acs_vintage",
        "geo_vintage",
        "benchmark",
        "source_column",
        "confirmed",
    ):
        with pytest.raises(ValidationError):
            CensusParams.model_validate({"source": "point", extra: "old"})
    with pytest.raises(ValidationError):
        CensusParams(source="point", geography="county")
    with pytest.raises(ValidationError):
        CensusParams(source="point", provider="other")
    assert CensusParams(source="point", include_moe="false").include_moe is False


def test_one_output_model_controls_nineteen_base_and_three_optional_fields():
    base = census_outputs(CensusParams(source="point"))
    all_fields = census_outputs(CensusParams(source="point", include_moe=True))
    assert len(base) == 19
    assert all_fields == set(CensusRecord.model_fields)
    assert all_fields - base == {
        "demo_population_moe",
        "demo_median_age_moe",
        "demo_median_household_income_moe",
    }
    assert CENSUS_DEMOGRAPHICS.run.output_model is CensusRecord


def test_handler_sends_one_scope_batch_and_preserves_independent_errors():
    calls = []

    class Census:
        async def lookup_many(self, points, *, geography, include_moe):
            calls.append((points, geography, include_moe))
            return {
                1: CensusRecord(demo_population=123),
                2: RowError("census_geography_missing", "no Census geography found"),
                3: CensusRecord(demo_population=123),
            }

    rows = Rows(
        {
            1: Row({"point": {"lat": 38.9, "lon": -77.03}}),
            2: Row({"point": GeoPoint(lat=0, lon=0)}),
            3: Row({"point": {"lat": 38.9, "lon": -77.03}}),
            4: Row({"point": None}),
        }
    )
    result = asyncio.run(
        demographics(
            CensusParams(source="point", geography="block_group", include_moe=True),
            rows,
            Census(),
        )
    )
    assert set(result) == set(rows)
    assert isinstance(result[1], RowResult)
    assert result[1].output.demo_population == 123
    assert result[2].code == "census_geography_missing"
    assert result[4].code == "invalid_geo_point"
    assert len(calls) == 1
    assert set(calls[0][0]) == {1, 2, 3}
    assert calls[0][1:] == ("block_group", True)


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "address",
        {"lat": "38", "lon": -77},
        {"lat": True, "lon": -77},
        {"lat": 0, "lon": False},
        {"lat": 91, "lon": 0},
        {"lat": 0, "lon": 181},
        {"lat": float("nan"), "lon": 0},
        {"lat": 0, "lon": float("inf")},
    ],
)
def test_invalid_cells_do_not_call_capability(value):
    class Census:
        async def lookup_many(self, *_args, **_kwargs):
            pytest.fail("invalid input must not cause egress")

    result = asyncio.run(
        demographics(
            CensusParams(source="point"), Rows({1: Row({"point": value})}), Census()
        )
    )
    assert result[1].code == "invalid_geo_point"


def test_handler_reads_only_explicit_source():
    class Census:
        async def lookup_many(self, *_args, **_kwargs):
            pytest.fail(
                "an unrelated point must not substitute for the selected source"
            )

    result = asyncio.run(
        demographics(
            CensusParams(source="selected"),
            Rows({1: Row({"selected": None, "other": {"lat": 0, "lon": 0}})}),
            Census(),
        )
    )
    assert result[1].code == "invalid_geo_point"


def test_empty_cells_remain_valid_geo_point_values():
    from frisket.authoring.column_types import validate_value

    assert validate_value("geo_point", "")
