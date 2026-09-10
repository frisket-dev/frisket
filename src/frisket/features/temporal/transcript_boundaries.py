"""Lock engine-native transcript boundaries to one source timeline.

Topic engines identify source structure (a unit, a gap between units, or a
genuine time).  They do not manufacture durable temporal values themselves.
This module validates those locators against the immutable segmentation
snapshot, locks them to transcript timing, and constructs ordinary
``timeline_ranges`` covering the complete source timeline.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal

from frisket.features.temporal_values import (
    TimelineAnchor,
    TimelineRangesValue,
    canonical_temporal_hash,
)
from frisket.features.topic_segmentation.contracts import (
    BetweenUnits,
    BoundaryCandidate,
    DialogueUnit,
    ExactTime,
    SegmentationSnapshot,
    WithinUnit,
)


LOCKING_POLICY_VERSION = "frisket.transcript_boundaries.v1"
LockedBoundaryKind = Literal["point", "span"]


class TranscriptBoundaryError(ValueError):
    """A native boundary cannot be locked honestly to the source timeline."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        candidate_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.candidate_id = candidate_id


@dataclass(frozen=True, slots=True)
class CandidateLock:
    """The direct, pre-normalization lock for one native candidate.

    Points use equal ``start_ms`` and ``end_ms``.  This single coordinate form
    makes source ordering and receipt serialization unambiguous without
    pretending that a point is a non-empty temporal range.
    """

    candidate_id: str
    boundary_key: int
    kind: LockedBoundaryKind
    start_ms: int
    end_ms: int

    @property
    def at_ms(self) -> int | None:
        return self.start_ms if self.kind == "point" else None


@dataclass(frozen=True, slots=True)
class LockedBoundary:
    """One canonical transition after same-key observations are coalesced."""

    boundary_key: int
    kind: LockedBoundaryKind
    start_ms: int
    end_ms: int
    candidate_ids: tuple[str, ...]

    @property
    def at_ms(self) -> int | None:
        return self.start_ms if self.kind == "point" else None


@dataclass(frozen=True, slots=True)
class TranscriptBoundaryResult:
    """Inspectable locks plus the durable, generic topic-section value."""

    snapshot_hash: str
    policy_version: str
    candidate_locks: tuple[CandidateLock, ...]
    locked_boundaries: tuple[LockedBoundary, ...]
    timeline_ranges: TimelineRangesValue
    section_unit_ids: tuple[tuple[str, ...], ...]

    @property
    def raw_locks(self) -> tuple[CandidateLock, ...]:
        """Alias emphasizing that these records precede normalization."""

        return self.candidate_locks

    def receipt_metadata(self) -> dict[str, Any]:
        """Return the compact locking portion of an action receipt.

        The action receipt remains responsible for storing the exact snapshot
        and native candidates.  This mapping records how those candidate IDs
        locked, the effective transitions, and the final generic-value hash.
        """

        return {
            "policy_version": self.policy_version,
            "snapshot_hash": self.snapshot_hash,
            "candidate_locks": [
                {
                    "candidate_id": lock.candidate_id,
                    "boundary_key": lock.boundary_key,
                    "kind": lock.kind,
                    "start_ms": lock.start_ms,
                    "end_ms": lock.end_ms,
                }
                for lock in self.candidate_locks
            ],
            "locked_boundaries": [
                {
                    "boundary_key": boundary.boundary_key,
                    "kind": boundary.kind,
                    "start_ms": boundary.start_ms,
                    "end_ms": boundary.end_ms,
                    "candidate_ids": list(boundary.candidate_ids),
                }
                for boundary in self.locked_boundaries
            ],
            "section_unit_ids": {
                item.id: list(unit_ids)
                for item, unit_ids in zip(
                    self.timeline_ranges.items,
                    self.section_unit_ids,
                    strict=True,
                )
            },
            "timeline_ranges_hash": canonical_temporal_hash(
                "timeline_ranges",
                self.timeline_ranges,
            ),
        }


def _error(
    code: str,
    message: str,
    candidate: BoundaryCandidate | None = None,
) -> TranscriptBoundaryError:
    return TranscriptBoundaryError(
        code,
        message,
        candidate_id=candidate.id if candidate is not None else None,
    )


