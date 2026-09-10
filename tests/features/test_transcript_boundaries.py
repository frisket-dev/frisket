from __future__ import annotations

from collections.abc import Iterable

import pytest

from frisket.features.temporal.transcript_boundaries import (
    LOCKING_POLICY_VERSION,
    TranscriptBoundaryError,
    lock_transcript_boundaries,
)
from frisket.features.temporal_values import canonical_temporal_hash
from frisket.features.topic_segmentation.contracts import (
    BetweenUnits,
    BoundaryCandidate,
    DialogueUnit,
    ExactTime,
    SegmentationSnapshot,
    WithinUnit,
)


def _timeline(duration_ms: int | None = 3_000) -> dict[str, object]:
    return {
        "artifact_stable_id": "source_artifact:transcript-video-1",
        "fingerprint": "sha256:" + "a" * 64,
        "duration_ms": duration_ms,
    }


def _snapshot(
    *units: DialogueUnit,
    source_kind: str = "timestamped_transcript",
) -> SegmentationSnapshot:
    return SegmentationSnapshot(
        snapshot_hash="sha256:" + "b" * 64,
        source_kind=source_kind,  # type: ignore[arg-type]
        language="en",
        units=tuple(units),
    )


def _unit(
    unit_id: str,
    ordinal: int,
    start_ms: int,
    end_ms: int,
) -> DialogueUnit:
    return DialogueUnit(
        id=unit_id,
        ordinal=ordinal,
        text=f"Unit {unit_id}",
        start_ms=start_ms,
        end_ms=end_ms,
    )


def _candidate(candidate_id: str, locator) -> BoundaryCandidate:
    return BoundaryCandidate(id=candidate_id, locator=locator)


def _coordinates(result) -> list[tuple[int, int]]:
    return [(item.start_ms, item.end_ms) for item in result.timeline_ranges.items]


def _lock(
    snapshot: SegmentationSnapshot,
    candidates: Iterable[BoundaryCandidate],
    *,
    duration_ms: int = 3_000,
):
    return lock_transcript_boundaries(
        snapshot,
        candidates,
        timeline=_timeline(duration_ms),
    )


def test_no_boundary_is_one_full_timeline_range() -> None:
    snapshot = _snapshot(_unit("a", 10, 200, 1_000))

    result = _lock(snapshot, [])

    assert result.policy_version == LOCKING_POLICY_VERSION
    assert result.candidate_locks == ()
    assert result.locked_boundaries == ()
    assert _coordinates(result) == [(0, 3_000)]
    assert result.timeline_ranges.items[0].id == "topic-section-0001"
    assert result.timeline_ranges.items[0].label is None


@pytest.mark.parametrize(
    ("left_end_ms", "right_start_ms"),
    [(1_000, 1_000), (1_000, 1_250)],
)
def test_clean_adjacent_boundary_locks_to_right_start_point(
    left_end_ms: int,
    right_start_ms: int,
) -> None:
    snapshot = _snapshot(
        _unit("a", 10, 0, left_end_ms),
        _unit("b", 20, right_start_ms, 2_000),
    )

    result = _lock(
        snapshot,
        [_candidate("gap", BetweenUnits("a", "b"))],
    )

    assert result.locked_boundaries[0].kind == "point"
    assert result.locked_boundaries[0].boundary_key == 20
    assert result.locked_boundaries[0].at_ms == right_start_ms
    assert _coordinates(result) == [
        (0, right_start_ms),
        (right_start_ms, 3_000),
    ]
    assert [item.id for item in result.timeline_ranges.items] == [
        "topic-section-0001",
        "topic-section-0002",
    ]
    assert all(item.label is None for item in result.timeline_ranges.items)


def test_adjacent_overlap_becomes_shared_span() -> None:
    snapshot = _snapshot(
        _unit("a", 10, 0, 1_300),
        _unit("b", 20, 1_000, 2_000),
    )

    result = _lock(
        snapshot,
        [_candidate("overlap", BetweenUnits("a", "b"))],
    )

    assert result.locked_boundaries[0].kind == "span"
    assert (
        result.locked_boundaries[0].start_ms,
        result.locked_boundaries[0].end_ms,
    ) == (0, 2_000)
    assert _coordinates(result) == [(0, 2_000), (0, 3_000)]


def test_within_unit_repeats_complete_chunk_in_adjacent_sections() -> None:
    snapshot = _snapshot(_unit("a", 10, 1_000, 2_000))

    result = _lock(snapshot, [_candidate("inside", WithinUnit("a"))])

    assert result.locked_boundaries[0].boundary_key == 10
    assert result.locked_boundaries[0].kind == "span"
    assert _coordinates(result) == [(0, 2_000), (1_000, 3_000)]


