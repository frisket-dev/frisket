from __future__ import annotations

from copy import deepcopy

import pytest

from frisket.actions.system import typed_action_for_request, validate_root_action
from frisket.actions.types import discover_references
from frisket.engine.executor.map_rows_action import typed_request_hash


def _request(
    kind, selection, *, source="video", row_ids=(8,), sheet_name=None, output_names=None
):
    scope = {"kind": "sheet_rows", "sheet_id": 1}
    if row_ids is not None:
        scope["row_ids"] = list(row_ids)
    body = {
        "action_id": kind,
        "scope": scope,
        "params": {"source": source, "selection": selection},
        "idempotency_key": "temporal-contract",
    }
    if kind.startswith("derive."):
        body["sheet_name"] = sheet_name or "Segments"
    if output_names is not None:
        body["output_names"] = output_names
    return body


def _validated_hash(body):
    bound = typed_action_for_request(body)
    return typed_request_hash(bound), bound.params.model_dump(mode="json")


@pytest.mark.parametrize(
    "kind",
    [
        "temporal.extract_range",
        "derive.temporal_segments",
        "derive.transcript_segments",
    ],
)
def test_column_selection_may_omit_row_ids_to_mean_all_visible_rows(kind):
    body = _request(
        kind, {"kind": "column", "column": "boundaries"}, source="source", row_ids=None
    )
    result = validate_root_action(body)
    assert result.ok, result.error
    bound = typed_action_for_request(body)
    assert bound.request.scope.row_ids is None
    assert {ref.column for ref in discover_references(bound.params)} == {
        "source",
        "boundaries",
    }
    assert set(type(bound.params).model_fields) == {"source", "selection"}


def test_column_selection_rejects_an_explicit_empty_row_list():
    result = validate_root_action(
        _request(
            "derive.temporal_segments",
            {"kind": "column", "column": "boundaries"},
            row_ids=[],
        )
    )
    assert not result.ok
    assert result.error.code == "invalid_action_request"
    assert "row_ids" in result.error.message


@pytest.mark.parametrize(
    "selection",
    [
        {"kind": "draft_range", "start_ms": 1000, "end_ms": 2000},
        {"kind": "draft_points", "items": [{"at_ms": 1000}]},
        {"kind": "draft_ranges", "items": [{"start_ms": 1000, "end_ms": 2000}]},
        {
            "kind": "typed_value",
            "value": {
                "schema_version": "frisket.timeline_point.v1",
                "timeline": {
                    "artifact_stable_id": "source_artifact:contract-fixture",
                    "fingerprint": "sha256:" + "a" * 64,
                    "duration_ms": 2000,
                },
                "item": {"id": "point", "at_ms": 1000},
            },
        },
    ],
)
def test_non_column_selection_rejects_omitted_row_ids(selection):
    result = validate_root_action(
        _request("derive.temporal_segments", selection, row_ids=None)
    )
    assert not result.ok
    assert result.error.code == "invalid_action_request"
    assert "explicit rows" in result.error.message


def test_extract_hash_uses_canonical_defaults_and_trimmed_selection():
    incidental = _request(
        "temporal.extract_range",
        {
            "kind": "draft_range",
            "start_ms": 1000,
            "end_ms": 2000,
            "id": "  quote-1  ",
            "label": "  opening quote  ",
        },
        source="video",
    )
    explicit = _request(
        "temporal.extract_range",
        {
            "kind": "draft_range",
            "start_ms": 1000,
            "end_ms": 2000,
            "id": "quote-1",
            "label": "opening quote",
            "repeat_for_rows": False,
        },
        output_names={"clip": "clip"},
    )
    incidental_hash, params = _validated_hash(incidental)
    assert incidental_hash == _validated_hash(explicit)[0]
    assert params["source"] == "video"
    assert params["selection"]["id"] == "quote-1"
    assert params["selection"]["label"] == "opening quote"
    assert params["selection"]["repeat_for_rows"] is False