def _require_timed_snapshot(
    snapshot: SegmentationSnapshot,
    *,
    duration_ms: int,
) -> tuple[DialogueUnit, ...]:
    if not isinstance(snapshot, SegmentationSnapshot):
        raise _error(
            "invalid_snapshot",
            "transcript boundary locking requires a SegmentationSnapshot",
        )
    if snapshot.source_kind != "timestamped_transcript":
        raise _error(
            "untimed_transcript",
            "topic boundaries cannot become timeline ranges without timed units",
        )
    for unit in snapshot.units:
        # SegmentationSnapshot already guarantees paired timing.  These asserts
        # narrow the type and document the locking layer's required evidence.
        assert unit.start_ms is not None
        assert unit.end_ms is not None
        if unit.end_ms > duration_ms:
            raise _error(
                "unit_out_of_bounds",
                f"dialogue unit {unit.id!r} exceeds timeline duration_ms",
            )
    return snapshot.units


def _key_for_exact_time(
    units: tuple[DialogueUnit, ...],
    at_ms: int,
    candidate: BoundaryCandidate,
) -> int:
    """Return the source-latest active unit, or the first unit after a gap.

    Once a later source unit has started, an older long-overlapping unit cannot
    become the following unit again merely because the later unit ended.  This
    keeps exact-time boundary keys monotonic in authoritative transcript order.
    """

    latest_started_position: int | None = None
    for position, unit in enumerate(units):
        assert unit.start_ms is not None
        if unit.start_ms > at_ms:
            break
        latest_started_position = position

    if latest_started_position is not None:
        latest_started = units[latest_started_position]
        assert latest_started.end_ms is not None
        if at_ms < latest_started.end_ms:
            return latest_started.ordinal

    following_position = (
        0 if latest_started_position is None else latest_started_position + 1
    )
    for unit in units[following_position:]:
        assert unit.start_ms is not None
        if unit.start_ms > at_ms:
            return unit.ordinal

    raise _error(
        "exact_time_without_following_unit",
        (
            f"exact_time {at_ms}ms does not fall in or before a dialogue unit; "
            "a canonical following-unit boundary key cannot be assigned"
        ),
        candidate,
    )


def _lock_candidate(
    candidate: BoundaryCandidate,
    *,
    units: tuple[DialogueUnit, ...],
    unit_by_id: dict[str, DialogueUnit],
    position_by_id: dict[str, int],
    duration_ms: int,
) -> CandidateLock:
    locator = candidate.locator
    if isinstance(locator, BetweenUnits):
        left = unit_by_id.get(locator.left_unit_id)
        right = unit_by_id.get(locator.right_unit_id)
        if left is None or right is None:
            missing = locator.left_unit_id if left is None else locator.right_unit_id
            raise _error(
                "unknown_unit",
                f"between_units references unknown dialogue unit {missing!r}",
                candidate,
            )
        if position_by_id[right.id] != position_by_id[left.id] + 1:
            raise _error(
                "non_adjacent_units",
                (
                    "between_units requires adjacent units in authoritative "
                    f"snapshot order: {left.id!r}, {right.id!r}"
                ),
                candidate,
            )
        assert left.end_ms is not None
        assert left.start_ms is not None
        assert right.start_ms is not None
        assert right.end_ms is not None
        if right.start_ms < left.end_ms:
            # Transcript chunks are atomic.  When adjacent chunks overlap,
            # there is no honest point seam between them: the complete union
            # of both chunks is shared by the two neighboring sections.
            return CandidateLock(
                candidate_id=candidate.id,
                boundary_key=right.ordinal,
                kind="span",
                start_ms=min(left.start_ms, right.start_ms),
                end_ms=max(left.end_ms, right.end_ms),
            )
        return CandidateLock(
            candidate_id=candidate.id,
            boundary_key=right.ordinal,
            kind="point",
            start_ms=right.start_ms,
            end_ms=right.start_ms,
        )

    if isinstance(locator, WithinUnit):
        unit = unit_by_id.get(locator.unit_id)
        if unit is None:
            raise _error(
                "unknown_unit",
                f"within_unit references unknown dialogue unit {locator.unit_id!r}",
                candidate,
            )
        assert unit.start_ms is not None
        assert unit.end_ms is not None
        return CandidateLock(
            candidate_id=candidate.id,
            boundary_key=unit.ordinal,
            kind="span",
            start_ms=unit.start_ms,
            end_ms=unit.end_ms,
        )

    if isinstance(locator, ExactTime):
        if locator.at_ms <= 0 or locator.at_ms >= duration_ms:
            raise _error(
                "exact_time_out_of_bounds",
                (
                    f"exact_time must be strictly inside the {duration_ms}ms "
                    f"timeline, got {locator.at_ms}ms"
                ),
                candidate,
            )
        return CandidateLock(
            candidate_id=candidate.id,
            boundary_key=_key_for_exact_time(units, locator.at_ms, candidate),
            kind="point",
            start_ms=locator.at_ms,
            end_ms=locator.at_ms,
        )

    raise _error(
        "unsupported_locator",
        f"candidate {candidate.id!r} has an unsupported boundary locator",
        candidate,
    )


