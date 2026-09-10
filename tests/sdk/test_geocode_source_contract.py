"""Typed Geocode source discovery is shared by admission and execution."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from frisket.actions.geocode import GeocodeParams
from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.types import ActionRequest
from frisket.engine.executor.map_rows_action import build_typed_map_rows_plan
from frisket.engine.store import Project


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"source": ["street"]},
        {"source": {"text": "{{street}}", "input_columns": ["street"]}},
        {"source": "street", "input_template": "{{city}}"},
        {"source": {"text": "{{street}}"}, "source_column": "city"},
        {"source": {"text": "{{street}}"}, "input_columns": ["street"]},
    ],
)
def test_source_union_rejects_missing_or_parallel_branch_authority(params):
    with pytest.raises(ValidationError):
        GeocodeParams.model_validate(params)


@pytest.fixture
def geocode_sheet(tmp_path):
    project = Project.create(tmp_path / "geocode-source.frisket")
    sheet_id = project.add_sheet("Places")
    street_id = project.add_column(sheet_id, "street", type="category")
    number_id = project.add_column(sheet_id, "house_number", type="number")
    project.add_rows(
        sheet_id,
        [{"street": "Main St", "house_number": 10}],
        {"street": street_id, "house_number": number_id},
    )
    try:
        yield project, sheet_id, street_id, number_id
    finally:
        project.close()


def _plan(project, sheet_id, source):
    bound = BoundTypedActionRequest.bind(
        ACTION_REGISTRY.get("enrich.geocode"),
        ActionRequest.model_validate(
            {
                "action_id": "enrich.geocode",
                "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
                "params": {"source": source},
                "idempotency_key": "source-contract",
            }
        ),
    )
    return build_typed_map_rows_plan(project, bound)


def test_direct_textlike_reference_admits_category(geocode_sheet):
    project, sheet_id, street_id, _ = geocode_sheet
    plan = _plan(project, sheet_id, "street")
    assert plan.source_columns == ("street",)
    assert dict(plan.source_column_ids) == {"street": street_id}
    assert dict(plan.source_column_types) == {"street": "category"}


def test_template_reference_order_is_shared_by_admission_and_runner(geocode_sheet):
    project, sheet_id, street_id, number_id = geocode_sheet
    text = "{{house_number}} {{street}} / {{house_number}}"
    plan = _plan(project, sheet_id, {"text": text})
    assert plan.source_columns == ("house_number", "street")
    assert dict(plan.source_column_ids) == {
        "house_number": number_id,
        "street": street_id,
    }
    assert dict(plan.source_column_types) == {
        "house_number": "number",
        "street": "category",
    }
    assert plan.spec_dict()["input_columns"] == ["house_number", "street"]
    assert plan.spec_dict()["params"]["source"] == {"text": text}