@pytest.mark.parametrize("field", ["source", "selection"])
def test_typed_column_references_reject_incidental_whitespace(field):
    body = _request(
        "derive.temporal_segments", {"kind": "column", "column": "boundaries"}
    )
    if field == "source":
        body["params"]["source"] = "  video  "
    else:
        body["params"]["selection"]["column"] = "  boundaries  "
    result = validate_root_action(body)
    assert not result.ok
    assert "trimmed" in result.error.message


def _typed_selection():
    return {
        "kind": "typed_value",
        "value": {
            "schema_version": "frisket.timeline_range.v1",
            "timeline": {
                "artifact_stable_id": "source_artifact:contract-fixture",
                "fingerprint": "sha256:" + "a" * 64,
            },
            "item": {"id": "quote-1", "start_ms": 1000, "end_ms": 2000},
        },
    }


def test_typed_value_hash_expands_structural_temporal_defaults():
    omitted = _request("temporal.extract_range", _typed_selection())
    explicit = deepcopy(omitted)
    explicit["params"]["selection"]["value"]["timeline"]["duration_ms"] = None
    explicit["params"]["selection"]["value"]["item"].update(
        {"label": None, "metadata": {}}
    )
    omitted_hash, params = _validated_hash(omitted)
    assert omitted_hash == _validated_hash(explicit)[0]
    value = params["selection"]["value"]
    assert value["timeline"]["duration_ms"] is None
    assert value["item"]["label"] is None
    assert value["item"]["metadata"] == {}


def test_typed_value_rejects_obsolete_proposal_origin_fields():
    selection = _typed_selection()
    obsolete = {
        "origin_receipt_id": "receipt-obsolete",
        "draft_revision": 1,
        "draft_hash": "sha256:" + "b" * 64,
    }
    selection.update(obsolete)
    result = validate_root_action(_request("derive.temporal_segments", selection))
    assert not result.ok
    assert result.error.code == "invalid_action_request"
    assert all(field in result.error.message for field in obsolete)


def test_typed_value_rejects_structurally_inverted_range():
    selection = _typed_selection()
    selection["value"]["item"].update(start_ms=2000, end_ms=1000)
    result = validate_root_action(_request("temporal.extract_range", selection))
    assert not result.ok
    assert result.error.code == "invalid_action_request"
    assert "end_ms" in result.error.message


@pytest.mark.parametrize("output_name", ["source_range", "transcript"])
def test_split_names_conflict_only_with_actual_outputs_and_can_be_remapped(
    tmp_path, output_name
):
    from frisket.engine.store import Project
    from test_temporal_split import (
        _request as split_request,
        _run,
        _fake_stage,
    )
    from test_temporal_suite_e2e import _seed_project

    project = Project.create(tmp_path / "names.frisket")
    try:
        source_path = tmp_path / "source.mp4"
        source_path.write_bytes(b"test media; rendering is injected")
        source = _seed_project(project, source_path)
        seeded = {"sheet_id": source.sheet_id, "row_id": source.row_id}
        request = split_request(
            seeded,
            {"kind": "draft_points", "items": [{"at_ms": 1000}]},
            output_name=output_name,
        )
        # Names are not globally reserved; actual discovered sibling collisions
        # belong to the generic table host, not the Params schema.
        assert validate_root_action(request.model_dump(mode="json")).ok
        conflict = _run(project, request, stage_fn=_fake_stage, project_id="names")
        assert conflict.status == "failed"
        assert conflict.outputs == []
        remapped = request.model_copy(
            update={
                "output_names": {
                    "clip": output_name,
                    output_name: "original_" + output_name,
                },
                "idempotency_key": "remapped",
            }
        )
        result = _run(project, remapped, stage_fn=_fake_stage, project_id="names")
        assert result.status == "completed", result
        sheet = next(
            output.sheet_id for output in result.outputs if output.kind == "sheet"
        )
        names = {column["name"] for column in project.columns(sheet)}
        assert {output_name, "original_" + output_name} <= names
    finally:
        project.close()


