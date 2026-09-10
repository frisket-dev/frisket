from __future__ import annotations

import pytest

from frisket.actions.types import ActionRequest, SheetRows
from frisket.actions.system import (
    action_id_from_request,
    root_action_catalog,
    typed_action_for_request,
)
from frisket.sdk import ActionParams, DynamicOutput, Rows, map_batch


def test_root_catalog_contains_each_legacy_and_typed_action_once() -> None:
    catalog = root_action_catalog()
    ids = [entry.kind for entry in catalog.actions]

    assert len(ids) == len(set(ids))
    assert ids.count("map.template") == 1
    assert ids.count("map.clean_dates") == 1


def test_root_catalog_projects_host_placement_for_typed_actions() -> None:
    modes = {
        entry.kind: entry.async_mode
        for entry in root_action_catalog().actions
        if entry.kind.startswith("map.")
    }

    assert {kind for kind, mode in modes.items() if mode == "queued"} >= {
        "map.regex_extract",
        "map.to_geo_point",
    }
    assert {
        kind: modes[kind]
        for kind in (
            "map.template",
            "map.columns_from_json",
            "map.clean_column",
            "map.clean_dates",
        )
    } == {
        "map.template": "sync",
        "map.columns_from_json": "sync",
        "map.clean_column": "sync",
        "map.clean_dates": "sync",
    }


def test_typed_request_routes_without_legacy_envelope() -> None:
    body = ActionRequest(
        action_id="map.template",
        scope=SheetRows(sheet_id=1),
        params={"template": {"text": "literal text"}},
        output_names={"rendered": "message"},
        idempotency_key="template@1",
    ).model_dump(mode="json")

    routed = typed_action_for_request(body)

    action, request = routed.action, routed.request
    assert action.action_id == "map.template"
    assert request.params == {"template": {"text": "literal text"}}
    assert action_id_from_request(body) == "map.template"


def test_legacy_envelope_has_no_native_action_identity() -> None:
    body = {"kind": "map.ask", "schema_version": "frisket.action.v2"}

    with pytest.raises(ValueError, match="requires action_id"):
        typed_action_for_request(body)
    assert action_id_from_request(body) is None


def test_public_sdk_exports_the_complete_first_wave_authoring_surface() -> None:
    assert ActionParams.__name__ == "ActionParams"
    assert DynamicOutput.__name__ == "DynamicOutput"
    assert Rows.__name__ == "Rows"
    assert map_batch.__name__ == "map_batch"
