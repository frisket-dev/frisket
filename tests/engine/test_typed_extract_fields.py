from __future__ import annotations

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from frisket.actions.extract import ExtractParams
from frisket.actions.extraction_types import ExtractField, extraction_output_fields
from frisket.ops.extraction import extract_response_schema, normalize_extracted_value


def test_typed_prompt_and_completion_share_fields_but_unwrap_grounding():
    from frisket.actions.extract import complete_extract, extract
    from frisket.actions.types import DynamicOutput, Row

    params = ExtractParams.model_validate(
        {
            "source": ["document"],
            "model": "anthropic/claude-haiku-4-5",
            "fields": [
                {"name": "events", "type": "list"},
                {"name": "amount", "type": "number", "required": True},
            ],
            "include_confidence": True,
            "grounding": {"enabled": True},
        }
    )
    row = Row(values={"document": "A launch occurred."})
    prompt = extract(params, row)
    reply = {
        "events": {
            "value": ["launch", "landing"],
            "evidence": [[{"quote": "launch", "source_id": 42}], []],
        },
        "amount": {"value": None},
        "events_confidence": 0.9,
    }
    assert Draft202012Validator(prompt.response_schema).is_valid(reply)
    result = complete_extract(params, row, DynamicOutput(root=reply)).output.root
    assert result["events"].value == ["launch", "landing"]
    assert result["events"].evidence[0].item_index == 0
    assert result["events"].evidence[0].quote == "launch"
    assert not hasattr(result["events"].evidence[0], "source_id")
    assert result["amount"].value is None
    assert result["events_confidence"].value == 0.9


def test_claims_keep_supported_coordinate_spaces_and_tolerate_bad_independent_hints():
    from frisket.actions.extract import _evidence_claim

    claim = _evidence_claim(
        {
            "page": "3",
            "segment_indices": ["2", 3.0, "bad"],
            "bbox": {
                "space": "pixel",
                "x0": 20,
                "y0": 30,
                "x1": 40,
                "y1": 50,
                "width": 100,
                "height": 100,
            },
            "grounding_method": "some_existing_method",
        }
    )
    assert claim.segment_indices == (2, 3)
    assert claim.bbox.page_width == 100
    assert claim.bbox.space == "pixel"
    assert _evidence_claim({"quote": "hello", "bbox": {"bad": "data"}}).quote == "hello"


def test_field_authority_preserves_nested_schema_and_visible_list_reference():
    fields = [
        ExtractField(
            name="events",
            type="list",
            items={
                "type": "object",
                "properties": {"year": {"type": "integer"}},
                "required": ["year"],
                "additionalProperties": False,
            },
        ),
        ExtractField(
            name="details",
            type="json",
            properties={
                "count": {"type": "integer", "minimum": 0},
            },
        ),
        ExtractField(name="when", type="date"),
    ]
    outputs = extraction_output_fields(fields, include_confidence=True)
    response = extract_response_schema(
        [{"name": key, "schema": dict(value.schema)} for key, value in outputs.items()],
        grounding_enabled=False,
    )
    assert response["properties"]["events"] == fields[0].value_schema()
    assert outputs["events"].named_result.schema_name == "events_list"
    assert outputs["events"].named_result.may_feed == ["derive.table_from_list"]
    assert not outputs["events"].hidden
    assert outputs["when"].column_type == "date"
    assert (
        outputs["when"]
        .annotation.model_validate({"status": "ok", "value": "Spring 2020"})
        .value
        == "Spring 2020"
    )
    assert Draft202012Validator(response).is_valid(
        {
            "events": [{"year": 2020}],
            "details": {"count": 0},
            "when": "Spring 2020",
            "events_confidence": 0.8,
        }
    )
    assert not Draft202012Validator(outputs["events"].schema).is_valid(
        [{"year": "2020"}]
    )
    assert not Draft202012Validator(outputs["events_confidence"].schema).is_valid(1.1)
    assert not Draft202012Validator(outputs["details"].schema).is_valid({})


@pytest.mark.parametrize(
    "kwargs",
    [
        {"name": "x", "type": "category", "labels": []},
        {"name": "x", "type": "category", "labels": ["yes", "yes"]},
        {"name": "x", "type": "text", "labels": ["yes"]},
        {"name": "x", "type": "list", "items": {"type": "unknown"}},
        {"name": "x", "type": "json", "properties": {"bad": {"minimum": "zero"}}},
        {"name": "x", "type": "text", "items": {"type": "string"}},
    ],
)
def test_invalid_field_schema_rejected_before_execution(kwargs):
    with pytest.raises(ValidationError):
        ExtractField.model_validate(kwargs)


def test_params_reuse_rich_source_and_reject_colliding_fields():
    params = ExtractParams.model_validate(
        {
            "source": ["document"],
            "model": "openai/gpt-4.1-mini",
            "instruction": "  List events  ",
            "fields": [{"name": " events ", "type": "list"}],
            "include_confidence": True,
        }
    )
    assert params.instruction == "List events"
    assert list(extraction_output_fields(params.fields, include_confidence=True)) == [
        "events",
        "events_confidence",
    ]
    with pytest.raises(ValidationError):
        ExtractParams.model_validate(
            {
                **params.model_dump(mode="json"),
                "fields": [
                    {"name": "events", "type": "text"},
                    {"name": "events", "type": "list"},
                ],
            }
        )


@pytest.mark.parametrize("value", [0, False, [], {}, "N/A", "null hypothesis"])
def test_absence_normalization_does_not_destroy_real_values(value):
    assert normalize_extracted_value("answer", value, {"type": "string"}) == value


def test_category_null_label_survives_while_whole_null_token_normalizes():
    field = ExtractField(name="answer", type="category", labels=["none", "present"])
    assert normalize_extracted_value(field.name, "none", field.value_schema()) == "none"
    assert normalize_extracted_value("answer", " NULL ", {"type": "string"}) is None


def test_schema_generation_does_not_mutate_declared_nested_fields():
    field = ExtractField(name="items", type="list", items={"type": "string"})
    schema = field.value_schema()
    schema["items"]["type"] = "integer"
    assert field.value_schema()["items"] == {"type": "string"}
