from __future__ import annotations

from typing import Any

import pytest

from frisket.actions.registry import ACTION_REGISTRY
from frisket.server import action_catalog_hints
from frisket.server.action_catalog_hints import (
    action_catalog_payload_with_launcher_hints,
)


def _catalog_actions() -> list[dict[str, Any]]:
    payload = action_catalog_payload_with_launcher_hints({})
    return payload["actions"]


def _entry(actions: list[dict[str, Any]], kind: str) -> dict[str, Any]:
    matches = [action for action in actions if action["kind"] == kind]
    assert len(matches) == 1, f"expected exactly one {kind} catalog entry"
    return matches[0]


def _source_requirements(entry: dict[str, Any]) -> list[dict[str, Any]]:
    return list((entry.get("ui_hints") or {}).get("source_requirements") or [])


def test_catalog_refuses_definition_launcher_hint_collisions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        action_catalog_hints,
        "project_action_catalog_launcher_hints",
        lambda *args, **kwargs: {
            "map.classify": {
                "form": "injected-collision",
                "semantic_controls": {"source": "injected_collision"},
            }
        },
    )

    with pytest.raises(ValueError) as exc_info:
        action_catalog_payload_with_launcher_hints({})

    message = str(exc_info.value)
    assert "map.classify" in message
    assert "['form', 'semantic_controls']" in message


def test_builtin_descriptors_match_the_served_catalog() -> None:
    expected: dict[str, list[dict[str, Any]]] = {}
    for registered in ACTION_REGISTRY.actions:
        assert registered.action_id not in expected
        expected[registered.action_id] = _source_requirements(
            registered.catalog_entry()
        )

    actions = _catalog_actions()
    actual = {
        action["kind"]: _source_requirements(action)
        for action in actions
        if "source_requirements" in (action.get("ui_hints") or {})
    }
    assert actual == expected


def test_video_frames_advertises_narrow_video_types() -> None:
    entry = _entry(_catalog_actions(), "media.video_frames")
    (req,) = _source_requirements(entry)
    assert req["accepted_column_types"] == ["video", "file"]
    assert (req["param"], req["mode"], req["min"]) == ("source", "column", 1)
    # The typed declaration stays narrow instead of admitting unrelated
    # scalar and blob types.
    assert len(req["accepted_column_types"]) < 10


def test_semantic_join_advertises_its_exact_one_source_cardinality() -> None:
    entry = _entry(_catalog_actions(), "join.semantic")
    source, carry = _source_requirements(entry)
    assert (source["param"], source["mode"], source["min"]) == ("source", "column", 1)
    assert (carry["param"], carry["mode"]) == ("carry", "columns")
    schema = entry["input_schema"]
    source_schema = schema["properties"]["source"]
    assert schema["$defs"][source_schema["$ref"].split("/")[-1]]["type"] == "string"
    assert "source" in schema["required"]
    assert "carry" not in schema["required"]
    # The secondary column is deliberately a whole-sheet reference, not a
    # source-sheet picker or an independently selectable secondary row scope.
    target = schema["$defs"][schema["properties"]["target"]["$ref"].split("/")[-1]]
    assert target["type"] == "object"
    assert set(target["required"]) == {"sheet_id", "column"}
    assert set(target["properties"]) == {"sheet_id", "column"}
    assert target["additionalProperties"] is False
    assert entry["row_scope_policy"]["kind"] == "sheet_rows"
    assert carry["min"] == 0


def test_ner_advertises_text_types_only() -> None:
    """map.ner reads prose, and one declaration feeds every surface.

    An earlier permissive fallback put ``size`` (integer) and ``media`` (a
    blob column) in the NER panel's zero-config "All text columns" default and
    in the post-import nudge that advertises the same set -- observed on a files
    sheet during the 2026-07-26 hand-use pass.
    """

    entry = _entry(_catalog_actions(), "map.ner")
    (req,) = _source_requirements(entry)
    assert req["param"] == "source"
    assert req["accepted_column_types"] == ["text", "timestamped_transcript"]
    # Blob cells are out: a media column's handle is not text to extract
    # entities from.
    assert req["accepted_cell_kinds"] == ["text", "template"]
    for banned in ("integer", "number", "boolean", "json", "image", "file"):
        assert banned not in req["accepted_column_types"]


def test_extract_faces_advertises_narrow_image_types() -> None:
    entry = _entry(_catalog_actions(), "media.extract_faces")
    (req,) = _source_requirements(entry)
    assert req["accepted_column_types"] == ["image", "file"]
    assert (req["param"], req["mode"], req["min"]) == ("source", "column", 1)
    assert len(req["accepted_column_types"]) < 9


def test_extract_pdf_tables_advertises_narrow_pdf_types() -> None:
    entry = _entry(_catalog_actions(), "media.extract_pdf_tables")
    (req,) = _source_requirements(entry)
    assert req["accepted_column_types"] == ["file"]
    assert (req["param"], req["mode"], req["min"]) == ("source", "column", 1)
    schema = entry["input_schema"]
    source = schema["properties"]["source"]
    assert schema["$defs"][source["$ref"].split("/")[-1]]["type"] == "string"
    assert "source" in schema["required"]


def test_enclosure_materialize_does_not_invent_an_authored_source_param() -> None:
    entry = _entry(_catalog_actions(), "media.enclosure_materialize")
    assert _source_requirements(entry) == []
    assert entry["row_scope_policy"]["kind"] == "sheet_rows"
    assert set(entry["input_schema"]["properties"]) == {"force"}


def test_run_backfill_accepts_any_ai_generated_column_type() -> None:
    entry = _entry(_catalog_actions(), "run.backfill")
    (req,) = _source_requirements(entry)
    assert "accepted_column_types" not in req
    assert req["param"] == "column"
