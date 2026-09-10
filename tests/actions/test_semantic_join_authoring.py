from typing import Any

import pytest

from frisket.actions.core import SemanticJoin, map_rows, semantic_join
from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.semantic_join import SemanticJoinParams, match_one
from frisket.actions.semantic_join_types import SemanticJoinMatch, SemanticMatcher
from frisket.actions.types import ActionParams, ActionRequest, ColumnRef, Row, RowResult
from frisket.contracts.action import ActionCatalogEntry


def request(**changes):
    return ActionRequest.model_validate(
        {
            "action_id": "join.semantic",
            "scope": {"kind": "sheet_rows", "sheet_id": 1, "row_ids": [1, 3]},
            "params": {
                "source": "description",
                "target": {"sheet_id": 2, "column": "category"},
                "carry": ["id"],
            },
            "sheet_name": "Matches",
            "idempotency_key": "join-test",
            **changes,
        }
    )


def test_semantic_join_has_one_typed_declaration_and_cost_contract():
    registered = ACTION_REGISTRY.get("join.semantic")
    assert isinstance(registered.definition.run, SemanticJoin)
    params, outputs = registered.bind_request(request())
    assert isinstance(params, SemanticJoinParams)
    assert [field.key for field in outputs] == [
        "match_value",
        "match_score",
        "matched_row_id",
    ]
    entry = ActionCatalogEntry.model_validate(registered.catalog_entry())
    assert entry.required_capabilities == ["project:write", "model:embed"]
    assert entry.cost_policy.kind == "model_metered"
    assert entry.cost_policy.requires_confirmation
    assert entry.ui_hints["typed_action"]["creates_sheet"]
    assert entry.ui_hints["dynamic_outputs"]


def test_child_names_do_not_become_source_outputs():
    registered = ACTION_REGISTRY.get("join.semantic")
    _, outputs = registered.bind_request(
        request(
            output_names={
                "source": "Description",
                "carry.id": "Record ID",
                "match_value": "Category",
            }
        )
    )
    assert len(outputs) == 3
    for names in ({"carry.missing": "No"}, {"source": "match_value"}):
        with pytest.raises(ValueError):
            registered.bind_request(request(output_names=names))
    with pytest.raises(ValueError, match="sheet_name"):
        registered.bind_request(request(sheet_name=None))


def test_semantic_matcher_cannot_enter_ordinary_row_publication():
    with pytest.raises(TypeError, match="capability"):
        map_rows(match_one)


class RenamedParams(ActionParams):
    input: ColumnRef[Any]
    extra: list[ColumnRef[Any]]


async def custom(
    params: RenamedParams, row: Row, matcher: SemanticMatcher
) -> RowResult[SemanticJoinMatch]:
    raise AssertionError("declaration must not execute")


def test_custom_semantic_handler_uses_types_not_builtin_param_names():
    terminal = semantic_join(custom)
    assert terminal.child_output_keys(RenamedParams(input="text", extra=["id"])) == (
        "source",
        "carry.id",
    )
    with pytest.raises(ValueError, match="distinct"):
        terminal.child_output_keys(RenamedParams(input="text", extra=["id", "id"]))