def _coalesce_same_key(locks: list[CandidateLock]) -> LockedBoundary:
    boundary_key = locks[0].boundary_key
    candidate_ids = tuple(sorted(lock.candidate_id for lock in locks))
    spans = sorted(
        (lock for lock in locks if lock.kind == "span"),
        key=lambda lock: (lock.start_ms, lock.end_ms, lock.candidate_id),
    )
    if spans:
        return LockedBoundary(
            boundary_key=boundary_key,
            kind="span",
            start_ms=min(span.start_ms for span in spans),
            end_ms=max(span.end_ms for span in spans),
            candidate_ids=candidate_ids,
        )

    # Canonical source order is key then time, so the earliest point is the
    # effective coordinate when several observations describe this transition.
    # Every observation remains present in candidate_locks for the receipt.
    at_ms = min(lock.start_ms for lock in locks)
    return LockedBoundary(
        boundary_key=boundary_key,
        kind="point",
        start_ms=at_ms,
        end_ms=at_ms,
        candidate_ids=candidate_ids,
    )


def _normalize_locks(
    candidate_locks: tuple[CandidateLock, ...],
) -> tuple[LockedBoundary, ...]:
    by_key: dict[int, list[CandidateLock]] = defaultdict(list)
    for lock in candidate_locks:
        by_key[lock.boundary_key].append(lock)
    boundaries = tuple(_coalesce_same_key(by_key[key]) for key in sorted(by_key))

    for previous, current in zip(boundaries, boundaries[1:], strict=False):
        # Authoritative transcript order wins when imperfect ASR timestamps
        # overlap or nest.  The only time-order condition needed for section
        # construction is that the middle section [previous.start, current.end)
        # remains non-empty.
        if previous.start_ms >= current.end_ms:
            raise _error(
                "boundaries_not_source_ordered",
                (
                    "locked boundaries produce an empty section in source order: "
                    f"keys {previous.boundary_key} and {current.boundary_key}"
                ),
            )
    return boundaries


def _timeline_ranges(
    timeline: TimelineAnchor,
    boundaries: tuple[LockedBoundary, ...],
    units: tuple[DialogueUnit, ...],
) -> tuple[TimelineRangesValue, tuple[tuple[str, ...], ...]]:
    duration_ms = timeline.duration_ms
    assert duration_ms is not None
    coordinates: list[tuple[int, int]] = []
    if not boundaries:
        coordinates.append((0, duration_ms))
    else:
        coordinates.append((0, boundaries[0].end_ms))
        coordinates.extend(
            (previous.start_ms, current.end_ms)
            for previous, current in zip(boundaries, boundaries[1:], strict=False)
        )
        coordinates.append((boundaries[-1].start_ms, duration_ms))

    section_units = tuple(
        tuple(
            unit
            for unit in units
            if unit.start_ms is not None
            and unit.end_ms is not None
            and unit.start_ms < end_ms
            and unit.end_ms > start_ms
        )
        for start_ms, end_ms in coordinates
    )

    # Span boundaries express transcript granularity. Expand each adjacent
    # edge to the complete chunks selected from the original section exactly
    # once. Point boundaries stay exact so media consumers retain continuous
    # cuts; transcript consumers can still use the retained identities.
    aligned_coordinates: list[tuple[int, int]] = []
    for index, ((start_ms, end_ms), selected) in enumerate(
        zip(coordinates, section_units, strict=True)
    ):
        left_boundary = boundaries[index - 1] if index > 0 else None
        right_boundary = boundaries[index] if index < len(boundaries) else None
        if selected and left_boundary is not None and left_boundary.kind == "span":
            start_ms = min(
                start_ms,
                *(unit.start_ms for unit in selected if unit.start_ms is not None),
            )
        if selected and right_boundary is not None and right_boundary.kind == "span":
            end_ms = max(
                end_ms,
                *(unit.end_ms for unit in selected if unit.end_ms is not None),
            )
        aligned_coordinates.append((start_ms, end_ms))

    # Strict exact-time endpoint checks and source-order normalization should
    # make every section non-empty.  Keep this check here as an invariant guard
    # for future locator kinds or policy changes.
    for start_ms, end_ms in aligned_coordinates:
        if start_ms >= end_ms:
            raise _error(
                "empty_topic_section",
                f"locked boundaries produce an empty section [{start_ms}, {end_ms})",
            )

    items: list[dict[str, Any]] = []
    for index, ((requested_start, requested_end), (start_ms, end_ms)) in enumerate(
        zip(coordinates, aligned_coordinates, strict=True),
        start=1,
    ):
        item: dict[str, Any] = {
            "id": f"topic-section-{index:04d}",
            "start_ms": start_ms,
            "end_ms": end_ms,
        }
        if (start_ms, end_ms) != (requested_start, requested_end):
            item["metadata"] = {
                "requested_range": {
                    "start_ms": requested_start,
                    "end_ms": requested_end,
                },
                "boundary_policy": "include_intersecting_transcript_chunks",
            }
        items.append(item)
    ranges = TimelineRangesValue.model_validate(
        {
            "schema_version": "frisket.timeline_ranges.v1",
            "timeline": timeline.model_dump(mode="json"),
            "items": items,
        }
    )
    return ranges, tuple(
        tuple(unit.id for unit in selected) for selected in section_units
    )


