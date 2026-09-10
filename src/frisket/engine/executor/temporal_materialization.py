"""Action-owned preparation and exact clip materialization helpers.

This module is intentionally narrower than the generic timeline store.  It
turns action-panel drafts or typed temporal cells into ranges on one selected
source, stages exact media outside SQLite, and exposes commit-only operations
that the public actions call from their final write transaction.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from frisket.authoring import column_types
from frisket.contracts.action import ActionError, ActionOutput, ReceiptIO
from frisket.actions.temporal_types import (
    TemporalMediaColumn,
    TranscriptSelection,
    validate_transcript_selection_scope,
)
from frisket.actions.types import SheetRows, TableError
from frisket.engine.store.artifact_timeline import (
    EphemeralMediaClock,
    TimelineAnchor,
    TimelineError,
    TimelineLease,
    canonical_json_hash,
    root_clock_extent,
    resolve_timeline,
    validate_timeline_anchor,
    write_rate1_timeline_segment,
)
from frisket.engine.store.evidence import record_source_artifact
from frisket.engine.store.media_blobs import (
    MediaBlobStore,
    owned_media_metadata_document,
)
from frisket.features.temporal_values import (
    TimelinePointItem,
    TimelineRangeItem,
    normalize_temporal_value,
    parse_temporal_value,
)

if TYPE_CHECKING:
    from frisket.engine.executor.temporal_annotations import ResolvedTemporalAnnotation
    from frisket.engine.executor.temporal_finder_provenance import (
        ResolvedTopicAnalysisSidecar,
    )
    from frisket.engine.executor.temporal_transcripts import ResolvedTranscriptSource


_SCHEMA_TO_TYPE = {
    "frisket.timeline_point.v1": "timeline_point",
    "frisket.timeline_points.v1": "timeline_points",
    "frisket.timeline_range.v1": "timeline_range",
    "frisket.timeline_ranges.v1": "timeline_ranges",
}


@dataclass(frozen=True)
class NormalizedRange:
    start_ms: int
    end_ms: int
    item_id: str
    label: str | None = None
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class ResolvedTemporalSource:
    sheet_id: int
    row_id: int
    column_id: int
    column_name: str
    column_type: str
    value: dict[str, Any]
    value_ref: dict[str, Any]
    value_hash: str
    lease: TimelineLease
    selection_value: dict[str, Any]
    selection_ref: dict[str, Any]
    selection_hash: str
    selection_column_id: int | None
    ranges: tuple[NormalizedRange, ...]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class ResolvedTemporalMediaSource:
    """One media source with the exact compatible facts it can pass on."""

    source: ResolvedTemporalSource
    transcripts: tuple[ResolvedTranscriptSource, ...]
    annotations: tuple[ResolvedTemporalAnnotation, ...]
    topic_analysis: ResolvedTopicAnalysisSidecar | None
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class ResolvedTemporalInputColumns:
    source_column_id: int
    source_column_type: str
    selection_column_id: int | None
    selection_column_type: str | None


def resolve_temporal_input_columns(
    project: Any,
    *,
    scope: SheetRows,
    source: TemporalMediaColumn,
    selection: TranscriptSelection,
    action_kind: str,
) -> ResolvedTemporalInputColumns | ActionError:
    """Resolve the capability's actual semantic arguments, not action Params."""
    from frisket.engine.executor.map_rows_action import (
        TypedMapRowsPlanError,
        validate_typed_project_references,
    )

    try:
        references = validate_typed_project_references(
            project, scope.sheet_id, (source, selection)
        )
    except TypedMapRowsPlanError as exc:
        return action_error(action_kind, exc.code, str(exc), details=exc.details)
    columns = {
        str(column["name"]): column for column in project.columns(scope.sheet_id)
    }
    selection_name = selection.column.root if selection.kind == "column" else None
    return ResolvedTemporalInputColumns(
        source_column_id=references.source_column_ids[source.root],
        source_column_type=str(columns[source.root]["type"]),
        selection_column_id=references.source_column_ids[selection_name]
        if selection_name
        else None,
        selection_column_type=str(columns[selection_name]["type"])
        if selection_name
        else None,
    )


@dataclass(frozen=True)
class StagedTemporalClip:
    source: ResolvedTemporalSource
    requested: NormalizedRange
    payload: Any

    @property
    def resolved_start_ms(self) -> int:
        return int(self.payload.resolved_source_start_ms)

    @property
    def resolved_end_ms(self) -> int:
        return int(self.payload.resolved_source_end_ms)