@pytest.mark.parametrize(
    "schema_version,item_key,item",
    [
        ("frisket.timeline_point.v1", "item", {"id": "late-point", "at_ms": 2001}),
        (
            "frisket.timeline_ranges.v1",
            "items",
            [{"id": "late-range", "start_ms": 1000, "end_ms": 2001}],
        ),
    ],
)
def test_typed_value_rejects_embedded_duration_overflow(schema_version, item_key, item):
    selection = _typed_selection()
    selection["value"] = {
        "schema_version": schema_version,
        "timeline": {**selection["value"]["timeline"], "duration_ms": 2000},
        item_key: item,
    }
    result = validate_root_action(_request("derive.temporal_segments", selection))
    assert not result.ok
    assert result.error.code == "invalid_action_request"
    assert "range_out_of_bounds" in result.error.message


def test_extract_hash_distinguishes_literal_repeat_and_effective_content():
    base = _request(
        "temporal.extract_range",
        {"kind": "draft_range", "start_ms": 1000, "end_ms": 2000},
    )
    base_hash, _ = _validated_hash(base)
    repeated = deepcopy(base)
    repeated["params"]["selection"]["repeat_for_rows"] = True
    changed = deepcopy(base)
    changed["params"]["selection"]["end_ms"] = 2001
    assert _validated_hash(repeated)[0] != base_hash
    assert _validated_hash(changed)[0] != base_hash


def test_temporal_literal_batch_still_requires_true_confirmation():
    base = _request(
        "derive.temporal_segments",
        {"kind": "draft_points", "items": [{"at_ms": 1000}]},
        row_ids=[8, 3],
    )
    omitted = validate_root_action(base)
    false = deepcopy(base)
    false["params"]["selection"]["repeat_for_rows"] = False
    false_result = validate_root_action(false)
    assert not omitted.ok and not false_result.ok
    assert "literal_selection_requires_confirmation" in omitted.error.message
    assert false_result.error.message == omitted.error.message
    confirmed = deepcopy(base)
    confirmed["params"]["selection"]["repeat_for_rows"] = True
    result = validate_root_action(confirmed)
    assert result.ok
    assert typed_action_for_request(confirmed).params.selection.repeat_for_rows is True


def test_split_hash_canonicalizes_names_and_row_set_without_erasing_membership():
    incidental = _request(
        "derive.temporal_segments",
        {"kind": "column", "column": "boundaries"},
        source="video",
        row_ids=[8, 3],
        sheet_name="  Video segments  ",
    )
    explicit = _request(
        "derive.temporal_segments",
        {"kind": "column", "column": "boundaries"},
        row_ids=[8, 3],
        sheet_name="Video segments",
        output_names={"clip": "clip"},
    )
    hash_value, params = _validated_hash(incidental)
    assert hash_value == _validated_hash(explicit)[0]
    assert params["selection"]["column"] == "boundaries"
    assert typed_action_for_request(incidental).request.sheet_name == "Video segments"
    reordered = deepcopy(explicit)
    reordered["scope"]["row_ids"] = [3, 8]
    changed_rows = deepcopy(explicit)
    changed_rows["scope"]["row_ids"] = [4, 8]
    changed = deepcopy(explicit)
    changed["params"]["selection"]["column"] = "other_boundaries"
    # SheetRows is a canonical row set, while temporal item lists below retain
    # semantic order. Reordering the same row selection is not a new execution.
    assert _validated_hash(reordered)[0] == hash_value
    assert _validated_hash(changed_rows)[0] != hash_value
    assert _validated_hash(changed)[0] != hash_value


def test_split_hash_preserves_selection_item_order_after_nested_trimming():
    base = _request(
        "derive.temporal_segments",
        {
            "kind": "draft_points",
            "items": [
                {"at_ms": 1000, "id": "  first  ", "label": "  One  "},
                {"at_ms": 2000, "id": "  second  ", "label": "  Two  "},
            ],
        },
    )
    hash_value, params = _validated_hash(base)
    assert [item["id"] for item in params["selection"]["items"]] == ["first", "second"]
    reordered = deepcopy(base)
    reordered["params"]["selection"]["items"].reverse()
    assert _validated_hash(reordered)[0] != hash_value
