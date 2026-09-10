"""Project one typed temporal annotation into one derived clip timeline.

This module is deliberately pure.  Callers first prove that the annotation's
timeline maps unambiguously to the selected source artifact, then pass the
source-local clip window here.  Only Frisket's four temporal value types are
accepted; rows, columns, transcripts, and arbitrary enrichments are outside
this helper's scope.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from frisket.features.temporal_values import (
    TimelineAnchor,
    TimelinePointItem,
    TimelinePointsValue,
    TimelinePointValue,
    TimelineRangeItem,
    TimelineRangesValue,
    TimelineRangeValue,
    canonical_temporal_hash,
    parse_temporal_value,
)


PROJECTION_SCHEMA_VERSION = "frisket.temporal_annotation_projection.v1"


@dataclass(frozen=True, slots=True)
class TemporalAnnotationProjection:
    """Projected value plus the source identity needed by action receipts."""

    type_name: str
    value: dict[str, Any]
    source_timeline: dict[str, Any]
    derived_timeline: dict[str, Any]
    source_start_ms: int
    source_end_ms: int
    source_item_ids: tuple[str, ...]

    def receipt_metadata(self) -> dict[str, Any]:
        return {
            "schema_version": PROJECTION_SCHEMA_VERSION,
            "type": self.type_name,
            "source_timeline": dict(self.source_timeline),
            "derived_timeline": dict(self.derived_timeline),
            "source_start_ms": self.source_start_ms,
            "source_end_ms": self.source_end_ms,
            "source_item_ids": list(self.source_item_ids),
            "projected_value_hash": canonical_temporal_hash(self.type_name, self.value),
        }


def _integer_ms(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer millisecond coordinate")
    return value


def _project_point(
    item: TimelinePointItem,
    *,
    source_start_ms: int,
    source_end_ms: int,
) -> TimelinePointItem | None:
    if not source_start_ms <= item.at_ms < source_end_ms:
        return None
    return item.model_copy(update={"at_ms": item.at_ms - source_start_ms})


def _project_range(
    item: TimelineRangeItem,
    *,
    source_start_ms: int,
    source_end_ms: int,
) -> TimelineRangeItem | None:
    overlap_start = max(item.start_ms, source_start_ms)
    overlap_end = min(item.end_ms, source_end_ms)
    if overlap_start >= overlap_end:
        return None
    return item.model_copy(
        update={
            "start_ms": overlap_start - source_start_ms,
            "end_ms": overlap_end - source_start_ms,
        }
    )


def temporal_annotation_intersects(
    type_name: str,
    value: Any,
    *,
    source_start_ms: int,
    source_end_ms: int,
) -> bool:
    """Whether a translated half-open source window contains any annotation."""

    start_ms = _integer_ms(source_start_ms, "source_start_ms")
    end_ms = _integer_ms(source_end_ms, "source_end_ms")
    if end_ms <= start_ms:
        raise ValueError("projection source range must satisfy start < end")
    parsed = parse_temporal_value(type_name, value)
    if isinstance(parsed, TimelinePointValue):
        return start_ms <= parsed.item.at_ms < end_ms
    if isinstance(parsed, TimelinePointsValue):
        return any(start_ms <= item.at_ms < end_ms for item in parsed.items)
    if isinstance(parsed, TimelineRangeValue):
        return parsed.item.start_ms < end_ms and parsed.item.end_ms > start_ms
    if isinstance(parsed, TimelineRangesValue):
        return any(
            item.start_ms < end_ms and item.end_ms > start_ms for item in parsed.items
        )
    raise AssertionError("unsupported temporal annotation model")  # pragma: no cover


def project_temporal_annotation_items(
    type_name: str,
    value: Any,
    *,
    source_start_ms: int,
    source_end_ms: int,
) -> tuple[dict[str, Any], tuple[str, ...]] | None:
    """Intersect and rebase one compatible temporal cell onto a clip window.

    Point membership follows the suite's half-open interval rule: a point at
    the window start is retained at ``0``; a point at the window end belongs to
    the following child.  Ranges are clipped to the intersection.  Collection
    order and item identity, labels, and metadata are preserved verbatim.
    """

    start_ms = _integer_ms(source_start_ms, "source_start_ms")
    end_ms = _integer_ms(source_end_ms, "source_end_ms")
    if end_ms <= start_ms:
        raise ValueError("projection source range must satisfy start < end")

    parsed = parse_temporal_value(type_name, value)
    source_item_ids: tuple[str, ...]
    if isinstance(parsed, TimelinePointValue):
        item = _project_point(
            parsed.item,
            source_start_ms=start_ms,
            source_end_ms=end_ms,
        )
        if item is None:
            return None
        projected = {"item": item.model_dump(mode="json")}
        source_item_ids = (parsed.item.id,)
    elif isinstance(parsed, TimelinePointsValue):
        items = [
            projected
            for item in parsed.items
            if (
                projected := _project_point(
                    item,
                    source_start_ms=start_ms,
                    source_end_ms=end_ms,
                )
            )
            is not None
        ]
        if not items:
            return None
        projected = {"items": [item.model_dump(mode="json") for item in items]}
        source_item_ids = tuple(item.id for item in items)
    elif isinstance(parsed, TimelineRangeValue):
        item = _project_range(
            parsed.item,
            source_start_ms=start_ms,
            source_end_ms=end_ms,
        )
        if item is None:
            return None
        projected = {"item": item.model_dump(mode="json")}
        source_item_ids = (parsed.item.id,)
    elif isinstance(parsed, TimelineRangesValue):
        items = [
            projected
            for item in parsed.items
            if (
                projected := _project_range(
                    item,
                    source_start_ms=start_ms,
                    source_end_ms=end_ms,
                )
            )
            is not None
        ]
        if not items:
            return None
        projected = {"items": [item.model_dump(mode="json") for item in items]}
        source_item_ids = tuple(item.id for item in items)
    else:  # pragma: no cover - the four-type parse dispatch is closed
        raise AssertionError("unsupported temporal annotation model")

    return projected, source_item_ids


def project_temporal_annotation(
    type_name: str,
    value: Any,
    *,
    source_start_ms: int,
    source_end_ms: int,
    derived_timeline: TimelineAnchor | dict[str, Any],
) -> TemporalAnnotationProjection | None:
    """Bind shared item clipping to a durable derived timeline."""
    source_start_ms = _integer_ms(source_start_ms, "source_start_ms")
    source_end_ms = _integer_ms(source_end_ms, "source_end_ms")
    derived = TimelineAnchor.model_validate(derived_timeline)
    if derived.duration_ms != source_end_ms - source_start_ms:
        raise ValueError(
            "derived timeline duration must equal the projected source range"
        )
    projection = project_temporal_annotation_items(
        type_name, value, source_start_ms=source_start_ms, source_end_ms=source_end_ms
    )
    if projection is None:
        return None
    items, source_item_ids = projection
    parsed = parse_temporal_value(type_name, value)
    normalized = parse_temporal_value(
        type_name,
        {
            "schema_version": parsed.schema_version,
            "timeline": derived.model_dump(mode="json"),
            **items,
        },
    ).model_dump(mode="json")
    return TemporalAnnotationProjection(
        type_name=type_name,
        value=normalized,
        source_timeline=parsed.timeline.model_dump(mode="json"),
        derived_timeline=derived.model_dump(mode="json"),
        source_start_ms=source_start_ms,
        source_end_ms=source_end_ms,
        source_item_ids=source_item_ids,
    )


__all__ = [
    "PROJECTION_SCHEMA_VERSION",
    "TemporalAnnotationProjection",
    "project_temporal_annotation",
    "project_temporal_annotation_items",
    "temporal_annotation_intersects",
]