def validate_staged_temporal_clip(
    source: ResolvedTemporalSource,
    requested: NormalizedRange,
    payload: Any,
    output_path: Path,
) -> None:
    """Require one honest rate-1 mapping before a staged clip can publish."""

    try:
        path = Path(payload.path).resolve()
        requested_start = int(payload.requested_start_ms)
        requested_end = int(payload.requested_end_ms)
        resolved_start = int(payload.resolved_source_start_ms)
        resolved_end = int(payload.resolved_source_end_ms)
        duration = int(payload.duration_ms)
    except (AttributeError, TypeError, ValueError) as exc:
        raise TimelineError(
            "cut_alignment_failed",
            "staged renderer returned incomplete temporal mapping facts",
        ) from exc
    if path != output_path.resolve() or not path.is_file() or path.stat().st_size <= 0:
        raise TimelineError(
            "cut_alignment_failed",
            "staged renderer did not produce its assigned output",
        )
    if requested_start != requested.start_ms or requested_end != requested.end_ms:
        raise TimelineError(
            "cut_alignment_failed",
            "staged renderer changed the requested interval",
        )
    if resolved_start < 0 or resolved_end <= resolved_start:
        raise TimelineError(
            "cut_alignment_failed",
            "staged renderer returned an invalid source interval",
        )
    if resolved_end > source.lease.anchor.duration_ms:
        raise TimelineError(
            "cut_alignment_failed",
            "staged renderer resolved beyond the source timeline",
        )
    if resolved_end - resolved_start != duration:
        raise TimelineError(
            "cut_alignment_failed",
            "staged duration and resolved source interval differ",
        )


@dataclass(frozen=True)
class CommittedTemporalClip:
    staged: StagedTemporalClip
    blob_hash: str
    cell_value: dict[str, Any]
    blob_derivation_id: int


class CoreTemporalMediaMaterializer:
    """Fixed-profile exact ffmpeg renderer and host-owned lineage committer."""

    def __init__(
        self,
        *,
        should_cancel: Callable[[], bool] | None = None,
    ) -> None:
        self._should_cancel = should_cancel

    def stage(
        self,
        project: Any,
        source: ResolvedTemporalSource,
        temporal_range: NormalizedRange,
        output_path: Path,
    ) -> Any:
        from frisket.engine.store.media_splice import (
            MediaSpliceError,
            stage_media_splice,
        )

        source_media_kind = media_kind(source)
        try:
            with project.materialize_blob(source.lease.blob_hash) as source_path:
                return asyncio.run(
                    stage_media_splice(
                        Path(source_path),
                        output_path,
                        media_kind=source_media_kind,
                        start_ms=temporal_range.start_ms,
                        end_ms=temporal_range.end_ms,
                        source_mime=source.lease.media_type,
                        source_filename=source.lease.filename,
                        should_cancel=self._should_cancel,
                    )
                )
        except MediaSpliceError:
            raise

    def commit_blob(
        self,
        project: Any,
        staged: StagedTemporalClip,
    ) -> CommittedTemporalClip:
        if not project.db.in_transaction:
            raise RuntimeError("temporal blob commit requires an active transaction")
        rendered = staged.payload
        probe_metadata = (
            rendered.probe.as_dict()
            if hasattr(rendered.probe, "as_dict")
            else dict(rendered.probe)
        )
        metadata = owned_media_metadata_document(
            probe={
                "kind": rendered.media_kind,
                "duration_seconds": rendered.duration_ms / 1000,
            },
            owner={
                "renderer_profile": rendered.renderer_profile,
                "renderer_params": rendered.renderer_params,
                "probe": probe_metadata,
            },
        )
        blob_hash = project.add_blob_from_path(
            Path(rendered.path),
            filename=rendered.filename,
            mime=rendered.mime,
            metadata=metadata,
            commit=False,
        )
        derivation_id = project.record_blob_derivation(
            derived_hash=blob_hash,
            source_hash=staged.source.lease.blob_hash,
            op="ffmpeg_exact_temporal_splice",
            params=rendered.receipt_metadata(),
            commit=False,
        )
        cell_value = MediaBlobStore.media_cell(
            blob_hash,
            mime=rendered.mime,
            filename=rendered.filename,
        )
        return CommittedTemporalClip(
            staged=staged,
            blob_hash=blob_hash,
            cell_value=cell_value,
            blob_derivation_id=derivation_id,
        )

    def attach_lineage(
        self,
        project: Any,
        committed: CommittedTemporalClip,
        *,
        output_sheet_id: int,
        output_row_id: int,
        output_column_id: int,
        receipt_id: str,
    ) -> dict[str, Any]:
        if not project.db.in_transaction:
            raise RuntimeError("temporal lineage commit requires an active transaction")
        rendered = committed.staged.payload
        artifact = record_source_artifact(
            project,
            artifact_kind="av",
            media_type=rendered.mime,
            blob_hash=committed.blob_hash,
            filename=rendered.filename,
            duration_ms=int(rendered.duration_ms),
            source_sheet_id=output_sheet_id,
            source_row_id=output_row_id,
            source_column_id=output_column_id,
            metadata={
                "producer_action": "temporal_splice",
                "renderer": rendered.receipt_metadata(),
            },
        )
        segment = write_rate1_timeline_segment(
            project,
            derived_artifact_id=int(artifact["id"]),
            source_artifact_id=committed.staged.source.lease.anchor.artifact_id,
            source_start_ms=committed.staged.resolved_start_ms,
            source_end_ms=committed.staged.resolved_end_ms,
            precision=rendered.precision,
            receipt_id=receipt_id,
            params=rendered.receipt_metadata(),
        )
        return {
            "kind": "derived_temporal_artifact",
            "artifact_id": int(artifact["id"]),
            "artifact_stable_id": str(artifact["stable_id"]),
            "timeline_segment_id": int(segment.id),
            "blob_hash": committed.blob_hash,
            "blob_derivation_id": committed.blob_derivation_id,
            "source_artifact_id": committed.staged.source.lease.anchor.artifact_id,
            "source_artifact_stable_id": (
                committed.staged.source.lease.anchor.artifact_stable_id
            ),
            "requested_start_ms": committed.staged.requested.start_ms,
            "requested_end_ms": committed.staged.requested.end_ms,
            "resolved_start_ms": committed.staged.resolved_start_ms,
            "resolved_end_ms": committed.staged.resolved_end_ms,
            "render": rendered.receipt_metadata(),
            "output": {
                "sheet_id": output_sheet_id,
                "row_id": output_row_id,
                "column_id": output_column_id,
            },
        }


