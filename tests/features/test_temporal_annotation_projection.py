from __future__ import annotations

import pytest

from frisket.features.temporal_annotation_projection import (
    project_temporal_annotation,
    temporal_annotation_intersects,
)


SOURCE_TIMELINE = {
    "artifact_stable_id": "source_artifact:source-media",
    "fingerprint": "sha256:" + ("a" * 64),
    "duration_ms": 10_000,
}
DERIVED_TIMELINE = {
    "artifact_stable_id": "source_artifact:derived-clip",
    "fingerprint": "sha256:" + ("b" * 64),
    "duration_ms": 4_000,
}


def _value(schema_version: str, key: str, items: object) -> dict:
    return {
        "schema_version": schema_version,
        "timeline": SOURCE_TIMELINE,
        key: items,
    }


def test_single_point_rebases_and_retains_identity_label_metadata_and_source() -> None:
    source = _value(
        "frisket.timeline_point.v1",
        "item",
        {
            "id": "speaker-change",
            "at_ms": 5_000,
            "label": "New speaker",
            "metadata": {"speaker": "Jane", "manual": True},
        },
    )

    projection = project_temporal_annotation(
        "timeline_point",
        source,
        source_start_ms=3_000,
        source_end_ms=7_000,
        derived_timeline=DERIVED_TIMELINE,
    )

    assert projection is not None
    assert projection.value == {
        "schema_version": "frisket.timeline_point.v1",
        "timeline": DERIVED_TIMELINE,
        "item": {
            "id": "speaker-change",
            "label": "New speaker",
            "metadata": {"speaker": "Jane", "manual": True},
            "at_ms": 2_000,
        },
    }
    assert projection.source_timeline == SOURCE_TIMELINE
    assert projection.source_item_ids == ("speaker-change",)
    receipt = projection.receipt_metadata()
    assert receipt["source_timeline"] == SOURCE_TIMELINE
    assert receipt["derived_timeline"] == DERIVED_TIMELINE
    assert receipt["source_start_ms"] == 3_000
    assert receipt["source_end_ms"] == 7_000
    assert receipt["projected_value_hash"].startswith("sha256:")


def test_point_collection_uses_half_open_membership_and_omits_empty_projection() -> (
    None
):
    source = _value(
        "frisket.timeline_points.v1",
        "items",
        [
            {"id": "at-start", "at_ms": 3_000},
            {"id": "inside", "at_ms": 6_999},
            {"id": "at-end", "at_ms": 7_000},
            {"id": "before", "at_ms": 2_999},
        ],
    )

    projection = project_temporal_annotation(
        "timeline_points",
        source,
        source_start_ms=3_000,
        source_end_ms=7_000,
        derived_timeline=DERIVED_TIMELINE,
    )
    assert projection is not None
    assert [(item["id"], item["at_ms"]) for item in projection.value["items"]] == [
        ("at-start", 0),
        ("inside", 3_999),
    ]

    assert (
        project_temporal_annotation(
            "timeline_points",
            _value(
                "frisket.timeline_points.v1",
                "items",
                [{"id": "outside", "at_ms": 8_000}],
            ),
            source_start_ms=3_000,
            source_end_ms=7_000,
            derived_timeline=DERIVED_TIMELINE,
        )
        is None
    )


def test_single_range_intersects_clips_and_rebases() -> None:
    projection = project_temporal_annotation(
        "timeline_range",
        _value(
            "frisket.timeline_range.v1",
            "item",
            {
                "id": "claim",
                "start_ms": 2_000,
                "end_ms": 5_000,
                "label": "Claim",
                "metadata": {"reviewed": False},
            },
        ),
        source_start_ms=3_000,
        source_end_ms=7_000,
        derived_timeline=DERIVED_TIMELINE,
    )

    assert projection is not None
    assert projection.value["item"] == {
        "id": "claim",
        "label": "Claim",
        "metadata": {"reviewed": False},
        "start_ms": 0,
        "end_ms": 2_000,
    }
    assert (
        project_temporal_annotation(
            "timeline_range",
            _value(
                "frisket.timeline_range.v1",
                "item",
                {"id": "outside", "start_ms": 7_000, "end_ms": 8_000},
            ),
            source_start_ms=3_000,
            source_end_ms=7_000,
            derived_timeline=DERIVED_TIMELINE,
        )
        is None
    )


def test_range_collection_preserves_source_order_duplicates_labels_and_metadata() -> (
    None
):
    source_items = [
        {
            "id": "early",
            "start_ms": 0,
            "end_ms": 3_500,
            "label": "Early",
            "metadata": {"rank": 1},
        },
        {
            "id": "late",
            "start_ms": 6_500,
            "end_ms": 9_000,
            "label": "Late",
            "metadata": {"rank": 2},
        },
        {
            "id": "middle",
            "start_ms": 4_000,
            "end_ms": 6_000,
            "label": "Middle",
            "metadata": {"rank": 3, "nested": {"kept": True}},
        },
        {
            "id": "outside",
            "start_ms": 7_000,
            "end_ms": 8_000,
            "metadata": {"rank": 4},
        },
    ]
    projection = project_temporal_annotation(
        "timeline_ranges",
        _value("frisket.timeline_ranges.v1", "items", source_items),
        source_start_ms=3_000,
        source_end_ms=7_000,
        derived_timeline=DERIVED_TIMELINE,
    )

    assert projection is not None
    assert [item["id"] for item in projection.value["items"]] == [
        "early",
        "late",
        "middle",
    ]
    assert [
        (item["start_ms"], item["end_ms"]) for item in projection.value["items"]
    ] == [(0, 500), (3_500, 4_000), (1_000, 3_000)]
    assert projection.value["items"][2]["label"] == "Middle"
    assert projection.value["items"][2]["metadata"] == {
        "rank": 3,
        "nested": {"kept": True},
    }
    assert projection.source_item_ids == ("early", "late", "middle")


def test_projection_rejects_non_temporal_values_and_mismatched_derived_clock() -> None:
    with pytest.raises(ValueError, match="unknown temporal column type"):
        project_temporal_annotation(
            "json",
            {"summary": "never inherited"},
            source_start_ms=3_000,
            source_end_ms=7_000,
            derived_timeline=DERIVED_TIMELINE,
        )

    with pytest.raises(ValueError, match="duration must equal"):
        project_temporal_annotation(
            "timeline_point",
            _value(
                "frisket.timeline_point.v1",
                "item",
                {"id": "point", "at_ms": 5_000},
            ),
            source_start_ms=3_000,
            source_end_ms=7_000,
            derived_timeline={**DERIVED_TIMELINE, "duration_ms": 3_999},
        )


def test_translated_window_can_partly_overlap_a_shorter_compatible_timeline() -> None:
    source = _value(
        "frisket.timeline_range.v1",
        "item",
        {"id": "child-local", "start_ms": 0, "end_ms": 500},
    )
    source["timeline"] = {**SOURCE_TIMELINE, "duration_ms": 2_000}

    assert temporal_annotation_intersects(
        "timeline_range",
        source,
        source_start_ms=-1_000,
        source_end_ms=1_000,
    )
    projection = project_temporal_annotation(
        "timeline_range",
        source,
        source_start_ms=-1_000,
        source_end_ms=1_000,
        derived_timeline={**DERIVED_TIMELINE, "duration_ms": 2_000},
    )
    assert projection is not None
    assert (
        projection.value["item"]["start_ms"],
        projection.value["item"]["end_ms"],
    ) == (
        1_000,
        1_500,
    )