def test_chained_asr_overlap_is_selected_once_by_the_locking_layer() -> None:
    snapshot = _snapshot(
        _unit("c0", 0, 0, 1_300),
        _unit("c1", 1, 1_000, 2_300),
        _unit("c2", 2, 2_000, 3_300),
        _unit("c3", 3, 3_000, 4_300),
        _unit("c4", 4, 4_000, 5_300),
    )

    result = _lock(
        snapshot,
        [_candidate("inside-c2", WithinUnit("c2"))],
        duration_ms=6_300,
    )

    assert _coordinates(result) == [(0, 4_300), (1_000, 6_300)]
    assert result.section_unit_ids == (
        ("c0", "c1", "c2", "c3"),
        ("c1", "c2", "c3", "c4"),
    )
    assert result.timeline_ranges.items[0].metadata["requested_range"] == {
        "start_ms": 0,
        "end_ms": 3_300,
    }
    assert result.receipt_metadata()["section_unit_ids"] == {
        "topic-section-0001": ["c0", "c1", "c2", "c3"],
        "topic-section-0002": ["c1", "c2", "c3", "c4"],
    }


def test_within_only_full_duration_unit_produces_two_shared_full_ranges() -> None:
    snapshot = _snapshot(_unit("all", 7, 0, 3_000))

    result = _lock(snapshot, [_candidate("inside", WithinUnit("all"))])

    assert _coordinates(result) == [(0, 3_000), (0, 3_000)]


@pytest.mark.parametrize(
    "unit",
    [_unit("first", 10, 0, 1_000), _unit("last", 10, 2_000, 3_000)],
)
def test_edge_touching_boundary_preserves_atomic_chunk_sharing(
    unit: DialogueUnit,
) -> None:
    result = _lock(_snapshot(unit), [_candidate("inside", WithinUnit(unit.id))])

    expected = (
        [(0, 1_000), (0, 3_000)] if unit.id == "first" else [(0, 3_000), (2_000, 3_000)]
    )
    assert _coordinates(result) == expected


def test_same_key_span_wins_and_candidate_order_does_not_change_value() -> None:
    snapshot = _snapshot(
        _unit("a", 10, 0, 1_000),
        _unit("b", 20, 1_250, 2_000),
    )
    point = BoundaryCandidate(
        id="z-gap",
        locator=BetweenUnits("a", "b"),
        strength=0.2,
        label="weak gap",
        diagnostics={"engine_fact": 1},
    )
    span = BoundaryCandidate(
        id="a-within",
        locator=WithinUnit("b"),
        strength=999.0,
        label="unrelated label",
        diagnostics={"engine_fact": 2},
    )

    forward = _lock(snapshot, [point, span])
    reverse = _lock(snapshot, [span, point])

    assert forward.locked_boundaries == reverse.locked_boundaries
    assert forward.timeline_ranges == reverse.timeline_ranges
    boundary = forward.locked_boundaries[0]
    assert (boundary.kind, boundary.start_ms, boundary.end_ms) == (
        "span",
        1_250,
        2_000,
    )
    assert boundary.candidate_ids == ("a-within", "z-gap")
    assert _coordinates(forward) == [(0, 2_000), (1_250, 3_000)]


def test_same_key_overlapping_spans_merge_into_union() -> None:
    snapshot = _snapshot(
        _unit("a", 10, 0, 1_300),
        _unit("b", 20, 1_000, 2_000),
    )

    result = _lock(
        snapshot,
        [
            _candidate("between", BetweenUnits("a", "b")),
            _candidate("within", WithinUnit("b")),
        ],
    )

    boundary = result.locked_boundaries[0]
    assert (boundary.start_ms, boundary.end_ms) == (0, 2_000)
    assert _coordinates(result) == [(0, 2_000), (0, 3_000)]


@pytest.mark.parametrize(
    ("locator", "code"),
    [
        (BetweenUnits("missing", "b"), "unknown_unit"),
        (BetweenUnits("b", "a"), "non_adjacent_units"),
        (BetweenUnits("a", "c"), "non_adjacent_units"),
        (WithinUnit("missing"), "unknown_unit"),
    ],
)
def test_unit_locators_validate_snapshot_identity_and_order(locator, code: str) -> None:
    snapshot = _snapshot(
        _unit("a", 10, 0, 900),
        _unit("b", 30, 1_000, 1_900),
        _unit("c", 90, 2_000, 2_900),
    )

    with pytest.raises(TranscriptBoundaryError) as caught:
        _lock(snapshot, [_candidate("bad", locator)])

    assert caught.value.code == code
    assert caught.value.candidate_id == "bad"


