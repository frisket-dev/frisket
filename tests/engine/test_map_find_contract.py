from __future__ import annotations

import pytest

from frisket.actions.system import typed_action_for_request
from frisket.actions.types import discover_references


def _action() -> dict:
    return {
        "action_id": "map.find",
        "scope": {"kind": "sheet_rows", "sheet_id": 1, "row_ids": [4, 5]},
        "params": {
            "source": "transcript",
            "instruction": "Find every distinct instance where China is discussed.",
            "fields": [
                {
                    "name": "sentiment",
                    "type": "category",
                    "description": "Sentiment expressed in this occurrence.",
                    "labels": ["positive", "neutral", "negative"],
                }
            ],
            "model": "anthropic/claude-haiku-4-5",
        },
        "sheet_name": "Findings",
        "output_names": {"match": "Occurrence", "sentiment": "Tone"},
        "idempotency_key": "map-find-contract",
    }


def test_map_find_schema_and_catalog_contract() -> None:
    from frisket.actions.find_types import FindSourceColumn
    from frisket.actions.system import root_action_catalog

    bound = typed_action_for_request(_action())
    assert bound.params.source.name == "transcript"
    assert bound.params.instruction.startswith("Find every")
    assert [field.name for field in bound.params.fields] == ["sentiment"]
    assert tuple(ref.column for ref in discover_references(bound.params)) == (
        "transcript",
    )
    assert bound.request.scope.row_ids == (4, 5)
    assert bound.request.sheet_name == "Findings"
    assert bound.request.output_names == {"match": "Occurrence", "sentiment": "Tone"}
    catalog = root_action_catalog()
    entry = next(item for item in catalog.actions if item.kind == "map.find")
    assert set(entry.required_capabilities) == {"project:write", "model:complete"}
    assert entry.receipt_policy == "writes_receipt"
    assert entry.ui_hints["dynamic_outputs"] is True
    assert FindSourceColumn.accepted_column_types == (
        "text",
        "timestamped_transcript",
        "audio",
        "video",
        "image",
        "file",
    )
    schema = bound.params.model_json_schema()
    assert "source" in schema["properties"]
    assert "sheet_id" not in schema["properties"]
    assert "target_sheet_name" not in schema["properties"]


def test_map_find_rejects_caller_capability_array_and_duplicate_fields() -> None:
    caller_grant = _action()
    caller_grant["capabilities"] = ["project:write"]
    with pytest.raises(ValueError):
        typed_action_for_request(caller_grant)
    duplicate = _action()
    duplicate["params"]["fields"] = [
        {"name": "detail", "type": "text"},
        {"name": "detail", "type": "text"},
    ]
    with pytest.raises(ValueError):
        typed_action_for_request(duplicate)


@pytest.mark.parametrize(
    "fields",
    [
        [{"name": "match", "type": "text"}],
        [{"name": "detail", "type": "text", "required": True}],
    ],
)
def test_find_details_cannot_override_grounded_match_or_require_guessed_values(fields):
    request = _action()
    request["params"]["fields"] = fields
    with pytest.raises(ValueError):
        typed_action_for_request(request)