def prepare_bound_sources(
    project: Any,
    *,
    scope: SheetRows,
    source: TemporalMediaColumn,
    selection: TranscriptSelection,
    action_kind: str,
    purpose: str,
    source_resolver: Callable[..., TimelineLease] | None = None,
) -> tuple[ResolvedTemporalSource, ...] | ActionError:
    """Resolve source/selection snapshots and normalize ranges for one action."""

    validate_transcript_selection_scope(selection, scope)
    sheet = project.db.execute(
        "SELECT id FROM sheets WHERE id=? AND hidden=0", (scope.sheet_id,)
    ).fetchone()
    if sheet is None:
        return action_error(
            action_kind, "invalid_input_ref", "source sheet is not visible"
        )
    input_columns = resolve_temporal_input_columns(
        project,
        scope=scope,
        source=source,
        selection=selection,
        action_kind=action_kind,
    )
    if isinstance(input_columns, ActionError):
        return input_columns
    column_id = input_columns.source_column_id
    row_ids = (
        list(scope.row_ids)
        if scope.row_ids is not None
        else project.visible_row_ids(scope.sheet_id, None)
    )
    if not row_ids:
        return action_error(
            action_kind, "invalid_input_ref", "source sheet has no rows"
        )
    visible = project.visible_row_ids(scope.sheet_id, row_ids)
    if set(visible) != set(row_ids):
        return action_error(action_kind, "invalid_input_ref", "row_ids are not visible")

    source_values, source_refs = project.get_values_with_refs(
        scope.sheet_id, column_id, row_ids=row_ids
    )
    selection_column = None
    selection_values: dict[int, Any] = {}
    selection_refs: dict[int, dict[str, Any]] = {}
    if input_columns.selection_column_id is not None:
        selection_column = {
            "id": input_columns.selection_column_id,
            "type": input_columns.selection_column_type,
        }
        selection_values, selection_refs = project.get_values_with_refs(
            scope.sheet_id,
            input_columns.selection_column_id,
            row_ids=row_ids,
        )

    resolved: list[ResolvedTemporalSource] = []
    for row_id in row_ids:
        source_value = source_values.get(row_id)
        source_ref = source_refs.get(row_id)
        if not isinstance(source_value, dict) or not isinstance(source_ref, dict):
            return action_error(
                action_kind, "invalid_input_ref", "source cell is empty"
            )
        try:
            captured = (
                {"value": source_value, "current_value_ref": source_ref}
                if source_resolver is not None
                else {}
            )
            lease = (source_resolver or resolve_source_timeline)(
                project,
                sheet_id=scope.sheet_id,
                row_id=row_id,
                column_id=column_id,
                **captured,
            )
            selection_value, selection_ref, selection_type = selection_for_row(
                selection,
                row_id=row_id,
                selection_column=selection_column,
                selection_values=selection_values,
                selection_refs=selection_refs,
            )
            is_draft = selection_type == "draft"
            if is_draft:
                selection_value, selection_type = _draft_to_bound_value(
                    selection_value, lease.anchor
                )
            else:
                # Receipt snapshots and their hashes always use the canonical,
                # default-expanded temporal wire representation.  In
                # particular, imported cells may omit optional ``label`` or
                # ``metadata`` fields.
                selection_value = _normalize_temporal_selection_value(
                    selection_type, selection_value
                )
            if is_draft and isinstance(lease.anchor, EphemeralMediaClock):
                item_model = (
                    TimelinePointItem
                    if selection_type == "timeline_points"
                    else TimelineRangeItem
                )
                items = [
                    item_model.model_validate(item)
                    for item in selection_value.get(
                        "items", [selection_value.get("item")]
                    )
                ]
                ranges, warnings = _normalize_selection_items(
                    project,
                    items,
                    selection_type=selection_type,
                    typed_anchor=lease.anchor,
                    source_anchor=lease.anchor,
                    purpose=purpose,
                )
            else:
                ranges, warnings = normalize_selection(
                    project,
                    selection_value,
                    selection_type=selection_type,
                    source_anchor=lease.anchor,
                    purpose=purpose,
                )
        except TableError:
            raise
        except TimelineError as exc:
            return action_error(action_kind, exc.code, exc.message)
        except (KeyError, TypeError, ValueError) as exc:
            return action_error(
                action_kind,
                "invalid_temporal_value",
                str(exc) or "temporal selection did not validate",
            )
        resolved.append(
            ResolvedTemporalSource(
                sheet_id=scope.sheet_id,
                row_id=row_id,
                column_id=column_id,
                column_name=source.root,
                column_type=input_columns.source_column_type,
                value=dict(source_value),
                value_ref=dict(source_ref),
                value_hash=canonical_json_hash(source_value),
                lease=lease,
                selection_value=selection_value,
                selection_ref=selection_ref,
                selection_hash=canonical_json_hash(selection_value),
                selection_column_id=(
                    int(selection_column["id"])
                    if selection_column is not None
                    else None
                ),
                ranges=tuple(ranges),
                warnings=tuple(warnings),
            )
        )
    return tuple(resolved)


