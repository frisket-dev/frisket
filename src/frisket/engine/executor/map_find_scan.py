"""Provider scanning, grounding, and model-call facts for ``map.find``."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from frisket.ai.llm.structured import StructuredCompleter, StructuredRequest
from frisket.ai.map.find_source import (
    FindCandidate,
    accepted_window_candidates,
    locate_unique_match,
)
from frisket.ai.vision.region_locator import HostedVLMRegionLocator, LocatedRegion
from frisket.contracts.action import ActionError
from frisket.engine.executor.find_plan import FindOperation
from frisket.engine.executor.map_find_planning import (
    FindPlan,
    ImageFindPlan,
    _MAX_OUTPUT_TOKENS,
    _REPAIR_ATTEMPTS,
    find_response_schema,
)
from frisket.engine.executor.map_find_source import ResolvedFindSource


@dataclass(frozen=True)
class FindMatch:
    source: ResolvedFindSource
    candidate: FindCandidate
    span_ids: tuple[int, ...]


@dataclass(frozen=True)
class VisualFindMatch:
    source: ResolvedFindSource
    region: LocatedRegion


AnyFindMatch = FindMatch | VisualFindMatch


@dataclass(frozen=True)
class FindScanResult:
    plans: tuple[FindPlan, ...]
    matches: tuple[AnyFindMatch, ...]
    wire_calls: tuple[Any, ...]
    issues: tuple[ActionError, ...] = ()


async def scan_find_plans(
    params: FindOperation,
    plans: tuple[FindPlan, ...],
    *,
    router: Any,
) -> FindScanResult:
    schema = find_response_schema(params)
    located_matches: list[tuple[tuple[int, int, int, int], AnyFindMatch]] = []
    wire_calls: list[Any] = []
    issues: list[ActionError] = []
    seen_occurrences: set[tuple[int, int, int, str, int | str, int, int]] = set()
    source_positions: dict[
        tuple[int, int, int, str], tuple[int, dict[int | str, int]]
    ] = {}
    for plan in plans:
        source_key = (
            plan.source.sheet_id,
            plan.source.row_id,
            plan.source.column_id,
            plan.source.source_snapshot,
        )
        if source_key not in source_positions:
            source_positions[source_key] = (
                len(source_positions),
                {unit.unit_id: index for index, unit in enumerate(plan.source.units)},
            )
    for plan_index, plan in enumerate(plans):
        source_key = (
            plan.source.sheet_id,
            plan.source.row_id,
            plan.source.column_id,
            plan.source.source_snapshot,
        )
        source_index, unit_positions = source_positions[source_key]
        if isinstance(plan, ImageFindPlan):
            try:
                located = await HostedVLMRegionLocator(router).locate(plan.request)
            except Exception as exc:
                wire_calls.extend(getattr(exc, "wire_calls", ()) or ())
                issues.append(
                    ActionError(
                        code="incomplete_scan",
                        message=(
                            "An image source failed, so exhaustive coverage is "
                            "incomplete"
                        ),
                        action_kind=params.action_kind,
                        details={
                            "plan_index": plan_index,
                            "sheet_id": plan.source.sheet_id,
                            "row_id": plan.source.row_id,
                            "column_id": plan.source.column_id,
                            "source_snapshot": plan.source.source_snapshot,
                            "error": str(exc)[:500],
                        },
                    )
                )
                continue
            wire_calls.extend(located.wire_calls)
            if located.issues:
                issues.append(
                    ActionError(
                        code="region_validation_failed",
                        message=(
                            "One or more image matches had invalid grounded regions"
                        ),
                        action_kind=params.action_kind,
                        details={
                            "plan_index": plan_index,
                            "sheet_id": plan.source.sheet_id,
                            "row_id": plan.source.row_id,
                            "column_id": plan.source.column_id,
                            "source_snapshot": plan.source.source_snapshot,
                            "issues": [
                                {
                                    "code": issue.code,
                                    "message": issue.message,
                                    "item_index": issue.item_index,
                                }
                                for issue in located.issues
                            ],
                        },
                    )
                )
            located_matches.extend(
                (
                    (source_index, region_index, 0, 0),
                    VisualFindMatch(source=plan.source, region=region),
                )
                for region_index, region in enumerate(located.regions)
            )
            continue
        try:
            response = await StructuredCompleter(router).complete(
                StructuredRequest(
                    model=params.model,
                    messages=list(plan.messages),
                    schema=schema,
                    repair_attempts=_REPAIR_ATTEMPTS,
                    max_tokens=_MAX_OUTPUT_TOKENS,
                    temperature=0.0,
                )
            )
        except Exception as exc:
            wire_calls.extend(getattr(exc, "wire_calls", ()) or ())
            issues.append(
                ActionError(
                    code="incomplete_scan",
                    message=(
                        "A source window failed, so exhaustive coverage is incomplete"
                    ),
                    action_kind=params.action_kind,
                    details={
                        "window_index": plan.window.index,
                        "sheet_id": plan.source.sheet_id,
                        "row_id": plan.source.row_id,
                        "column_id": plan.source.column_id,
                        "source_snapshot": plan.source.source_snapshot,
                        "error": str(exc)[:500],
                    },
                )
            )
            continue
        wire_calls.extend(response.wire_calls)
        raw_matches = (
            response.data.get("matches") if isinstance(response.data, dict) else None
        )
        if not isinstance(raw_matches, list):
            issues.append(
                ActionError(
                    code="model_response_invalid",
                    message=("map.find model response did not contain a matches list"),
                    action_kind=params.action_kind,
                    details={
                        "window_index": plan.window.index,
                        "sheet_id": plan.source.sheet_id,
                        "row_id": plan.source.row_id,
                        "column_id": plan.source.column_id,
                        "source_snapshot": plan.source.source_snapshot,
                    },
                )
            )
            continue
        candidates = tuple(
            FindCandidate(
                match=str(item.get("match") or ""),
                source_unit_ids=tuple(item.get("source_unit_ids") or ()),
                details=dict(item.get("details") or {}),
            )
            for item in raw_matches
            if isinstance(item, dict)
        )
        admitted = accepted_window_candidates(plan.window, candidates)
        invalid = tuple(
            item for item in admitted.rejected if item.reason != "not_owner"
        )
        if invalid:
            issues.append(
                ActionError(
                    code="citation_validation_failed",
                    message=("A map.find match cited invalid or missing source units"),
                    action_kind=params.action_kind,
                    details={
                        "window_index": plan.window.index,
                        "sheet_id": plan.source.sheet_id,
                        "row_id": plan.source.row_id,
                        "column_id": plan.source.column_id,
                        "source_snapshot": plan.source.source_snapshot,
                        "reasons": [item.reason for item in invalid],
                    },
                )
            )
        units = {unit.unit_id: unit for unit in plan.window.units}

        def candidate_order(candidate: FindCandidate) -> tuple[int, int]:
            location = locate_unique_match(plan.window, candidate)
            assert location is not None
            return (
                min(
                    plan.window.unit_ids.index(unit_id)
                    for unit_id in candidate.source_unit_ids
                ),
                location[1],
            )

        for candidate in sorted(admitted.accepted, key=candidate_order):
            location = locate_unique_match(plan.window, candidate)
            if location is None:  # admitted candidates already prove this
                continue
            location_unit_id, match_start, match_end = location
            cited_span_ids = tuple(
                span_id
                for unit_id in candidate.source_unit_ids
                for span_id in units[unit_id].span_ids
            )
            if cited_span_ids and all(span_id < 0 for span_id in cited_span_ids):
                location_unit = units[location_unit_id]
                unit_start = -location_unit.span_ids[0] - 1
                span_ids = (
                    -(unit_start + match_start + 1),
                    -(unit_start + match_end + 1),
                )
            else:
                span_ids = tuple(dict.fromkeys(cited_span_ids))
            occurrence = (
                plan.source.sheet_id,
                plan.source.row_id,
                plan.source.column_id,
                plan.source.source_snapshot,
                location_unit_id,
                match_start,
                match_end,
            )
            if occurrence in seen_occurrences:
                continue
            seen_occurrences.add(occurrence)
            located_matches.append(
                (
                    (
                        source_index,
                        unit_positions[location_unit_id],
                        match_start,
                        match_end,
                    ),
                    FindMatch(
                        source=plan.source,
                        candidate=candidate,
                        span_ids=span_ids,
                    ),
                )
            )
    return FindScanResult(
        plans=plans,
        matches=tuple(
            match
            for _position, match in sorted(located_matches, key=lambda item: item[0])
        ),
        wire_calls=tuple(wire_calls),
        issues=tuple(issues),
    )
