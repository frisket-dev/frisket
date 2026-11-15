from __future__ import annotations

import pytest

from frisket.actions.types import ActionRequest, SheetRows
from frisket.actions.system import (
    action_id_from_request,
    root_action_catalog,
    typed_action_for_request,
)
from frisket.sdk import ActionParams, DynamicOutput, Rows, map_batch


@pytest.fixture(autouse=True)
def isolated_catalog_cache():
    from frisket.actions.system import (
        _catalog_envelope_schemas_json,
        _declared_catalog_entry_json,
    )

    _catalog_envelope_schemas_json.cache_clear()
    _declared_catalog_entry_json.cache_clear()
    yield
    _catalog_envelope_schemas_json.cache_clear()
    _declared_catalog_entry_json.cache_clear()


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


@pytest.mark.parametrize(
    "action_kinds", [frozenset({"media.to_markdown"}), frozenset()]
)
def test_filtered_root_catalog_builds_only_requested_entries(
    monkeypatch, action_kinds
) -> None:
    from frisket.actions.core import RegisteredAction

    full = root_action_catalog().model_dump(mode="json")
    from frisket.actions.system import _declared_catalog_entry_json

    _declared_catalog_entry_json.cache_clear()
    built = []
    original = RegisteredAction.catalog_entry

    def record(self):
        built.append(self.action_id)
        return original(self)

    monkeypatch.setattr(RegisteredAction, "catalog_entry", record)
    filtered = root_action_catalog(action_kinds=action_kinds).model_dump(mode="json")
    expected = {
        **full,
        "actions": [
            entry for entry in full["actions"] if entry["kind"] in action_kinds
        ],
    }
    assert filtered == expected
    assert set(built) == action_kinds
    assert len(built) == len(action_kinds)


def test_launcher_catalog_filters_before_building_action_schemas(monkeypatch) -> None:
    from frisket.actions.core import RegisteredAction
    from frisket.server.action_catalog_hints import (
        action_catalog_payload_with_launcher_hints,
    )

    full = action_catalog_payload_with_launcher_hints(
        {"configured": False, "engines": []}
    )
    from frisket.actions.system import _declared_catalog_entry_json

    _declared_catalog_entry_json.cache_clear()
    built = []
    original = RegisteredAction.catalog_entry

    def record(self):
        built.append(self.action_id)
        return original(self)

    monkeypatch.setattr(RegisteredAction, "catalog_entry", record)
    filtered = action_catalog_payload_with_launcher_hints(
        {"configured": False, "engines": []},
        action_kinds=frozenset({"media.to_markdown"}),
    )
    assert filtered == {
        **full,
        "actions": [
            entry for entry in full["actions"] if entry["kind"] == "media.to_markdown"
        ],
    }
    assert built == ["media.to_markdown"]


def test_catalog_reuses_declared_schemas_without_sharing_mutable_results(
    monkeypatch,
) -> None:
    from copy import deepcopy
    from frisket.actions.core import RegisteredAction
    from frisket.actions.system import _catalog_envelope_schemas_json

    built = []
    original = RegisteredAction.catalog_entry

    def record(self):
        built.append(self.action_id)
        return original(self)

    monkeypatch.setattr(RegisteredAction, "catalog_entry", record)
    first = root_action_catalog(action_kinds=frozenset({"media.to_markdown"}))
    expected = deepcopy(first.model_dump(mode="json"))
    first.actions[0].input_schema["properties"].clear()
    first.actions[0].ui_hints["request_only"] = True
    first.action_schema.clear()
    second = root_action_catalog(action_kinds=frozenset({"media.to_markdown"}))
    assert second.model_dump(mode="json") == expected
    assert built == ["media.to_markdown"]
    assert _catalog_envelope_schemas_json.cache_info().misses == 1
    assert _catalog_envelope_schemas_json.cache_info().hits == 1


def test_launcher_enrichment_is_fresh_and_never_enters_declaration_cache():
    from frisket.server.action_catalog_hints import (
        action_catalog_payload_with_launcher_hints,
    )

    action_kinds = frozenset({"media.to_markdown"})
    before = action_catalog_payload_with_launcher_hints(
        {"configured": False, "engines": []}, action_kinds=action_kinds
    )
    after = action_catalog_payload_with_launcher_hints(
        {
            "configured": True,
            "engines": [
                {
                    "route": "/to-markdown",
                    "name": "docling",
                    "available": True,
                    "models": ["fresh-model"],
                }
            ],
        },
        action_kinds=action_kinds,
    )

    def docling(payload):
        return next(
            engine
            for engine in payload["actions"][0]["ui_hints"]["engines"]
            if engine["id"] == "docling"
        )

    assert docling(before)["available"] is False
    assert docling(after)["available"] is True
    assert docling(after)["models"] == ["fresh-model"]
    declaration = root_action_catalog(action_kinds=action_kinds).actions[0]
    assert "engines" not in declaration.ui_hints