def temporal_inherited_output_keys(
    transcripts, annotations, *, reserved=("clip", "source_range"), prefix=""
):
    """Name inherited outputs once, by source names rather than database IDs."""
    from frisket.engine.executor.temporal_annotations import (
        assign_temporal_annotation_output_names,
    )

    used = set(reserved)
    names = {}
    by_column = {item.transcript_column_id: item for item in transcripts}
    for item in sorted(
        by_column.values(),
        key=lambda item: (item.transcript_column_position, item.transcript_column_id),
    ):
        base = f"{prefix}{item.transcript_column_name}"
        candidate = base if base not in used else f"{base} transcript"
        suffix = 2
        while candidate in used:
            candidate = f"{base} transcript {suffix}"
            suffix += 1
        used.add(candidate)
        names[item.transcript_column_id] = candidate
    return names, assign_temporal_annotation_output_names(
        annotations, used_names=used, prefix=prefix
    )


def resolve_temporal_media_source(
    project: Any,
    source: ResolvedTemporalSource,
) -> ResolvedTemporalMediaSource:
    """Resolve the current transcripts, annotations, and topic provenance once."""

    from frisket.engine.executor.temporal_annotations import (
        resolve_compatible_temporal_annotations,
    )
    from frisket.engine.executor.temporal_finder_provenance import (
        resolve_topic_analysis_sidecar,
    )
    from frisket.engine.executor.temporal_transcripts import (
        resolve_compatible_transcripts,
    )

    transcripts, transcript_warnings = resolve_compatible_transcripts(project, source)
    topic_analysis = (
        resolve_topic_analysis_sidecar(
            project,
            sheet_id=source.sheet_id,
            row_id=source.row_id,
            selection_column_id=source.selection_column_id,
        )
        if source.selection_column_id is not None
        else None
    )
    if topic_analysis is not None:
        producing_id = str(topic_analysis.payload["transcript_evidence_id"])
        if all(
            transcript.evidence_link_stable_id != producing_id
            for transcript in transcripts
        ):
            raise TimelineError(
                "stale_input",
                "Topic ranges require a producing transcript that is no longer current",
            )
    annotations = resolve_compatible_temporal_annotations(
        project,
        sheet_id=source.sheet_id,
        row_id=source.row_id,
        source_anchor=source.lease.anchor,
        excluded_column_ids=(
            (source.selection_column_id,)
            if source.selection_column_id is not None
            else ()
        ),
    )
    return ResolvedTemporalMediaSource(
        source=source,
        transcripts=transcripts,
        annotations=annotations,
        topic_analysis=topic_analysis,
        warnings=tuple(dict.fromkeys((*source.warnings, *transcript_warnings))),
    )


def revalidate_temporal_media_source(
    project: Any,
    resolved: ResolvedTemporalMediaSource,
) -> None:
    """Fence the compatible facts immediately before publication."""

    from frisket.engine.executor.temporal_annotations import (
        revalidate_temporal_annotation,
    )
    from frisket.engine.executor.temporal_finder_provenance import (
        resolve_topic_analysis_sidecar,
    )
    from frisket.engine.executor.temporal_transcripts import revalidate_transcript

    if resolved.topic_analysis is not None:
        current = resolve_topic_analysis_sidecar(
            project,
            sheet_id=resolved.source.sheet_id,
            row_id=resolved.source.row_id,
            selection_column_id=resolved.source.selection_column_id,
        )
        if (
            current is None
            or current.receipt_ref() != resolved.topic_analysis.receipt_ref()
        ):
            raise TimelineError("stale_input", "topic analysis changed during action")
    for transcript in resolved.transcripts:
        revalidate_transcript(project, transcript)
    for annotation in resolved.annotations:
        revalidate_temporal_annotation(project, annotation)