def test_adjacency_uses_snapshot_position_not_dense_ordinals() -> None:
    snapshot = _snapshot(
        _unit("a", 10, 0, 1_000),
        _unit("b", 90, 1_000, 2_000),
    )

    result = _lock(snapshot, [_candidate("gap", BetweenUnits("a", "b"))])

    assert result.locked_boundaries[0].boundary_key == 90


def test_exact_time_uses_latest_containing_unit_in_overlap() -> None:
    snapshot = _snapshot(
        _unit("a", 10, 0, 1_500),
        _unit("b", 20, 1_000, 2_000),
        _unit("c", 30, 1_000, 2_500),
    )

    result = _lock(snapshot, [_candidate("exact", ExactTime(1_000))])

    assert result.locked_boundaries[0].boundary_key == 30
    assert result.locked_boundaries[0].at_ms == 1_000


def test_exact_time_in_silence_maps_to_first_following_unit() -> None:
    snapshot = _snapshot(
        _unit("a", 10, 100, 800),
        _unit("b", 20, 1_200, 2_000),
    )

    head = _lock(snapshot, [_candidate("head", ExactTime(50))])
    middle = _lock(snapshot, [_candidate("middle", ExactTime(1_000))])

    assert head.locked_boundaries[0].boundary_key == 10
    assert middle.locked_boundaries[0].boundary_key == 20


def test_exact_time_does_not_jump_back_to_an_older_long_overlap() -> None:
    snapshot = _snapshot(
        _unit("a", 10, 0, 500),
        _unit("b", 20, 100, 200),
        _unit("c", 30, 300, 400),
    )

    in_gap = _lock(
        snapshot,
        [_candidate("gap", ExactTime(250))],
        duration_ms=600,
    )

    assert in_gap.locked_boundaries[0].boundary_key == 30
    with pytest.raises(TranscriptBoundaryError) as caught:
        _lock(
            snapshot,
            [_candidate("tail", ExactTime(450))],
            duration_ms=600,
        )
    assert caught.value.code == "exact_time_without_following_unit"


@pytest.mark.parametrize("at_ms", [0, 3_000, 3_001])
def test_exact_time_must_be_strictly_inside_timeline(at_ms: int) -> None:
    snapshot = _snapshot(_unit("a", 10, 100, 2_900))

    with pytest.raises(TranscriptBoundaryError) as caught:
        _lock(snapshot, [_candidate("exact", ExactTime(at_ms))])

    assert caught.value.code == "exact_time_out_of_bounds"


def test_exact_time_in_tail_silence_without_following_unit_is_rejected() -> None:
    snapshot = _snapshot(_unit("a", 10, 100, 2_000))

    with pytest.raises(TranscriptBoundaryError) as caught:
        _lock(snapshot, [_candidate("tail", ExactTime(2_500))])

    assert caught.value.code == "exact_time_without_following_unit"


def test_same_key_points_coalesce_to_first_time_in_canonical_order() -> None:
    snapshot = _snapshot(_unit("a", 10, 100, 2_900))

    result = _lock(
        snapshot,
        [
            _candidate("second", ExactTime(1_100)),
            _candidate("first", ExactTime(1_000)),
        ],
    )

    boundary = result.locked_boundaries[0]
    assert boundary.at_ms == 1_000
    assert boundary.candidate_ids == ("first", "second")
    assert _coordinates(result) == [(0, 1_000), (1_000, 3_000)]
    assert result.section_unit_ids == (("a",), ("a",))


def test_exact_gap_and_clean_between_observations_coalesce_by_boundary_key() -> None:
    snapshot = _snapshot(
        _unit("a", 10, 0, 1_000),
        _unit("b", 20, 1_200, 2_000),
    )

    result = _lock(
        snapshot,
        [
            _candidate("between", BetweenUnits("a", "b")),
            _candidate("exact", ExactTime(1_100)),
        ],
    )

    boundary = result.locked_boundaries[0]
    assert (boundary.boundary_key, boundary.at_ms) == (20, 1_100)
    assert _coordinates(result) == [(0, 1_100), (1_100, 3_000)]


def test_span_wins_over_exact_point_at_same_key() -> None:
    snapshot = _snapshot(_unit("a", 10, 100, 2_900))

    result = _lock(
        snapshot,
        [
            _candidate("exact", ExactTime(1_000)),
            _candidate("within", WithinUnit("a")),
        ],
    )

    boundary = result.locked_boundaries[0]
    assert (boundary.kind, boundary.start_ms, boundary.end_ms) == (
        "span",
        100,
        2_900,
    )


