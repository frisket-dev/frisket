"""Actual-argument transcript reads and atomic occurrence-bound projection."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from functools import partial
from typing import Any
from pydantic import ValidationError, TypeAdapter

from frisket.actions.transcript_types import (
    TranscriptColumn,
    TranscriptSelection,
    TranscriptProjectionValue,
    TranscriptAnnotationValue,
    validate_transcript_selection_scope,
)
from frisket.actions.types import (
    DynamicOutput,
    DynamicTableResult,
    TableColumn,
    TableRow,
    RowSource,
    SheetRows,
    TableError,
)
from frisket.contracts.action import ActionError, ReceiptIO, ReceiptEvidence
from frisket.engine.executor.embedding_read import TableReadRefused
from frisket.engine.executor.map_rows_action import (
    validate_typed_project_references,
    TypedMapRowsPlanError,
)
from frisket.engine.executor.temporal_materialization import (
    NormalizedRange,
    action_error,
    bind_draft_selection,
    normalize_selection,
    selection_for_row,
    selection_snapshot_ref,
    transcript_snapshot_ref,
)
from frisket.engine.executor.temporal_annotations import (
    ResolvedTemporalAnnotation,
    annotation_intersects_source_range,
    assign_temporal_annotation_output_names,
    project_resolved_temporal_annotation,
    project_resolved_temporal_annotation_items,
    resolve_compatible_temporal_annotations,
    revalidate_temporal_annotation,
)
from frisket.engine.executor.temporal_finder_provenance import (
    ResolvedTopicAnalysisSidecar,
    resolve_topic_analysis_sidecar,
    resolve_topic_section_unit_ids,
)
from frisket.engine.executor.temporal_transcripts import (
    ResolvedTranscriptSource,
    persist_projected_transcript_evidence,
    resolve_timestamped_transcript,
    revalidate_transcript,
)
from frisket.engine.store import Project
from frisket.engine.store.artifact_timeline import (
    EphemeralMediaClock,
    TimelineAnchor,
    TimelineError,
    canonical_json_hash,
    resolve_artifact_timeline,
    write_rate1_timeline_segment,
)
from frisket.engine.store.cell_writes import (
    BaseCellWrite,
    create_base_cell_producer,
    initialize_base_cells,
)
from frisket.engine.store.evidence import record_source_artifact
from frisket.engine.store.transcript_projection import (
    TranscriptProjection,
    project_transcript_segments,
    whole_intersecting_transcript_chunks,
)
from frisket.features.temporal_values import normalize_temporal_value
from frisket.engine.executor.temporal_preview import PreviewTemporalValue

ACTION_KIND = "derive.transcript_segments"


@dataclass(frozen=True)
class TranscriptSelectionSource:
    transcript: ResolvedTranscriptSource
    anchor: TimelineAnchor
    selection_value: dict[str, Any]
    selection_ref: dict[str, Any]
    selection_hash: str
    selection_column_id: int | None
    annotations: tuple[ResolvedTemporalAnnotation, ...]
    topic_analysis: ResolvedTopicAnalysisSidecar | None
    ranges: tuple[NormalizedRange, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class TranscriptSegment:
    source: TranscriptSelectionSource
    requested: NormalizedRange
    locked: NormalizedRange
    projection: TranscriptProjection


@dataclass(frozen=True)
class TranscriptSegmentsPlan:
    sources: tuple[TranscriptSelectionSource, ...]
    segments: tuple[TranscriptSegment, ...]


_error = partial(action_error, ACTION_KIND)


def _selection_for_row(
    selection: TranscriptSelection,
    *,
    row_id: int,
    anchor: TimelineAnchor,
    selection_column: Any | None,
    selection_values: dict[int, Any],
    selection_refs: dict[int, dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any], str, int | None]:
    """The shared selection resolver, plus this action's two extras: draft
    selections bind eagerly against the transcript's timeline anchor, and a
    column selection also reports its column id."""
    value, ref, type_name = selection_for_row(
        selection,
        row_id=row_id,
        selection_column=selection_column,
        selection_values=selection_values,
        selection_refs=selection_refs,
    )
    if selection.kind == "column":
        assert selection_column is not None
        return value, ref, type_name, int(selection_column["id"])
    ref = {"kind": "transcript_selection_argument"}
    if selection.kind == "typed_value":
        return value, ref, type_name, None
    # Repetition is selection intent, not part of a timestamp item's identity.
    value.pop("repeat_for_rows", None)
    value, type_name = bind_draft_selection(value, anchor)
    return value, ref, type_name, None


def _lock_range_to_chunks(
    transcript: ResolvedTranscriptSource,
    item: NormalizedRange,
    *,
    selected_ids: frozenset[str] | None = None,
) -> tuple[NormalizedRange, tuple[dict[str, Any], ...]]:
    if selected_ids is None:
        intersecting, start_ms, end_ms = whole_intersecting_transcript_chunks(
            transcript.spans,
            source_start_ms=item.start_ms,
            source_end_ms=item.end_ms,
        )
    else:
        intersecting = tuple(
            span for span in transcript.spans if str(span["stable_id"]) in selected_ids
        )
        if {str(span["stable_id"]) for span in intersecting} != selected_ids:
            raise TimelineError(
                "selection_unmappable",
                "locked topic chunks no longer match the producing transcript",
            )
        start_ms = min(
            item.start_ms,
            *(int(span["start_ms"]) for span in intersecting),
        )
        end_ms = max(
            item.end_ms,
            *(int(span["end_ms"]) for span in intersecting),
        )
    if not intersecting:
        raise TimelineError(
            "invalid_range",
            "a selected range does not intersect timestamped transcript evidence",
        )
    metadata = dict(item.metadata or {})
    if start_ms != item.start_ms or end_ms != item.end_ms:
        metadata["requested_range"] = {
            "start_ms": item.start_ms,
            "end_ms": item.end_ms,
        }
        metadata["boundary_policy"] = "include_intersecting_transcript_chunks"
    return (
        NormalizedRange(
            start_ms=start_ms,
            end_ms=end_ms,
            item_id=item.item_id,
            label=item.label,
            metadata=metadata,
        ),
        tuple(dict(span) for span in intersecting),
    )


def resolve_transcript_segments_plan(
    project: Project,
    *,
    scope: SheetRows,
    source: TranscriptColumn,
    selection: TranscriptSelection,
) -> TranscriptSegmentsPlan | ActionError:
    validate_transcript_selection_scope(selection, scope)
    sheet_id = scope.sheet_id
    sheet = project.db.execute(
        "SELECT id FROM sheets WHERE id=? AND hidden=0", (sheet_id,)
    ).fetchone()
    if sheet is None:
        return _error("invalid_input_ref", "source sheet is not visible")
    try:
        references = validate_typed_project_references(
            project, sheet_id, (source, selection)
        )
    except TypedMapRowsPlanError as exc:
        return _error(exc.code, str(exc), details=exc.details)
    source_column_id = references.source_column_ids[source.root]
    selection_name = selection.column.root if selection.kind == "column" else None
    selection_column_id = (
        references.source_column_ids[selection_name] if selection_name else None
    )
    row_ids = (
        list(scope.row_ids)
        if scope.row_ids is not None
        else project.visible_row_ids(sheet_id)
    )
    if not row_ids or set(project.visible_row_ids(sheet_id, row_ids)) != set(row_ids):
        return _error("invalid_input_ref", "source rows are not visible")

    selection_column = None
    selection_values: dict[int, Any] = {}
    selection_refs: dict[int, dict[str, Any]] = {}
    if selection_column_id is not None:
        selection_column = {
            "id": selection_column_id,
            "type": references.source_column_types[selection_name],
        }
        selection_values, selection_refs = project.get_values_with_refs(
            sheet_id,
            selection_column_id,
            row_ids=row_ids,
        )

    sources: list[TranscriptSelectionSource] = []
    segments: list[TranscriptSegment] = []
    try:
        for row_id in row_ids:
            transcript = resolve_timestamped_transcript(
                project,
                sheet_id=sheet_id,
                row_id=row_id,
                column_id=source_column_id,
            )
            if transcript is None:
                return _error(
                    "invalid_input_ref",
                    "source transcript lacks exact current timestamp evidence",
                    field="params",
                    details={"row_id": row_id},
                )
            anchor = resolve_artifact_timeline(project, transcript.artifact_id)
            value, ref, type_name, selection_column_id = _selection_for_row(
                selection,
                row_id=row_id,
                anchor=anchor,
                selection_column=selection_column,
                selection_values=selection_values,
                selection_refs=selection_refs,
            )
            normalized, warnings = normalize_selection(
                project,
                value,
                selection_type=type_name,
                source_anchor=anchor,
                purpose="split",
            )
            ranges = tuple(normalized)
            if not ranges:
                return _error(
                    "timestamps_required",
                    "no transcript segment ranges remain after normalization",
                    field="params.selection",
                    details={"row_id": row_id},
                )
            topic_analysis = (
                resolve_topic_analysis_sidecar(
                    project,
                    sheet_id=sheet_id,
                    row_id=row_id,
                    selection_column_id=selection_column_id,
                )
                if selection_column_id is not None
                else None
            )
            if (
                topic_analysis is not None
                and topic_analysis.payload["transcript_evidence_id"]
                != transcript.evidence_link_stable_id
            ):
                return _error(
                    "timeline_mismatch",
                    "topic ranges were produced from a different transcript",
                    field="params.selection",
                    details={
                        "row_id": row_id,
                        "selected_transcript_evidence_id": (
                            transcript.evidence_link_stable_id
                        ),
                        "range_transcript_evidence_id": topic_analysis.payload[
                            "transcript_evidence_id"
                        ],
                    },
                )
            locked_ids_by_item = (
                resolve_topic_section_unit_ids(topic_analysis, value)
                if topic_analysis is not None
                else None
            )
            source = TranscriptSelectionSource(
                transcript=transcript,
                anchor=anchor,
                selection_value=value,
                selection_ref=ref,
                selection_hash=canonical_json_hash(value),
                selection_column_id=selection_column_id,
                annotations=resolve_compatible_temporal_annotations(
                    project,
                    sheet_id=sheet_id,
                    row_id=row_id,
                    source_anchor=anchor,
                    excluded_column_ids=(
                        (selection_column_id,)
                        if selection_column_id is not None
                        else ()
                    ),
                ),
                topic_analysis=topic_analysis,
                ranges=ranges,
                warnings=tuple(warnings),
            )
            sources.append(source)
            for item in ranges:
                locked, selected_spans = _lock_range_to_chunks(
                    transcript,
                    item,
                    selected_ids=(
                        locked_ids_by_item[item.item_id]
                        if locked_ids_by_item is not None
                        else None
                    ),
                )
                projection = project_transcript_segments(
                    selected_spans,
                    source_start_ms=locked.start_ms,
                    source_end_ms=locked.end_ms,
                    source_artifact_stable_id=transcript.artifact_stable_id,
                    transcript_run_id=transcript.transcript_run_id,
                    evidence_link_stable_id=transcript.evidence_link_stable_id,
                    language=transcript.language,
                )
                if not projection.segments:
                    raise TimelineError(
                        "invalid_range",
                        "a selected range produced no transcript segments",
                    )
                segments.append(
                    TranscriptSegment(
                        source=source,
                        requested=item,
                        locked=locked,
                        projection=projection,
                    )
                )
    except TimelineError as exc:
        return _error(exc.code, exc.message, field="params.selection")
    except (KeyError, TypeError, ValueError, ValidationError) as exc:
        return _error(
            "invalid_temporal_value",
            "transcript selection is malformed",
            field="params.selection",
            details={"error": str(exc)},
        )
    return TranscriptSegmentsPlan(sources=tuple(sources), segments=tuple(segments))


def _revalidate_plan(project: Project, plan: TranscriptSegmentsPlan) -> None:
    for source in plan.sources:
        revalidate_transcript(project, source.transcript)
        if source.selection_column_id is not None:
            values, refs = project.get_values_with_refs(
                source.transcript.sheet_id,
                source.selection_column_id,
                row_ids=[source.transcript.row_id],
            )
            if (
                canonical_json_hash(values.get(source.transcript.row_id))
                != source.selection_hash
                or refs.get(source.transcript.row_id) != source.selection_ref
            ):
                raise TimelineError(
                    "stale_input", "temporal selection changed during execution"
                )
            if source.topic_analysis is not None:
                current = resolve_topic_analysis_sidecar(
                    project,
                    sheet_id=source.transcript.sheet_id,
                    row_id=source.transcript.row_id,
                    selection_column_id=source.selection_column_id,
                )
                if (
                    current is None
                    or current.receipt_ref() != source.topic_analysis.receipt_ref()
                ):
                    raise TimelineError(
                        "stale_input", "topic analysis changed during execution"
                    )
        for annotation in source.annotations:
            revalidate_temporal_annotation(project, annotation)


def _source_range_value(item: TranscriptSegment) -> dict[str, Any]:
    value = {
        "schema_version": "frisket.timeline_range.v1",
        "timeline": item.source.anchor.wire_value(),
        "item": {
            "id": item.locked.item_id,
            "start_ms": item.locked.start_ms,
            "end_ms": item.locked.end_ms,
            "metadata": dict(item.locked.metadata or {}),
        },
    }
    if item.locked.label is not None:
        value["item"]["label"] = item.locked.label
    return normalize_temporal_value("timeline_range", value)


def _projectable_annotations(
    plan: TranscriptSegmentsPlan,
) -> tuple[ResolvedTemporalAnnotation, ...]:
    by_column: dict[int, ResolvedTemporalAnnotation] = {}
    for segment in plan.segments:
        for annotation in segment.source.annotations:
            if annotation_intersects_source_range(
                annotation,
                source_start_ms=segment.locked.start_ms,
                source_end_ms=segment.locked.end_ms,
            ):
                by_column.setdefault(annotation.column_id, annotation)
    return tuple(by_column.values())


def _annotation_output_names(
    plan: TranscriptSegmentsPlan,
    *,
    output_name: str,
) -> dict[int, str]:
    return assign_temporal_annotation_output_names(
        _projectable_annotations(plan),
        used_names={output_name, "source_range"},
    )


def _receipt_inputs(plan: TranscriptSegmentsPlan) -> list[ReceiptIO]:
    values: list[ReceiptIO] = []
    for source in plan.sources:
        transcript = source.transcript
        values.extend(
            [
                ReceiptIO(
                    name=f"transcript.{transcript.row_id}",
                    ref=transcript_snapshot_ref(transcript),
                ),
                ReceiptIO(
                    name=f"selection.{transcript.row_id}",
                    ref=selection_snapshot_ref(
                        source,
                        row_id=transcript.row_id,
                        topic_analysis=source.topic_analysis,
                    ),
                ),
            ]
        )
        values.extend(
            ReceiptIO(
                name=f"annotation.{transcript.row_id}.{annotation.column_id}",
                ref=annotation.input_ref(),
            )
            for annotation in source.annotations
        )
    return values


class AdmittedTranscriptReader:
    def __init__(self, project: Project, *, scope: SheetRows, action_kind: str):
        self.project = project
        self.scope = scope
        self.action_kind = action_kind
        self.parent_sheet_id = scope.sheet_id
        self.sources: set[RowSource] = set()
        self.facts: list[dict[str, Any]] = []
        self.plans: list[TranscriptSegmentsPlan] = []
        self.projections: dict[
            TranscriptProjectionValue, tuple[TranscriptSegment, RowSource]
        ] = {}
        self.annotations: dict[
            TranscriptAnnotationValue, tuple[TranscriptProjectionValue, int]
        ] = {}
        self.occurrences: list[dict[str, Any]] = []
        self.closed = False

    def close(self):
        self.closed = True

    def read(
        self, source: TranscriptColumn, selection: TranscriptSelection
    ) -> DynamicTableResult:
        if self.closed:
            raise TableError("invalid_input_ref", "transcript reader is closed")
        if not isinstance(source, TranscriptColumn):
            raise TableError(
                "invalid_input_ref", "expected a transcript column reference"
            )
        # Validate actual call values, including compatible models from sibling
        # temporal actions. Such models cannot bypass literal acknowledgement.
        selection = TypeAdapter(TranscriptSelection).validate_python(
            selection.model_dump(mode="python")
            if hasattr(selection, "model_dump")
            else selection
        )
        validate_transcript_selection_scope(selection, self.scope)
        with self.project.read_snapshot() as project:
            plan = resolve_transcript_segments_plan(
                project,
                scope=self.scope,
                source=source,
                selection=selection,
            )
        if isinstance(plan, ActionError):
            raise TableReadRefused(
                plan.model_copy(update={"action_kind": self.action_kind})
            )
        self.plans.append(plan)
        self.facts.extend(entry.ref for entry in _receipt_inputs(plan))
        names = _annotation_output_names(plan, output_name="transcript")
        annotations = {item.column_id: item for item in _projectable_annotations(plan)}
        schema = (
            TableColumn("transcript", "timestamped_transcript"),
            TableColumn("source_range", "timeline_range"),
            *(
                TableColumn(name, annotations[column_id].type_name)
                for column_id, name in names.items()
            ),
        )
        rows = []
        for item in plan.segments:
            source_token = RowSource(
                sheet_id=item.source.transcript.sheet_id,
                row_id=item.source.transcript.row_id,
            )
            self.sources.add(source_token)
            handle = TranscriptProjectionValue()
            self.projections[handle] = (item, source_token)
            values = {"transcript": handle, "source_range": _source_range_value(item)}
            by_column = {
                annotation.column_id: annotation
                for annotation in item.source.annotations
            }
            for column_id, name in names.items():
                if column_id in by_column:
                    annotation_handle = TranscriptAnnotationValue()
                    self.annotations[annotation_handle] = (handle, column_id)
                    values[name] = annotation_handle
                else:
                    values[name] = None
            rows.append(
                TableRow(
                    output=DynamicOutput(values),
                    sources=(source_token,),
                    parent=source_token,
                )
            )
        return DynamicTableResult(
            schema=schema,
            rows=tuple(rows),
            warnings=tuple(
                dict.fromkeys(
                    warning for source in plan.sources for warning in source.warnings
                )
            ),
        )

    def lower_row(self, values, fields, sources, parent, output_names):
        lowered = dict(values)
        projected = {}
        annotations = {}
        for field in fields:
            value = values[field.key]
            name = output_names.get(field.key, field.key)
            if isinstance(value, TranscriptProjectionValue):
                admitted = self.projections.get(value)
                if admitted is None:
                    raise ValueError("foreign transcript projection")
                item, source = admitted
                if sources != (source,) or parent is not source:
                    raise ValueError(
                        "transcript projection requires its exact admitted parent"
                    )
                if field.column_type != "timestamped_transcript":
                    raise ValueError(
                        "transcript projection requires a timestamped_transcript output"
                    )
                if value in projected:
                    raise ValueError(
                        "one transcript projection cannot define two clocks in one row"
                    )
                projected[value] = name
                lowered[field.key] = item.projection.text
            elif isinstance(value, TranscriptAnnotationValue):
                admitted = self.annotations.get(value)
                if admitted is None:
                    raise ValueError("foreign transcript annotation projection")
                handle, column_id = admitted
                item, _source = self.projections[handle]
                annotation = next(
                    item
                    for item in item.source.annotations
                    if item.column_id == column_id
                )
                if field.column_type != annotation.type_name:
                    raise ValueError("transcript annotation output type changed")
                annotations.setdefault(handle, {}).setdefault(column_id, []).append(
                    name
                )
                lowered[field.key] = None
        if set(annotations) - set(projected):
            raise ValueError(
                "transcript annotations require their same-row transcript projection"
            )
        self.occurrences.append(
            {
                "projections": tuple(
                    (handle, name, annotations.get(handle, {}))
                    for handle, name in projected.items()
                )
            }
        )
        return lowered

    def revalidate(self):
        for plan in self.plans:
            _revalidate_plan(self.project, plan)

    def preview_row(self, row, ordinal):
        """Lower validated occurrences for display without publishing their clocks."""
        values = dict(row)
        for handle, _name, annotation_names in self.occurrences[ordinal]["projections"]:
            item, _source = self.projections[handle]
            clock = EphemeralMediaClock(
                uuid.uuid4().hex,
                canonical_json_hash(
                    {
                        "source": item.source.anchor.fingerprint,
                        "start_ms": item.locked.start_ms,
                        "end_ms": item.locked.end_ms,
                    }
                ),
                item.locked.end_ms - item.locked.start_ms,
            )
            for annotation in item.source.annotations:
                names = annotation_names.get(annotation.column_id)
                if not names:
                    continue
                projection = project_resolved_temporal_annotation_items(
                    annotation,
                    source_start_ms=item.locked.start_ms,
                    source_end_ms=item.locked.end_ms,
                )
                if projection is not None:
                    for name in names:
                        values[name] = PreviewTemporalValue(
                            annotation.type_name, clock.wire_value(), projection
                        )
        return values

    def publication_evidence(self, write, rows, *, receipt_id):
        evidence = []
        producer_id = create_base_cell_producer(
            self.project.db, stage_id=f"op:{write.op_id}", op_id=write.op_id
        )
        for occurrence, output_row_id, row in zip(
            self.occurrences, write.row_ids, rows, strict=True
        ):
            for handle, name, annotation_names in occurrence["projections"]:
                item, _source = self.projections[handle]
                evidence.extend(
                    _publish_projection(
                        self.project,
                        item,
                        write,
                        output_row_id=output_row_id,
                        output_column_id=write.column_ids[name],
                        annotation_names=annotation_names,
                        receipt_id=receipt_id,
                        action_kind=self.action_kind,
                        row=row,
                        producer_id=producer_id,
                    )
                )
        return evidence

    def generated_columns(self):
        return {
            name
            for occurrence in self.occurrences
            for _handle, name, _annotations in occurrence["projections"]
        }

    def initial_cell_values(self, row, ordinal):
        # Annotation clocks exist only after this output occurrence is assigned
        # an artifact. Leave those cells absent until their authoritative INSERT.
        projected = {
            name
            for _handle, _column, annotations in self.occurrences[ordinal][
                "projections"
            ]
            for names in annotations.values()
            for name in names
        }
        return {name: value for name, value in row.items() if name not in projected}


def _publish_projection(
    project,
    item,
    write,
    *,
    output_row_id,
    output_column_id,
    annotation_names,
    receipt_id,
    action_kind,
    row,
    producer_id,
):
    evidence = []
    duration_ms = item.locked.end_ms - item.locked.start_ms
    artifact = record_source_artifact(
        project,
        artifact_kind="transcript",
        media_type="text/plain",
        duration_ms=duration_ms,
        source_sheet_id=write.sheet_id,
        source_row_id=output_row_id,
        source_column_id=output_column_id,
        metadata={"producer_action": action_kind},
    )
    mapping = write_rate1_timeline_segment(
        project,
        derived_artifact_id=int(artifact["id"]),
        source_artifact_id=item.source.transcript.artifact_id,
        source_start_ms=item.locked.start_ms,
        source_end_ms=item.locked.end_ms,
        precision="exact",
        receipt_id=receipt_id,
        params={
            "item_id": item.locked.item_id,
            "boundary_policy": "include_intersecting_transcript_chunks",
        },
    )
    derived_anchor = resolve_artifact_timeline(project, int(artifact["id"]))
    annotation_cells = []
    for annotation in item.source.annotations:
        target_names = annotation_names.get(annotation.column_id)
        if not target_names:
            continue
        annotation_projection = project_resolved_temporal_annotation(
            annotation,
            source_start_ms=item.locked.start_ms,
            source_end_ms=item.locked.end_ms,
            derived_timeline=derived_anchor,
        )
        if annotation_projection is None:
            continue
        for annotation_name in target_names:
            annotation_column_id = write.column_ids[annotation_name]
            row[annotation_name] = annotation_projection.value
            annotation_cells.append(
                BaseCellWrite(
                    row_id=output_row_id,
                    column_id=annotation_column_id,
                    value=annotation_projection.value,
                )
            )
            evidence.append(
                ReceiptEvidence(
                    ref={
                        "kind": "temporal_annotation_projection",
                        "source": annotation.input_ref(),
                        "output": {
                            "sheet_id": write.sheet_id,
                            "row_id": output_row_id,
                            "column_id": annotation_column_id,
                            "column_name": annotation_name,
                            "type": annotation.type_name,
                            "value_hash": canonical_json_hash(
                                annotation_projection.value
                            ),
                        },
                        "projection": annotation_projection.receipt_metadata(),
                    },
                    retention="materialized",
                )
            )
    initialize_base_cells(project.db, producer_id=producer_id, cells=annotation_cells)
    projected = persist_projected_transcript_evidence(
        project,
        transcript=item.source.transcript,
        projection=item.projection,
        derived_artifact_id=int(artifact["id"]),
        output_sheet_id=write.sheet_id,
        output_row_id=output_row_id,
        transcript_column_id=output_column_id,
        receipt_id=receipt_id,
        op_id=write.op_id,
    )
    evidence.append(
        ReceiptEvidence(
            ref={
                "kind": "derived_transcript_timeline",
                "artifact_id": int(artifact["id"]),
                "artifact_stable_id": str(artifact["stable_id"]),
                "timeline_segment_id": int(mapping.id),
                "source_artifact_id": item.source.transcript.artifact_id,
                "source_artifact_stable_id": (
                    item.source.transcript.artifact_stable_id
                ),
                "source_start_ms": item.locked.start_ms,
                "source_end_ms": item.locked.end_ms,
                "output": {
                    "sheet_id": write.sheet_id,
                    "row_id": output_row_id,
                    "column_id": output_column_id,
                },
            },
            retention="materialized",
        )
    )
    if projected is not None:
        evidence.append(ReceiptEvidence(ref=projected, retention="materialized"))

    return evidence