def revalidate_action_sources(
    project: Any,
    sources: tuple[ResolvedTemporalSource, ...],
) -> None:
    """TOCTOU fence used immediately before staging and in final transaction."""

    for source in sources:
        values, refs = project.get_values_with_refs(
            source.sheet_id, source.column_id, row_ids=[source.row_id]
        )
        current = values.get(source.row_id)
        current_ref = refs.get(source.row_id)
        if (
            canonical_json_hash(current) != source.value_hash
            or current_ref != source.value_ref
        ):
            raise TimelineError("stale_input", "source cell changed during execution")
        lease = resolve_timeline(
            project,
            sheet_id=source.sheet_id,
            row_id=source.row_id,
            column_id=source.column_id,
        )
        if (
            lease.blob_hash != source.lease.blob_hash
            or lease.anchor.fingerprint != source.lease.anchor.fingerprint
            or lease.anchor.duration_ms != source.lease.anchor.duration_ms
            or lease.media_kind != source.lease.media_kind
            or lease.media_type != source.lease.media_type
        ):
            raise TimelineError(
                "stale_input", "source timeline changed during execution"
            )
        if source.selection_column_id is not None:
            selection_values, selection_refs = project.get_values_with_refs(
                source.sheet_id,
                source.selection_column_id,
                row_ids=[source.row_id],
            )
            selection = selection_values.get(source.row_id)
            selection_ref = selection_refs.get(source.row_id)
            try:
                current_selection = normalize_temporal_value(
                    _type_for_value(source.selection_value), selection
                )
            except (LookupError, TypeError, ValueError) as exc:
                raise TimelineError(
                    "stale_input", "temporal selection changed during execution"
                ) from exc
            if (
                canonical_json_hash(current_selection) != source.selection_hash
                or selection_ref != source.selection_ref
            ):
                raise TimelineError(
                    "stale_input", "temporal selection changed during execution"
                )


def normalize_selection(
    project: Any,
    value: dict[str, Any],
    *,
    selection_type: str,
    source_anchor: TimelineAnchor | EphemeralMediaClock,
    purpose: str,
) -> tuple[list[NormalizedRange], list[str]]:
    """Map and normalize a structural temporal value for Extract or Split."""

    try:
        parsed = parse_temporal_value(selection_type, value)
    except ValidationError as exc:
        if _is_temporal_bounds_validation_error(exc):
            raise TimelineError(
                "range_out_of_bounds",
                "The selected timestamp or range extends outside its timeline duration.",
            ) from exc
        raise
    typed_anchor = validate_timeline_anchor(project, parsed.timeline.model_dump())
    items = (
        [parsed.item]
        if selection_type in {"timeline_range", "timeline_point"}
        else list(parsed.items)
    )
    return _normalize_selection_items(
        project,
        items,
        selection_type=selection_type,
        typed_anchor=typed_anchor,
        source_anchor=source_anchor,
        purpose=purpose,
    )


def _normalize_selection_items(
    project: Any,
    items: list[TimelineRangeItem] | list[TimelinePointItem],
    *,
    selection_type: str,
    typed_anchor: TimelineAnchor | EphemeralMediaClock,
    source_anchor: TimelineAnchor | EphemeralMediaClock,
    purpose: str,
) -> tuple[list[NormalizedRange], list[str]]:
    points = selection_type in {"timeline_point", "timeline_points"}
    if any(
        (item.at_ms if points else item.end_ms) > typed_anchor.duration_ms
        for item in items
    ):
        raise TimelineError(
            "range_out_of_bounds",
            "The selected timestamp or range extends outside its timeline duration.",
        )
    warnings: list[str] = []
    if selection_type in {"timeline_range", "timeline_ranges"}:
        if purpose == "extract" and len(items) != 1:
            raise TimelineError("invalid_range", "Extract requires exactly one range")
        ranges = [
            _map_range_to_source(
                project,
                typed_anchor=typed_anchor,
                source_anchor=source_anchor,
                start_ms=item.start_ms,
                end_ms=item.end_ms,
                item_id=item.id,
                label=item.label,
                metadata=dict(item.metadata),
            )
            for item in items
        ]
        return ranges, warnings

    if purpose == "extract":
        raise TimelineError("invalid_range", "Extract requires one timeline_range")
    source_points: dict[int, list[str]] = {}
    for item in items:
        point = _map_point_to_source(
            project,
            typed_anchor=typed_anchor,
            source_anchor=source_anchor,
            at_ms=item.at_ms,
        )
        if point in {0, source_anchor.duration_ms}:
            warnings.append(f"timestamp {point} ms is already a timeline edge")
            continue
        source_points.setdefault(point, []).append(item.id)
    if not source_points:
        return [], warnings
    boundaries = [0, *sorted(source_points), source_anchor.duration_ms]
    ranges: list[NormalizedRange] = []
    for index, (start_ms, end_ms) in enumerate(pairwise(boundaries)):
        boundary_item_ids = source_points.get(start_ms, []) + source_points.get(
            end_ms, []
        )
        ranges.append(
            NormalizedRange(
                start_ms=start_ms,
                end_ms=end_ms,
                item_id=_normalized_split_range_id(
                    source_anchor=source_anchor,
                    index=index,
                    start_ms=start_ms,
                    end_ms=end_ms,
                    boundary_item_ids=boundary_item_ids,
                ),
                metadata={"boundary_item_ids": boundary_item_ids},
            )
        )
    return ranges, warnings


