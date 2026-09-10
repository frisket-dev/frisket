from __future__ import annotations

import pytest
from pydantic import ValidationError

from frisket.actions.extract import RegexExtractParams
from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.types import ActionRequest, discover_references


def test_regex_descriptor_matches_multi_column_runtime() -> None:
    entry = ACTION_REGISTRY.get("map.regex_extract").catalog_entry()
    (requirement,) = entry["ui_hints"]["source_requirements"]

    params = RegexExtractParams(input_columns=["title", "body"], pattern="frisket")
    assert [column.name for column in params.input_columns] == ["title", "body"]
    assert (requirement["mode"], requirement["param"], requirement["min"]) == (
        "columns",
        "input_columns",
        1,
    )
    assert requirement["accepted_column_types"] == [
        "text",
        "timestamped_transcript",
        "category",
    ]


def test_fetch_url_descriptor_declares_one_link_or_text_column() -> None:
    action = ACTION_REGISTRY.get("media.fetch_url")
    request = ActionRequest(
        action_id=action.action_id,
        scope={"kind": "sheet_rows", "sheet_id": 1},
        params={"source": "url"},
        idempotency_key="fetch-source",
    )
    bound = BoundTypedActionRequest.bind(action, request)
    (source,) = discover_references(bound.params)
    assert source.column == "url"
    assert source.accepted_column_types == ("link", "text")
    with pytest.raises((ValidationError, ValueError)):
        BoundTypedActionRequest.bind(
            action,
            request.model_copy(
                update={"params": {"source": ["primary_url", "fallback_url"]}}
            ),
        )
    (requirement,) = action.catalog_entry()["ui_hints"]["source_requirements"]
    assert (
        requirement["mode"],
        requirement["param"],
        requirement["min"],
    ) == ("column", "source", 1)
    assert requirement["accepted_column_types"] == ["link", "text"]
