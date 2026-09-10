from __future__ import annotations

import pytest

from frisket.actions.core import ActionCategory, RegisteredAction, action, create_sheet
from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.semantic_match_types import SemanticMatchReader
from frisket.actions.transcript_types import TranscriptReader
from frisket.actions.types import ActionParams, ActionRequest, DynamicTableResult


@pytest.mark.parametrize(
    "action_id,scope_kind",
    [
        ("map.template", "sheet_rows"),
        ("run.backfill", "sheet_rows"),
        ("derive.transcript_segments", "sheet_rows"),
        ("import.csv", "project"),
        ("source.create", "project"),
        ("derive.link_table", "project"),
    ],
)
def test_scope_examples_catalog_and_binding_agree(action_id, scope_kind):
    registered = ACTION_REGISTRY.get(action_id)
    catalog = registered.catalog_entry()
    assert catalog["row_scope_policy"]["kind"] == scope_kind
    for example in catalog["examples"]:
        assert example["scope"]["kind"] == scope_kind
        request = ActionRequest.model_validate(example)
        registered.bind_request(request)
        wrong_scope = (
            {"kind": "project"}
            if scope_kind == "sheet_rows"
            else {"kind": "sheet_rows", "sheet_id": 1}
        )
        # Semantic-match tables allow selected secondary scope, but not an
        # implicit whole sheet. The other operations accept only one shape.
        with pytest.raises(ValueError):
            registered.bind_request(
                ActionRequest.model_validate({**example, "scope": wrong_scope})
            )


def _read_two(
    params: ActionParams, transcripts: TranscriptReader, matches: SemanticMatchReader
) -> DynamicTableResult:
    raise AssertionError("scope validation must not invoke either reader")


def test_second_reader_cannot_broaden_scope_or_drop_explicit_selection():
    registered = RegisteredAction(
        "custom.read_two",
        action(
            name="read_two",
            title="Read two",
            description="Use both admitted readers",
            category=ActionCategory.CONVERT,
            run=create_sheet(_read_two),
            examples=(ActionParams(),),
        ),
    )
    body = {
        "action_id": registered.action_id,
        "params": {},
        "sheet_name": "Combined",
        "idempotency_key": "two-readers",
    }
    assert registered.catalog_entry()["row_scope_policy"]["kind"] == "sheet_rows"
    [example] = registered.catalog_entry()["examples"]
    assert example["scope"] == {"kind": "sheet_rows", "sheet_id": 1, "row_ids": [1]}
    registered.bind_request(ActionRequest.model_validate(example))
    for rejected in ({"kind": "project"}, {"kind": "sheet_rows", "sheet_id": 1}):
        with pytest.raises(ValueError):
            registered.bind_request(
                ActionRequest.model_validate({**body, "scope": rejected})
            )
    registered.bind_request(
        ActionRequest.model_validate(
            {**body, "scope": {"kind": "sheet_rows", "sheet_id": 1, "row_ids": [2]}}
        )
    )