def _normalize_temporal_selection_value(
    selection_type: str, value: Any
) -> dict[str, Any]:
    """Canonicalize while retaining the action's public bounds error code."""

    try:
        return normalize_temporal_value(selection_type, value)
    except ValidationError as exc:
        if _is_temporal_bounds_validation_error(exc):
            raise TimelineError(
                "range_out_of_bounds",
                "The selected timestamp or range extends outside its timeline duration.",
            ) from exc
        raise


def _is_temporal_bounds_validation_error(exc: ValidationError) -> bool:
    """Recognize only the duration checks owned by ``temporal_values``."""

    for error in exc.errors(include_url=False):
        message = str(error.get("msg", ""))
        context_error = str((error.get("ctx") or {}).get("error", ""))
        if "exceeds timeline duration_ms" in message or (
            "exceeds timeline duration_ms" in context_error
        ):
            return True
    return False


def source_range_value(
    source: ResolvedTemporalSource,
    *,
    start_ms: int,
    end_ms: int,
    item_id: str,
    label: str | None,
    metadata: dict[str, Any] | None,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": item_id,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "metadata": dict(metadata or {}),
    }
    if label is not None:
        item["label"] = label
    value = {
        "schema_version": "frisket.timeline_range.v1",
        "timeline": source.lease.anchor.wire_value(),
        "item": item,
    }
    if isinstance(source.lease.anchor, EphemeralMediaClock):
        # The preview reader wraps these items in its display-only envelope;
        # they are never accepted as a canonical temporal column value.
        return value
    return parse_temporal_value("timeline_range", value).model_dump(mode="json")


def staged_receipt_ref(committed: CommittedTemporalClip) -> dict[str, Any]:
    rendered = committed.staged.payload
    return {
        "kind": "temporal_materialized_clip",
        "source": committed.staged.source.lease.source_cell_ref,
        "source_blob_hash": committed.staged.source.lease.blob_hash,
        "derived_blob_hash": committed.blob_hash,
        "requested_start_ms": committed.staged.requested.start_ms,
        "requested_end_ms": committed.staged.requested.end_ms,
        "resolved_start_ms": committed.staged.resolved_start_ms,
        "resolved_end_ms": committed.staged.resolved_end_ms,
        "render": rendered.receipt_metadata(),
    }


def child_sheet_action_outputs(target_name: str, write: Any) -> list[ActionOutput]:
    return [
        ActionOutput(
            kind="sheet",
            name=target_name,
            sheet_id=write.sheet_id,
            ref=dict(write.materialized_sheet_ref),
        ),
        *[
            ActionOutput(
                kind="column",
                name=name,
                sheet_id=write.sheet_id,
                column_id=column_id,
                ref=dict(write.materialized_column_refs[name]),
            )
            for name, column_id in write.column_ids.items()
        ],
        ActionOutput(
            kind="rows",
            name="rows",
            sheet_id=write.sheet_id,
            row_ids=list(write.row_ids),
            ref=dict(write.materialized_rows_ref),
        ),
    ]


def child_sheet_receipt_outputs(target_name: str, write: Any) -> list[ReceiptIO]:
    return [
        ReceiptIO(name=target_name, ref=dict(write.materialized_sheet_ref)),
        *[
            ReceiptIO(
                name=f"column.{name}",
                ref=dict(write.materialized_column_refs[name]),
            )
            for name in write.column_ids
        ],
        ReceiptIO(name="rows", ref=dict(write.materialized_rows_ref)),
    ]


def source_snapshot_ref(source: Any) -> dict[str, Any]:
    return {
        "kind": "source_cell_snapshot",
        **source.lease.source_cell_ref,
        "value_ref": source.value_ref,
        "value_hash": source.value_hash,
        "blob_hash": source.lease.blob_hash,
        "media_kind": source.lease.media_kind,
        "media_type": source.lease.media_type,
        "timeline": source.lease.anchor.wire_value(),
    }


def selection_snapshot_ref(
    source: Any,
    *,
    row_id: int,
    topic_analysis: Any | None,
) -> dict[str, Any]:
    return {
        "kind": "temporal_selection_snapshot",
        "row_id": row_id,
        "column_id": source.selection_column_id,
        "value_ref": source.selection_ref,
        "value_hash": source.selection_hash,
        "value": source.selection_value,
        **(
            {"topic_analysis": topic_analysis.receipt_ref()}
            if topic_analysis is not None
            else {}
        ),
    }


def transcript_snapshot_ref(transcript: Any) -> dict[str, Any]:
    return {
        "kind": "temporal_transcript_snapshot",
        "sheet_id": transcript.sheet_id,
        "row_id": transcript.row_id,
        "column_id": transcript.transcript_column_id,
        "column_name": transcript.transcript_column_name,
        "value_ref": transcript.transcript_value_ref,
        "snapshot_hash": transcript.snapshot_hash,
        "artifact_stable_id": transcript.artifact_stable_id,
        "evidence_link_id": transcript.evidence_link_id,
        "evidence_link_stable_id": transcript.evidence_link_stable_id,
        "transcript_run_id": transcript.transcript_run_id,
        "source_to_transcript_offset_ms": transcript.source_to_transcript_offset_ms,
    }


