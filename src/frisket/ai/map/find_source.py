"""Pure exhaustive-scan planning and match admission for ``map.find``.

This module deliberately knows nothing about models, projects, or evidence
storage. Source adapters supply stable addressed units; the planner gives every
unit one non-overlapping ownership core plus bounded overlap context; runtime
grounding later resolves the admitted unit ids by lookup.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


UnitId = int | str


@dataclass(frozen=True)
class AddressedUnit:
    unit_id: UnitId
    text: str
    span_ids: tuple[int, ...]


@dataclass(frozen=True)
class FindWindow:
    index: int
    units: tuple[AddressedUnit, ...]
    core_unit_ids: tuple[UnitId, ...]

    @property
    def unit_ids(self) -> tuple[UnitId, ...]:
        return tuple(unit.unit_id for unit in self.units)


@dataclass(frozen=True)
class FindCandidate:
    match: str
    source_unit_ids: tuple[UnitId, ...]
    details: dict[str, Any]


@dataclass(frozen=True)
class RejectedCandidate:
    candidate: FindCandidate
    reason: str


@dataclass(frozen=True)
class WindowCandidateResult:
    accepted: tuple[FindCandidate, ...]
    rejected: tuple[RejectedCandidate, ...]


def plan_find_windows(
    units: Iterable[AddressedUnit],
    *,
    max_core_units: int,
    overlap_units: int,
) -> tuple[FindWindow, ...]:
    """Partition ordered units into disjoint cores with adjacent context.

    The production caller chooses ``max_core_units`` from its model/token
    budget. Keeping the ownership primitive unit-count based makes it stable
    and independently testable; a source adapter may coalesce or split natural
    units before planning when one unit itself exceeds its text budget.
    """

    if max_core_units <= 0:
        raise ValueError("max_core_units must be positive")
    if overlap_units < 0:
        raise ValueError("overlap_units must be non-negative")
    ordered = tuple(units)
    unit_ids = [unit.unit_id for unit in ordered]
    if len(unit_ids) != len(set(unit_ids)):
        raise ValueError("addressed unit ids must be unique")

    windows: list[FindWindow] = []
    for index, core_start in enumerate(range(0, len(ordered), max_core_units)):
        core_end = min(len(ordered), core_start + max_core_units)
        context_start = max(0, core_start - overlap_units)
        context_end = min(len(ordered), core_end + overlap_units)
        windows.append(
            FindWindow(
                index=index,
                units=ordered[context_start:context_end],
                core_unit_ids=tuple(
                    unit.unit_id for unit in ordered[core_start:core_end]
                ),
            )
        )
    return tuple(windows)


def accepted_window_candidates(
    window: FindWindow,
    candidates: Iterable[FindCandidate],
) -> WindowCandidateResult:
    """Validate candidate anchors and enforce deterministic window ownership."""

    order = {unit_id: index for index, unit_id in enumerate(window.unit_ids)}
    core = set(window.core_unit_ids)
    accepted: list[FindCandidate] = []
    rejected: list[RejectedCandidate] = []
    for candidate in candidates:
        if not candidate.match.strip():
            rejected.append(RejectedCandidate(candidate, "blank_match"))
            continue
        if not candidate.source_unit_ids:
            rejected.append(RejectedCandidate(candidate, "evidence_required"))
            continue
        if any(unit_id not in order for unit_id in candidate.source_unit_ids):
            rejected.append(RejectedCandidate(candidate, "unknown_source_unit"))
            continue
        earliest = min(candidate.source_unit_ids, key=order.__getitem__)
        if earliest not in core:
            rejected.append(RejectedCandidate(candidate, "not_owner"))
            continue
        normalized = FindCandidate(
            match=candidate.match,
            source_unit_ids=tuple(
                sorted(dict.fromkeys(candidate.source_unit_ids), key=order.__getitem__)
            ),
            details=candidate.details,
        )
        if locate_unique_match(window, normalized) is None:
            rejected.append(
                RejectedCandidate(normalized, "match_not_unique_in_evidence")
            )
            continue
        accepted.append(normalized)
    return WindowCandidateResult(tuple(accepted), tuple(rejected))


def locate_unique_match(
    window: FindWindow,
    candidate: FindCandidate,
) -> tuple[UnitId, int, int] | None:
    """Locate one verbatim Match excerpt wholly inside one cited unit."""

    needle = candidate.match
    if not needle or not needle.strip():
        return None
    cited = set(candidate.source_unit_ids)
    locations: list[tuple[UnitId, int, int]] = []
    for unit in window.units:
        if unit.unit_id not in cited:
            continue
        cursor = 0
        while True:
            start = unit.text.find(needle, cursor)
            if start < 0:
                break
            locations.append((unit.unit_id, start, start + len(needle)))
            if len(locations) > 1:
                return None
            cursor = start + 1
    return locations[0] if len(locations) == 1 else None