def test_source_ordered_overlapping_spans_construct_shared_middle_ranges() -> None:
    snapshot = _snapshot(
        _unit("a", 10, 0, 300),
        _unit("b", 20, 100, 400),
        _unit("c", 30, 200, 500),
    )

    result = _lock(
        snapshot,
        [
            _candidate("one", BetweenUnits("a", "b")),
            _candidate("two", BetweenUnits("b", "c")),
        ],
        duration_ms=600,
    )

    assert _coordinates(result) == [(0, 500), (0, 500), (0, 600)]
    assert result.section_unit_ids == (
        ("a", "b", "c"),
        ("a", "b", "c"),
        ("a", "b", "c"),
    )


def test_nested_boundaries_follow_authoritative_transcript_order() -> None:
    snapshot = _snapshot(
        _unit("a", 10, 0, 500),
        _unit("b", 20, 100, 300),
        _unit("c", 30, 200, 400),
    )

    result = _lock(
        snapshot,
        [
            _candidate("one", BetweenUnits("a", "b")),
            _candidate("two", BetweenUnits("b", "c")),
        ],
        duration_ms=600,
    )

    assert _coordinates(result) == [(0, 500), (0, 500), (0, 600)]
    assert result.section_unit_ids == (
        ("a", "b", "c"),
        ("a", "b", "c"),
        ("a", "b", "c"),
    )


def test_equal_boundary_ends_can_still_make_nonempty_sections() -> None:
    snapshot = _snapshot(
        _unit("a", 10, 0, 1_000),
        _unit("b", 20, 1_000, 2_000),
        _unit("c", 30, 2_000, 3_000),
    )

    result = _lock(
        snapshot,
        [
            _candidate("within-b", WithinUnit("b")),
            _candidate("after-b", BetweenUnits("b", "c")),
        ],
        duration_ms=6_000,
    )

    assert _coordinates(result) == [(0, 2_000), (1_000, 2_000), (2_000, 6_000)]


def test_snapshot_and_timeline_evidence_is_validated() -> None:
    untimed = SegmentationSnapshot(
        snapshot_hash="untimed",
        source_kind="untimed_transcript",
        language=None,
        units=(DialogueUnit(id="a", ordinal=1, text="hello"),),
    )
    with pytest.raises(TranscriptBoundaryError) as untimed_error:
        lock_transcript_boundaries(untimed, [], timeline=_timeline())
    assert untimed_error.value.code == "untimed_transcript"

    timed = _snapshot(_unit("a", 1, 0, 2_000))
    with pytest.raises(TranscriptBoundaryError) as duration_error:
        lock_transcript_boundaries(timed, [], timeline=_timeline(None))
    assert duration_error.value.code == "timeline_duration_required"

    with pytest.raises(TranscriptBoundaryError) as bounds_error:
        _lock(timed, [], duration_ms=1_000)
    assert bounds_error.value.code == "unit_out_of_bounds"


def test_duplicate_candidate_ids_fail_before_receipt_mapping() -> None:
    snapshot = _snapshot(_unit("a", 10, 100, 2_900))

    with pytest.raises(TranscriptBoundaryError) as caught:
        _lock(
            snapshot,
            [
                _candidate("same", ExactTime(1_000)),
                _candidate("same", WithinUnit("a")),
            ],
        )

    assert caught.value.code == "duplicate_candidate_id"


def test_invalid_candidate_error_is_independent_of_input_order() -> None:
    snapshot = _snapshot(
        _unit("a", 10, 0, 900),
        _unit("b", 20, 1_000, 1_900),
        _unit("c", 30, 2_000, 2_900),
    )
    first_by_id = _candidate("a-non-adjacent", BetweenUnits("a", "c"))
    second_by_id = _candidate("z-unknown", WithinUnit("missing"))

    errors: list[tuple[str, str | None]] = []
    for candidates in (
        [first_by_id, second_by_id],
        [second_by_id, first_by_id],
    ):
        with pytest.raises(TranscriptBoundaryError) as caught:
            _lock(snapshot, candidates)
        errors.append((caught.value.code, caught.value.candidate_id))

    assert errors == [
        ("non_adjacent_units", "a-non-adjacent"),
        ("non_adjacent_units", "a-non-adjacent"),
    ]


def test_receipt_metadata_records_mapping_and_final_value_hash() -> None:
    snapshot = _snapshot(
        _unit("a", 10, 0, 1_300),
        _unit("b", 20, 1_000, 2_000),
    )
    result = _lock(
        snapshot,
        [_candidate("overlap", BetweenUnits("a", "b"))],
    )

    metadata = result.receipt_metadata()

    assert metadata["policy_version"] == LOCKING_POLICY_VERSION
    assert metadata["candidate_locks"] == [
        {
            "candidate_id": "overlap",
            "boundary_key": 20,
            "kind": "span",
            "start_ms": 0,
            "end_ms": 2_000,
        }
    ]
    assert metadata["timeline_ranges_hash"] == canonical_temporal_hash(
        "timeline_ranges",
        result.timeline_ranges,
    )