def resolve_source_timeline(
    project: Any, *, sheet_id: int, row_id: int, column_id: int
) -> TimelineLease:
    try:
        return resolve_timeline(
            project,
            sheet_id=sheet_id,
            row_id=row_id,
            column_id=column_id,
        )
    except TimelineError as exc:
        if exc.code != "timeline_duration_required":
            raise
    # Probe missing immutable media facts once, then resolve the timeline again.
    values = project.get_values(sheet_id, column_id, row_ids=[row_id])
    value = values.get(row_id)
    if not isinstance(value, dict) or not isinstance(value.get("blob"), str):
        raise TimelineError("timeline_not_found", "source cell is not blob-backed")
    from frisket.engine.store.media_blobs import update_blob_metadata

    update_blob_metadata(project, value["blob"], force=True)
    return resolve_timeline(
        project,
        sheet_id=sheet_id,
        row_id=row_id,
        column_id=column_id,
    )


def selection_for_row(
    selection: Any,
    *,
    row_id: int,
    selection_column: Any,
    selection_values: dict[int, Any],
    selection_refs: dict[int, dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any], str]:
    if selection.kind == "column":
        value = selection_values.get(row_id)
        ref = selection_refs.get(row_id)
        if not isinstance(value, dict) or not isinstance(ref, dict):
            raise TimelineError("invalid_temporal_value", "selection cell is empty")
        selection_type = str(selection_column["type"])
        if not column_types.validate_value(selection_type, value):
            raise TimelineError(
                "invalid_temporal_value", "selection cell fails structural validation"
            )
        return dict(value), dict(ref), selection_type
    if selection.kind == "typed_value":
        value = deepcopy(selection.value)
        selection_type = _type_for_value(value)
        selection_ref: dict[str, Any] = {
            "kind": "action_param",
            "field": "selection.value",
        }
        return value, selection_ref, selection_type
    return (
        selection.model_dump(mode="json"),
        {"kind": "action_param", "field": "selection"},
        "draft",
    )


def _type_for_value(value: dict[str, Any]) -> str:
    schema = value.get("schema_version")
    try:
        return _SCHEMA_TO_TYPE[str(schema)]
    except KeyError as exc:
        raise TimelineError(
            "invalid_temporal_value", "selection has an unknown temporal schema"
        ) from exc


def _draft_to_bound_value(
    draft: dict[str, Any], anchor: TimelineAnchor | EphemeralMediaClock
) -> tuple[dict[str, Any], str]:
    kind = draft.get("kind")
    timeline = anchor.wire_value()
    # A direct manual ActionSpec is an authoring boundary: an explicit item ID
    # is a reporter-authored correlation ID, not a security authority, and is
    # preserved. Missing IDs are deterministically host-minted below.
    if kind == "draft_range":
        item = {
            "id": draft.get("id")
            or _manual_draft_item_id(
                anchor=anchor,
                draft_kind=kind,
                item_kind="range",
                index=0,
                item=draft,
            ),
            "start_ms": draft["start_ms"],
            "end_ms": draft["end_ms"],
            "metadata": {},
        }
        if draft.get("label") is not None:
            item["label"] = draft["label"]
        return {
            "schema_version": "frisket.timeline_range.v1",
            "timeline": timeline,
            "item": item,
        }, "timeline_range"
    if kind == "draft_ranges":
        items = []
        for index, raw in enumerate(draft.get("items") or []):
            item = {
                "id": raw.get("id")
                or _manual_draft_item_id(
                    anchor=anchor,
                    draft_kind=kind,
                    item_kind="range",
                    index=index,
                    item=raw,
                ),
                "start_ms": raw["start_ms"],
                "end_ms": raw["end_ms"],
                "metadata": {},
            }
            if raw.get("label") is not None:
                item["label"] = raw["label"]
            items.append(item)
        return {
            "schema_version": "frisket.timeline_ranges.v1",
            "timeline": timeline,
            "items": items,
        }, "timeline_ranges"
    if kind == "draft_points":
        items = []
        for index, raw in enumerate(draft.get("items") or []):
            item = {
                "id": raw.get("id")
                or _manual_draft_item_id(
                    anchor=anchor,
                    draft_kind=kind,
                    item_kind="point",
                    index=index,
                    item=raw,
                ),
                "at_ms": raw["at_ms"],
                "metadata": {},
            }
            if raw.get("label") is not None:
                item["label"] = raw["label"]
            items.append(item)
        return {
            "schema_version": "frisket.timeline_points.v1",
            "timeline": timeline,
            "items": items,
        }, "timeline_points"
    raise TimelineError("invalid_temporal_value", "unknown draft selection kind")


def bind_draft_selection(
    selection_value: dict[str, Any], anchor: TimelineAnchor
) -> tuple[dict[str, Any], str]:
    return _draft_to_bound_value(selection_value, anchor)


