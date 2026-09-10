"""Resolve and project compatible typed temporal cells from one source row."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from frisket.engine.store.artifact_timeline import (
    EphemeralMediaClock,
    RootClockExtent,
    TimelineAnchor,
    TimelineError,
    canonical_json_hash,
    root_clock_extent,
    validate_timeline_anchor,
)
from frisket.features.temporal_annotation_projection import (
    TemporalAnnotationProjection,
    project_temporal_annotation,
    project_temporal_annotation_items,
    temporal_annotation_intersects,
)
from frisket.features.temporal_ingress import (
    TEMPORAL_COLUMN_TYPES,
    validate_temporal_persistence_value,
)
from frisket.features.temporal_values import parse_temporal_value


@dataclass(frozen=True, slots=True)
class ResolvedTemporalAnnotation:
    sheet_id: int
    row_id: int
    column_id: int
    column_name: str
    column_position: int
    type_name: str
    value: dict[str, Any]
    value_ref: dict[str, Any]
    value_hash: str
    timeline: TimelineAnchor
    source_to_annotation_offset_ms: int

    def input_ref(self) -> dict[str, Any]:
        return {
            "kind": "temporal_annotation_snapshot",
            "sheet_id": self.sheet_id,
            "row_id": self.row_id,
            "column_id": self.column_id,
            "column_name": self.column_name,
            "type": self.type_name,
            "value_ref": dict(self.value_ref),
            "value_hash": self.value_hash,
            "timeline": self.timeline.wire_value(),
            "source_to_annotation_offset_ms": self.source_to_annotation_offset_ms,
        }


def _root_extent(
    project: Any, anchor: TimelineAnchor | EphemeralMediaClock
) -> RootClockExtent:
    return root_clock_extent(project, anchor)


def _compatible_offset(
    project: Any,
    *,
    annotation_anchor: TimelineAnchor,
    source_anchor: TimelineAnchor | EphemeralMediaClock,
    source_root: RootClockExtent,
) -> int | None:
    if (
        isinstance(source_anchor, TimelineAnchor)
        and annotation_anchor.artifact_id == source_anchor.artifact_id
    ):
        return 0
    if (
        annotation_anchor.fingerprint == source_anchor.fingerprint
        and annotation_anchor.duration_ms == source_anchor.duration_ms
    ):
        return 0
    annotation_root = _root_extent(project, annotation_anchor)
    if (
        annotation_root.root_artifact_fingerprint
        != source_root.root_artifact_fingerprint
    ):
        return None
    if max(annotation_root.root_start_ms, source_root.root_start_ms) >= min(
        annotation_root.root_end_ms, source_root.root_end_ms
    ):
        return None
    # annotation-local = source-local + this offset
    return source_root.root_start_ms - annotation_root.root_start_ms


def resolve_compatible_temporal_annotations(
    project: Any,
    *,
    sheet_id: int,
    row_id: int,
    source_anchor: TimelineAnchor | EphemeralMediaClock,
    excluded_column_ids: Iterable[int] = (),
) -> tuple[ResolvedTemporalAnnotation, ...]:
    """Resolve only visible temporal cells that share an unambiguous root clock."""

    excluded = {int(column_id) for column_id in excluded_column_ids}
    source_root = _root_extent(project, source_anchor)
    columns = project.db.execute(
        "SELECT id, name, type, position FROM columns "
        "WHERE sheet_id=? AND hidden=0 ORDER BY position, id",
        (sheet_id,),
    ).fetchall()
    resolved: list[ResolvedTemporalAnnotation] = []
    for column in columns:
        column_id = int(column["id"])
        type_name = str(column["type"])
        if column_id in excluded or type_name not in TEMPORAL_COLUMN_TYPES:
            continue
        values, refs = project.get_values_with_refs(
            sheet_id,
            column_id,
            row_ids=[row_id],
        )
        value = values.get(row_id)
        value_ref = refs.get(row_id)
        if not isinstance(value, Mapping) or not isinstance(value_ref, Mapping):
            continue
        try:
            normalized = validate_temporal_persistence_value(
                project,
                type_name=type_name,
                value=value,
            )
            if normalized is None:  # pragma: no cover - Mapping excludes None
                continue
            parsed = parse_temporal_value(type_name, normalized)
            annotation_anchor = validate_timeline_anchor(
                project,
                parsed.timeline.model_dump(mode="json"),
            )
            offset = _compatible_offset(
                project,
                annotation_anchor=annotation_anchor,
                source_anchor=source_anchor,
                source_root=source_root,
            )
            if offset is None:
                continue
            value_hash = canonical_json_hash(dict(value))
        except (
            KeyError,
            LookupError,
            OverflowError,
            RecursionError,
            TimelineError,
            TypeError,
            ValidationError,
            ValueError,
        ):
            continue
        resolved.append(
            ResolvedTemporalAnnotation(
                sheet_id=sheet_id,
                row_id=row_id,
                column_id=column_id,
                column_name=str(column["name"]),
                column_position=int(column["position"]),
                type_name=type_name,
                value=normalized,
                value_ref=dict(value_ref),
                value_hash=value_hash,
                timeline=annotation_anchor,
                source_to_annotation_offset_ms=offset,
            )
        )
    return tuple(resolved)


def revalidate_temporal_annotation(
    project: Any,
    annotation: ResolvedTemporalAnnotation,
) -> None:
    column = project.db.execute(
        "SELECT name, type, position FROM columns "
        "WHERE id=? AND sheet_id=? AND hidden=0",
        (annotation.column_id, annotation.sheet_id),
    ).fetchone()
    values, refs = project.get_values_with_refs(
        annotation.sheet_id,
        annotation.column_id,
        row_ids=[annotation.row_id],
    )
    current = values.get(annotation.row_id)
    if (
        column is None
        or str(column["name"]) != annotation.column_name
        or str(column["type"]) != annotation.type_name
        or int(column["position"]) != annotation.column_position
        or not isinstance(current, Mapping)
        or canonical_json_hash(dict(current)) != annotation.value_hash
        or refs.get(annotation.row_id) != annotation.value_ref
    ):
        raise TimelineError(
            "stale_input", "a temporal annotation changed during execution"
        )
    current_anchor = validate_timeline_anchor(
        project,
        parse_temporal_value(annotation.type_name, current).timeline.model_dump(
            mode="json"
        ),
    )
    if current_anchor != annotation.timeline:
        raise TimelineError(
            "stale_input", "a temporal annotation timeline changed during execution"
        )


def annotation_intersects_source_range(
    annotation: ResolvedTemporalAnnotation,
    *,
    source_start_ms: int,
    source_end_ms: int,
) -> bool:
    offset = annotation.source_to_annotation_offset_ms
    return temporal_annotation_intersects(
        annotation.type_name,
        annotation.value,
        source_start_ms=source_start_ms + offset,
        source_end_ms=source_end_ms + offset,
    )


def project_resolved_temporal_annotation(
    annotation: ResolvedTemporalAnnotation,
    *,
    source_start_ms: int,
    source_end_ms: int,
    derived_timeline: TimelineAnchor | Mapping[str, Any],
) -> TemporalAnnotationProjection | None:
    offset = annotation.source_to_annotation_offset_ms
    timeline = (
        derived_timeline.wire_value()
        if isinstance(derived_timeline, TimelineAnchor)
        else dict(derived_timeline)
    )
    return project_temporal_annotation(
        annotation.type_name,
        annotation.value,
        source_start_ms=source_start_ms + offset,
        source_end_ms=source_end_ms + offset,
        derived_timeline=timeline,
    )


def project_resolved_temporal_annotation_items(
    annotation: ResolvedTemporalAnnotation,
    *,
    source_start_ms: int,
    source_end_ms: int,
) -> dict[str, Any] | None:
    """Shared clipping without asserting a persisted destination clock."""
    offset = annotation.source_to_annotation_offset_ms
    projection = project_temporal_annotation_items(
        annotation.type_name,
        annotation.value,
        source_start_ms=source_start_ms + offset,
        source_end_ms=source_end_ms + offset,
    )
    return projection[0] if projection is not None else None


def assign_temporal_annotation_output_names(
    annotations: Iterable[ResolvedTemporalAnnotation],
    *,
    used_names: Iterable[str],
    prefix: str = "",
) -> dict[int, str]:
    """Assign stable source-column names without colliding with other outputs."""

    by_column = {annotation.column_id: annotation for annotation in annotations}
    used = set(used_names)
    names: dict[int, str] = {}
    for annotation in sorted(
        by_column.values(),
        key=lambda item: (item.column_position, item.column_id),
    ):
        base = f"{prefix}{annotation.column_name}"
        candidate = base
        suffix = 2
        while candidate in used:
            candidate = f"{base} {suffix}"
            suffix += 1
        used.add(candidate)
        names[annotation.column_id] = candidate
    return names


__all__ = [
    "ResolvedTemporalAnnotation",
    "annotation_intersects_source_range",
    "assign_temporal_annotation_output_names",
    "project_resolved_temporal_annotation",
    "resolve_compatible_temporal_annotations",
    "revalidate_temporal_annotation",
]
