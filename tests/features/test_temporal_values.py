from __future__ import annotations

import copy
import json
import math
import re

import pytest
from pydantic import ValidationError

from frisket.authoring import column_types
from frisket.features.temporal_values import (
    JAVASCRIPT_SAFE_INTEGER,
    TimelinePointValue,
    TimelinePointsValue,
    TimelineRangeValue,
    TimelineRangesValue,
    canonical_temporal_hash,
    canonical_temporal_json,
    is_valid_temporal_value,
    normalize_temporal_value,
    parse_temporal_value,
    temporal_model_for_type,
)


FINGERPRINT = "sha256:" + "a" * 64
ANCHOR = {
    "artifact_stable_id": "source_artifact:fixture-risk",
    "fingerprint": FINGERPRINT,
    "duration_ms": 10_000,
}


def _point_value(at_ms: int = 1_000) -> dict[str, object]:
    return {
        "schema_version": "frisket.timeline_point.v1",
        "timeline": dict(ANCHOR),
        "item": {"id": "point-1", "at_ms": at_ms},
    }


def _points_value(items: list[dict[str, object]]) -> dict[str, object]:
    return {
        "schema_version": "frisket.timeline_points.v1",
        "timeline": dict(ANCHOR),
        "items": items,
    }


def _range_value(start_ms: int = 1_000, end_ms: int = 2_000) -> dict[str, object]:
    return {
        "schema_version": "frisket.timeline_range.v1",
        "timeline": dict(ANCHOR),
        "item": {"id": "range-1", "start_ms": start_ms, "end_ms": end_ms},
    }


def _ranges_value(items: list[dict[str, object]]) -> dict[str, object]:
    return {
        "schema_version": "frisket.timeline_ranges.v1",
        "timeline": dict(ANCHOR),
        "items": items,
    }


@pytest.mark.parametrize(
    ("type_name", "model", "value"),
    [
        ("timeline_point", TimelinePointValue, _point_value()),
        (
            "timeline_points",
            TimelinePointsValue,
            _points_value([{"id": "p1", "at_ms": 1}]),
        ),
        ("timeline_range", TimelineRangeValue, _range_value()),
        (
            "timeline_ranges",
            TimelineRangesValue,
            _ranges_value([{"id": "r1", "start_ms": 1, "end_ms": 2}]),
        ),
    ],
)
def test_temporal_models_parse_exact_declared_schema(
    type_name: str,
    model: type,
    value: dict[str, object],
) -> None:
    parsed = parse_temporal_value(type_name, value)
    assert isinstance(parsed, model)
    assert temporal_model_for_type(type_name) is model
    assert (
        normalize_temporal_value(type_name, parsed)["schema_version"]
        == value["schema_version"]
    )


def test_four_temporal_column_types_are_core_and_share_timeline_renderer() -> None:
    expected = {
        "timeline_point": ("point", False, "Timestamp"),
        "timeline_points": ("point", True, "Timestamps"),
        "timeline_range": ("range", False, "Time range"),
        "timeline_ranges": ("range", True, "Time ranges"),
    }
    for type_name, (geometry, multiple, label) in expected.items():
        spec = column_types.get_column_type(type_name)
        assert spec is not None
        assert spec.core is True
        assert spec.plugin == "core"
        assert spec.presentation == {
            "renderer": "timeline",
            "geometry": geometry,
            "multiple": multiple,
            "label": label,
            "userSelectable": False,
        }
        assert spec.validate is not None
        assert spec.parse is not None


def test_registry_parser_normalizes_defaults_and_validator_is_strict() -> None:
    raw = _point_value()
    normalized = column_types.parse_value("timeline_point", raw)

    assert normalized["item"] == {
        "id": "point-1",
        "label": None,
        "metadata": {},
        "at_ms": 1_000,
    }
    assert column_types.validate_value("timeline_point", normalized)

    wrong_schema = {**raw, "schema_version": "frisket.timeline_points.v1"}
    assert not column_types.validate_value("timeline_point", wrong_schema)
    with pytest.raises(ValidationError):
        column_types.parse_value("timeline_point", wrong_schema)