def _map_range_to_source(
    project: Any,
    *,
    typed_anchor: TimelineAnchor | EphemeralMediaClock,
    source_anchor: TimelineAnchor | EphemeralMediaClock,
    start_ms: int,
    end_ms: int,
    item_id: str,
    label: str | None,
    metadata: dict[str, Any],
) -> NormalizedRange:
    if (
        typed_anchor.fingerprint == source_anchor.fingerprint
        and typed_anchor.duration_ms == source_anchor.duration_ms
    ):
        return NormalizedRange(start_ms, end_ms, item_id, label, metadata)
    typed = root_clock_extent(
        project,
        typed_anchor,
        start_ms=start_ms,
        end_ms=end_ms,
    )
    target = root_clock_extent(project, source_anchor)
    if (
        typed.root_artifact_fingerprint != target.root_artifact_fingerprint
        or typed.root_start_ms < target.root_start_ms
        or typed.root_end_ms > target.root_end_ms
    ):
        raise TimelineError(
            "selection_unmappable", "selection does not map completely to source"
        )
    return NormalizedRange(
        typed.root_start_ms - target.root_start_ms,
        typed.root_end_ms - target.root_start_ms,
        item_id,
        label,
        metadata,
    )


def _map_point_to_source(
    project: Any,
    *,
    typed_anchor: TimelineAnchor | EphemeralMediaClock,
    source_anchor: TimelineAnchor | EphemeralMediaClock,
    at_ms: int,
) -> int:
    if (
        typed_anchor.fingerprint == source_anchor.fingerprint
        and typed_anchor.duration_ms == source_anchor.duration_ms
    ):
        return at_ms
    typed = root_clock_extent(project, typed_anchor)
    target = root_clock_extent(project, source_anchor)
    if typed.root_artifact_fingerprint != target.root_artifact_fingerprint:
        raise TimelineError("selection_unmappable", "point belongs to another timeline")
    root_at = typed.root_start_ms + at_ms
    if root_at < target.root_start_ms or root_at > target.root_end_ms:
        raise TimelineError("selection_unmappable", "point is outside source timeline")
    return root_at - target.root_start_ms


def media_kind(source: ResolvedTemporalSource) -> str:
    return source.lease.media_kind


def _manual_draft_item_id(
    *,
    anchor: TimelineAnchor | EphemeralMediaClock,
    draft_kind: str,
    item_kind: str,
    index: int,
    item: dict[str, Any],
) -> str:
    """Mint a stable source-scoped identity for an unkeyed manual draft item."""

    digest = canonical_json_hash(
        {
            "schema_version": "frisket.manual_draft_item_id.v1",
            "timeline": anchor.wire_value(),
            "draft_kind": draft_kind,
            "item_kind": item_kind,
            "item_index": index,
            "item": {key: value for key, value in item.items() if key != "id"},
        }
    )
    prefix = "tp" if item_kind == "point" else "tr"
    return f"{prefix}_{digest.removeprefix('sha256:')}"


def _normalized_split_range_id(
    *,
    source_anchor: TimelineAnchor | EphemeralMediaClock,
    index: int,
    start_ms: int,
    end_ms: int,
    boundary_item_ids: list[str],
) -> str:
    """Mint a stable identity for a range derived from point boundaries."""

    digest = canonical_json_hash(
        {
            "schema_version": "frisket.normalized_split_range_id.v1",
            "timeline": source_anchor.wire_value(),
            "range_index": index,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "boundary_item_ids": boundary_item_ids,
        }
    )
    return f"tr_{digest.removeprefix('sha256:')}"


def action_error(
    action_kind: str,
    code: str,
    message: str,
    *,
    field: str | None = None,
    details: dict[str, Any] | None = None,
) -> ActionError:
    """The one ActionError constructor the temporal executors share."""

    return ActionError(
        code=code,
        message=message,
        action_kind=action_kind,
        field=field,
        details=details or {},
    )


def duplicate_target_sheet_error(
    project: Any,
    target_name: str,
    *,
    action_kind: str,
) -> ActionError | None:
    row = project.db.execute(
        "SELECT id, hidden FROM sheets WHERE name=?",
        (target_name,),
    ).fetchone()
    if row is None:
        return None
    return action_error(
        action_kind,
        "duplicate_sheet_name",
        f"{action_kind} target sheet name already exists",
        field="params.target_sheet_name",
        details={
            "sheet_id": int(row["id"]),
            "name": target_name,
            "hidden": bool(row["hidden"]),
        },
    )


__all__ = [
    "CommittedTemporalClip",
    "action_error",
    "duplicate_target_sheet_error",
    "media_kind",
    "selection_for_row",
    "CoreTemporalMediaMaterializer",
    "NormalizedRange",
    "ResolvedTemporalSource",
    "StagedTemporalClip",
    "child_sheet_action_outputs",
    "child_sheet_receipt_outputs",
    "prepare_bound_sources",
    "revalidate_action_sources",
    "selection_snapshot_ref",
    "source_snapshot_ref",
    "source_range_value",
    "staged_receipt_ref",
    "transcript_snapshot_ref",
]
