from __future__ import annotations

from typing import Mapping

import pytest
from pydantic import BaseModel

from frisket.sdk import (
    ActionParams,
    CensusDemographics,
    EngineRef,
    Geocoder,
    Row,
    RowError,
    RowResult,
    Rows,
    map_batch,
    map_rows,
)


class Params(ActionParams):
    selected: EngineRef[Geocoder] = EngineRef[Geocoder]("auto")


class Output(BaseModel):
    value: str


class BatchParams(ActionParams):
    expanded: bool = False


def lookup(params: Params, row: Row, geocoder: Geocoder) -> RowResult[Output]: ...


def batch(
    params: BatchParams, rows: Rows, census: CensusDemographics
) -> Mapping[int, RowResult[Output] | RowError]: ...


def test_engine_reference_associates_by_type_not_field_name():
    terminal = map_rows(lookup)
    assert terminal.engine_param == "selected"
    assert terminal.capabilities == (Geocoder,)
    assert Params.model_validate({"selected": "nominatim"}).selected.root == "nominatim"


def test_batch_supports_capability_active_outputs_and_per_row_errors():
    terminal = map_batch(batch, active_outputs=lambda params: ("value",))
    assert terminal.engine_param is None
    assert terminal.output_model is Output
    assert [field.key for field in terminal.resolve_output_fields(BatchParams())] == [
        "value"
    ]
    with pytest.raises(TypeError, match="exactly"):
        map_batch(batch, active_outputs=lambda params, extra: ())


def test_wrong_engine_capability_and_multiple_routed_capabilities_are_rejected():
    def wrong(
        params: Params, row: Row, census: CensusDemographics
    ) -> RowResult[Output]: ...

    def multiple(
        params: Params, row: Row, geocoder: Geocoder, census: CensusDemographics
    ) -> RowResult[Output]: ...

    with pytest.raises(TypeError, match="requires map_batch"):
        map_rows(wrong)
    with pytest.raises(TypeError, match="requires map_batch"):
        map_rows(multiple)


def test_required_credentials_declare_the_runtime_refusal_once():
    from frisket.actions.registry import ACTION_REGISTRY
    from frisket.server.action_enqueue import MISSING_ACTION_CREDENTIAL_ERROR_CODE

    census = ACTION_REGISTRY.get("enrich.census_demographics").catalog_entry()
    assert census["required_credentials"] == ["CENSUS_API_KEY"]
    assert [error["code"] for error in census["errors"]].count(
        MISSING_ACTION_CREDENTIAL_ERROR_CODE
    ) == 1
    geocode = ACTION_REGISTRY.get("enrich.geocode").catalog_entry()
    assert geocode["required_credentials"] == []
    assert MISSING_ACTION_CREDENTIAL_ERROR_CODE not in {
        error["code"] for error in geocode["errors"]
    }