def test_null_scalar_and_empty_collections_remain_distinct_valid_values() -> None:
    assert column_types.validate_value("timeline_point", None)
    assert column_types.parse_value("timeline_point", None) is None
    assert is_valid_temporal_value("timeline_points", _points_value([]))
    assert is_valid_temporal_value("timeline_ranges", _ranges_value([]))
    assert not is_valid_temporal_value(
        "timeline_point",
        {
            "schema_version": "frisket.timeline_point.v1",
            "timeline": dict(ANCHOR),
        },
    )


@pytest.mark.parametrize("at_ms", [0, 10_000])
def test_points_at_timeline_edges_are_valid(at_ms: int) -> None:
    assert is_valid_temporal_value("timeline_point", _point_value(at_ms))


@pytest.mark.parametrize("bad_coordinate", [-1, 10_001, JAVASCRIPT_SAFE_INTEGER + 1])
def test_point_coordinates_must_be_in_bounds(bad_coordinate: int) -> None:
    assert not is_valid_temporal_value("timeline_point", _point_value(bad_coordinate))


@pytest.mark.parametrize("bad_coordinate", [True, False, 1.0, 1.5, "1000"])
def test_millisecond_coordinates_are_strict_integers(bad_coordinate: object) -> None:
    value = _point_value()
    value["item"]["at_ms"] = bad_coordinate  # type: ignore[index]
    assert not is_valid_temporal_value("timeline_point", value)


@pytest.mark.parametrize("bad_duration", [0, -1, True, 10_000.0, "10000"])
def test_duration_is_optional_but_strictly_positive_when_present(
    bad_duration: object,
) -> None:
    value = _point_value()
    value["timeline"]["duration_ms"] = bad_duration  # type: ignore[index]
    assert not is_valid_temporal_value("timeline_point", value)

    without_duration = _point_value(JAVASCRIPT_SAFE_INTEGER)
    without_duration["timeline"].pop("duration_ms")  # type: ignore[union-attr]
    assert is_valid_temporal_value("timeline_point", without_duration)


@pytest.mark.parametrize(
    ("start_ms", "end_ms"),
    [(-1, 1), (1, 1), (2, 1), (1, 10_001)],
)
def test_ranges_are_nonempty_half_open_and_within_duration(
    start_ms: int,
    end_ms: int,
) -> None:
    assert not is_valid_temporal_value("timeline_range", _range_value(start_ms, end_ms))


def test_collection_keeps_order_overlap_and_duplicate_coordinates() -> None:
    value = _ranges_value(
        [
            {"id": "later", "start_ms": 4_000, "end_ms": 7_000},
            {"id": "earlier", "start_ms": 1_000, "end_ms": 5_000},
            {"id": "duplicate-coordinate", "start_ms": 4_000, "end_ms": 7_000},
        ]
    )

    parsed = parse_temporal_value("timeline_ranges", value)
    assert isinstance(parsed, TimelineRangesValue)
    assert [item.id for item in parsed.items] == [
        "later",
        "earlier",
        "duplicate-coordinate",
    ]


def test_collection_item_ids_must_be_unique_and_nonblank() -> None:
    duplicate = _points_value(
        [{"id": "same", "at_ms": 100}, {"id": "same", "at_ms": 200}]
    )
    blank = _points_value([{"id": "   ", "at_ms": 100}])

    assert not is_valid_temporal_value("timeline_points", duplicate)
    assert not is_valid_temporal_value("timeline_points", blank)


@pytest.mark.parametrize(
    "mutator",
    [
        lambda value: value.update({"unexpected": True}),
        lambda value: value["timeline"].update({"unexpected": True}),
        lambda value: value["item"].update({"unexpected": True}),
    ],
)
def test_unknown_fields_are_forbidden(mutator: object) -> None:
    value = _point_value()
    mutator(value)  # type: ignore[operator]
    assert not is_valid_temporal_value("timeline_point", value)


@pytest.mark.parametrize(
    "artifact_stable_id",
    [
        "",
        "artifact:abc",
        "source_artifact:",
        " source_artifact:abc",
        "source_artifact:a/b",
    ],
)
def test_artifact_stable_id_must_use_existing_opaque_id_shape(
    artifact_stable_id: str,
) -> None:
    value = _point_value()
    value["timeline"]["artifact_stable_id"] = artifact_stable_id  # type: ignore[index]
    assert not is_valid_temporal_value("timeline_point", value)