def lock_transcript_boundaries(
    snapshot: SegmentationSnapshot,
    candidates: Iterable[BoundaryCandidate],
    *,
    timeline: TimelineAnchor | dict[str, Any],
) -> TranscriptBoundaryResult:
    """Validate, lock, normalize, and materialize native topic boundaries.

    Candidate order, strength, labels, and diagnostics do not affect the final
    temporal value.  Engine-specific facts remain available to the caller for
    its receipt; only source structure controls the durable section ranges.
    """

    anchor = (
        timeline
        if isinstance(timeline, TimelineAnchor)
        else TimelineAnchor.model_validate(timeline)
    )
    if anchor.duration_ms is None:
        raise _error(
            "timeline_duration_required",
            "topic section construction requires timeline duration_ms",
        )
    units = _require_timed_snapshot(snapshot, duration_ms=anchor.duration_ms)
    unit_by_id = {unit.id: unit for unit in units}
    position_by_id = {unit.id: position for position, unit in enumerate(units)}

    raw_candidates = tuple(candidates)
    for candidate in raw_candidates:
        if not isinstance(candidate, BoundaryCandidate):
            raise _error(
                "invalid_candidate",
                "transcript boundary candidates must be BoundaryCandidate values",
            )

    candidate_ids = [candidate.id for candidate in raw_candidates]
    candidate_id_counts: dict[str, int] = defaultdict(int)
    for candidate_id in candidate_ids:
        candidate_id_counts[candidate_id] += 1
    duplicate_ids = sorted(
        candidate_id for candidate_id, count in candidate_id_counts.items() if count > 1
    )
    if duplicate_ids:
        duplicate_id = duplicate_ids[0]
        duplicate = next(
            candidate for candidate in raw_candidates if candidate.id == duplicate_id
        )
        raise _error(
            "duplicate_candidate_id",
            f"duplicate boundary candidate id: {duplicate_id!r}",
            duplicate,
        )

    locks: list[CandidateLock] = []
    for candidate in sorted(raw_candidates, key=lambda item: item.id):
        locks.append(
            _lock_candidate(
                candidate,
                units=units,
                unit_by_id=unit_by_id,
                position_by_id=position_by_id,
                duration_ms=anchor.duration_ms,
            )
        )

    candidate_locks = tuple(
        sorted(
            locks,
            key=lambda lock: (
                lock.boundary_key,
                lock.start_ms,
                lock.end_ms,
                lock.kind,
                lock.candidate_id,
            ),
        )
    )
    locked_boundaries = _normalize_locks(candidate_locks)
    timeline_ranges, section_unit_ids = _timeline_ranges(
        anchor,
        locked_boundaries,
        units,
    )
    return TranscriptBoundaryResult(
        snapshot_hash=snapshot.snapshot_hash,
        policy_version=LOCKING_POLICY_VERSION,
        candidate_locks=candidate_locks,
        locked_boundaries=locked_boundaries,
        timeline_ranges=timeline_ranges,
        section_unit_ids=section_unit_ids,
    )


__all__ = [
    "LOCKING_POLICY_VERSION",
    "CandidateLock",
    "LockedBoundary",
    "TranscriptBoundaryError",
    "TranscriptBoundaryResult",
    "lock_transcript_boundaries",
]