@pytest.mark.parametrize(
    "fingerprint",
    [
        "",
        "sha256:abc",
        "sha256:" + "A" * 64,
        "md5:" + "a" * 32,
        "sha256:" + "g" * 64,
    ],
)
def test_fingerprint_is_canonical_sha256(fingerprint: str) -> None:
    value = _point_value()
    value["timeline"]["fingerprint"] = fingerprint  # type: ignore[index]
    assert not is_valid_temporal_value("timeline_point", value)


@pytest.mark.parametrize("bad_number", [math.nan, math.inf, -math.inf])
def test_metadata_rejects_nonfinite_numbers(bad_number: float) -> None:
    value = _point_value()
    value["item"]["metadata"] = {"score": bad_number}  # type: ignore[index]
    assert not is_valid_temporal_value("timeline_point", value)


@pytest.mark.parametrize("bad_value", [{1, 2}, (1, 2), object()])
def test_metadata_rejects_python_only_values(bad_value: object) -> None:
    value = _point_value()
    value["item"]["metadata"] = {"bad": bad_value}  # type: ignore[index]
    assert not is_valid_temporal_value("timeline_point", value)


def test_metadata_rejects_cycles_and_excessive_depth() -> None:
    cycle: dict[str, object] = {}
    cycle["self"] = cycle
    cyclic_value = _point_value()
    cyclic_value["item"]["metadata"] = cycle  # type: ignore[index]
    assert not is_valid_temporal_value("timeline_point", cyclic_value)

    root: dict[str, object] = {}
    cursor = root
    for _ in range(66):
        child: dict[str, object] = {}
        cursor["child"] = child
        cursor = child
    deep_value = _point_value()
    deep_value["item"]["metadata"] = root  # type: ignore[index]
    assert not is_valid_temporal_value("timeline_point", deep_value)


def test_label_limit_is_enforced() -> None:
    long_label = _point_value()
    long_label["item"]["label"] = "x" * 4_097  # type: ignore[index]
    assert not is_valid_temporal_value("timeline_point", long_label)


def test_canonical_hash_normalizes_key_order_and_default_fields() -> None:
    left = _point_value()
    right = {
        "item": {
            "metadata": {},
            "at_ms": 1_000,
            "label": None,
            "id": "point-1",
        },
        "timeline": {
            "duration_ms": 10_000,
            "fingerprint": FINGERPRINT,
            "artifact_stable_id": "source_artifact:fixture-risk",
        },
        "schema_version": "frisket.timeline_point.v1",
    }

    assert canonical_temporal_hash("timeline_point", left) == canonical_temporal_hash(
        "timeline_point", right
    )
    canonical = canonical_temporal_json("timeline_point", left)
    assert canonical == json.dumps(
        json.loads(canonical),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    assert " " not in canonical
    assert re.fullmatch(
        r"sha256:[0-9a-f]{64}", canonical_temporal_hash("timeline_point", left)
    )


def test_canonical_hash_tracks_order_ids_labels_and_metadata() -> None:
    base = _points_value(
        [
            {"id": "p1", "at_ms": 100, "label": "one"},
            {"id": "p2", "at_ms": 200, "metadata": {"score": 0.8}},
        ]
    )
    reordered = copy.deepcopy(base)
    reordered["items"].reverse()  # type: ignore[union-attr]
    relabeled = copy.deepcopy(base)
    relabeled["items"][0]["label"] = "changed"  # type: ignore[index]
    metadata_changed = copy.deepcopy(base)
    metadata_changed["items"][1]["metadata"]["score"] = 0.7  # type: ignore[index]

    base_hash = canonical_temporal_hash("timeline_points", base)
    assert canonical_temporal_hash("timeline_points", reordered) != base_hash
    assert canonical_temporal_hash("timeline_points", relabeled) != base_hash
    assert canonical_temporal_hash("timeline_points", metadata_changed) != base_hash


def test_declared_type_cannot_be_inferred_or_silently_retyped() -> None:
    with pytest.raises(ValueError, match="unknown temporal column type"):
        temporal_model_for_type("json")
    assert not is_valid_temporal_value("timeline_ranges", _range_value())
