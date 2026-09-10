"""Deterministic, action-atomic media Split executor.

Split has two deliberately separate phases.  Source/selection/transcript facts
are snapshotted and every media file is rendered in caller-owned scratch space
without holding a SQLite transaction.  Only after every output is staged and
probed does one transaction publish the blobs, child sheet, artifact timeline
rows, projected transcript evidence, and completed receipt.

The admitted TemporalMediaReader reuses these domain operations; the typed
create_sheet host owns reservation, transaction, and receipt publication.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from typing import Any

from frisket.actions.temporal_types import TemporalMediaColumn, TranscriptSelection
from frisket.actions.types import SheetRows

from frisket.contracts.action import (
    ActionError,
    ReceiptEvidence,
    ReceiptIO,
)
from frisket.engine.executor.temporal_materialization import (
    CoreTemporalMediaMaterializer,
    NormalizedRange,
    ResolvedTemporalMediaSource,
    ResolvedTemporalSource,
    StagedTemporalClip,
    action_error,
    media_kind,
    prepare_bound_sources,
    resolve_temporal_media_source,
    selection_snapshot_ref,
    source_snapshot_ref,
    source_range_value,
    staged_receipt_ref,
    transcript_snapshot_ref,
    validate_staged_temporal_clip,
)
from frisket.engine.executor.temporal_annotations import (
    ResolvedTemporalAnnotation,
    annotation_intersects_source_range,
    project_resolved_temporal_annotation,
)
from frisket.engine.executor.temporal_finder_provenance import (
    resolve_topic_section_unit_ids,
)
from frisket.engine.executor.temporal_transcripts import (
    ResolvedTranscriptSource,
    persist_projected_transcript_evidence,
    project_transcript,
)
from frisket.engine.store import Project
from frisket.engine.store.artifact_timeline import (
    TimelineError,
    canonical_json_hash,
    resolve_artifact_timeline,
)
from frisket.engine.store.cell_writes import BaseCellWrite, initialize_base_cells
from frisket.engine.store.media_splice import MediaSpliceError, stage_media_splice
from frisket.engine.sandbox.media_sync import run_media_sync
from frisket.engine.store.transcript_projection import TranscriptProjection


ACTION_KIND = "derive.temporal_segments"


StageTemporalRange = Callable[
    [Project, ResolvedTemporalSource, NormalizedRange, Path], Any
]


@dataclass(frozen=True)
class SplitSourcePlan(ResolvedTemporalMediaSource):
    topic_span_ids_by_item: dict[str, frozenset[str]]


@dataclass(frozen=True)
class TemporalSplitPlan:
    sources: tuple[SplitSourcePlan, ...]
    output_count: int

    @property
    def source_snapshots(self) -> tuple[ResolvedTemporalSource, ...]:
        return tuple(item.source for item in self.sources)

    @property
    def warnings(self) -> tuple[str, ...]:
        return tuple(warning for item in self.sources for warning in item.warnings)


@dataclass(frozen=True)
class StagedSplitClip:
    clip: StagedTemporalClip
    transcripts: tuple[tuple[ResolvedTranscriptSource, TranscriptProjection], ...]
    annotations: tuple[ResolvedTemporalAnnotation, ...]

    @property
    def warnings(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                warning
                for _transcript, projection in self.transcripts
                for warning in projection.warnings
            )
        )


# This module's ActionError construction and duplicate-target gate are the
# shared temporal ones, pinned to this action kind.
_error = partial(action_error, ACTION_KIND)


def _empty_selection_error(
    sources: tuple[ResolvedTemporalSource, ...],
    *,
    row_id: int | None = None,
) -> ActionError:
    schemas = {
        str(source.selection_value.get("schema_version") or "") for source in sources
    }
    details = {"row_id": row_id} if row_id is not None else None
    if schemas and schemas <= {
        "frisket.timeline_point.v1",
        "frisket.timeline_points.v1",
    }:
        return _error(
            "timestamps_required",
            "No effective internal timestamps remain after normalization.",
            details=details,
        )
    return _error(
        "invalid_range",
        "No ranges were selected for Split.",
        details=details,
    )


def resolve_temporal_split_plan(
    project: Project,
    *,
    scope: SheetRows,
    source: TemporalMediaColumn,
    selection: TranscriptSelection,
    source_resolver=None,
) -> TemporalSplitPlan | ActionError:
    """Resolve normalized ranges and transcript snapshots."""

    resolved = prepare_bound_sources(
        project,
        scope=scope,
        source=source,
        selection=selection,
        action_kind=ACTION_KIND,
        purpose="split",
        source_resolver=source_resolver,
    )
    if isinstance(resolved, ActionError):
        return resolved

    empty_source = next((source for source in resolved if not source.ranges), None)
    if empty_source is not None:
        return _empty_selection_error(
            (empty_source,),
            row_id=empty_source.row_id,
        )

    output_count = sum(len(source.ranges) for source in resolved)
    if output_count == 0:
        return _empty_selection_error(resolved)

    source_plans: list[SplitSourcePlan] = []
    for source in resolved:
        try:
            media_source = resolve_temporal_media_source(project, source)
        except TimelineError as exc:
            return _error(
                exc.code,
                exc.message,
                details={"row_id": source.row_id},
            )
        topic_span_ids_by_item: dict[str, frozenset[str]] = {}
        if media_source.topic_analysis is not None:
            producing_id = str(
                media_source.topic_analysis.payload["transcript_evidence_id"]
            )
            producing_transcript = next(
                transcript
                for transcript in media_source.transcripts
                if transcript.evidence_link_stable_id == producing_id
            )
            locked_ids = resolve_topic_section_unit_ids(
                media_source.topic_analysis,
                source.selection_value,
            )
            if locked_ids is not None:
                available_ids = {
                    str(span["stable_id"]) for span in producing_transcript.spans
                }
                selected_ids = set().union(*locked_ids.values())
                if not selected_ids <= available_ids:
                    return _error(
                        "selection_unmappable",
                        "locked topic chunks no longer match the producing transcript",
                        details={"row_id": source.row_id},
                    )
                topic_span_ids_by_item = locked_ids
        source_plans.append(
            SplitSourcePlan(
                source=source,
                transcripts=media_source.transcripts,
                annotations=media_source.annotations,
                topic_analysis=media_source.topic_analysis,
                topic_span_ids_by_item=topic_span_ids_by_item,
                warnings=media_source.warnings,
            )
        )
    return TemporalSplitPlan(
        sources=tuple(source_plans),
        output_count=output_count,
    )


def _staged_output_path(
    scratch: Path,
    *,
    ordinal: int,
    source: ResolvedTemporalSource,
) -> Path:
    suffix = ".flac" if media_kind(source) == "audio" else ".mp4"
    return scratch / f"clip-{ordinal:04d}{suffix}"


def _project_staged_clip(
    source_plan: SplitSourcePlan,
    requested: NormalizedRange,
    payload: Any,
    output_path: Path,
) -> StagedSplitClip:
    validate_staged_temporal_clip(
        source_plan.source,
        requested,
        payload,
        output_path,
    )
    staged = StagedTemporalClip(
        source=source_plan.source,
        requested=requested,
        payload=payload,
    )

    return StagedSplitClip(
        clip=staged,
        transcripts=project_split_transcripts(
            source_plan,
            requested,
            start_ms=staged.resolved_start_ms,
            end_ms=staged.resolved_end_ms,
        ),
        annotations=source_plan.annotations,
    )


def project_split_transcripts(source_plan, requested, *, start_ms, end_ms):
    """Project captured transcript membership for planning and rendered clips."""

    def project_one(transcript: ResolvedTranscriptSource) -> TranscriptProjection:
        topic_analysis = source_plan.topic_analysis
        selected_ids = source_plan.topic_span_ids_by_item.get(requested.item_id)
        if (
            selected_ids is not None
            and topic_analysis is not None
            and transcript.evidence_link_stable_id
            == str(topic_analysis.payload["transcript_evidence_id"])
        ):
            # Project the exact identity set recorded by locking. A neighbor
            # touching only an already widened edge must not enter here.
            transcript = replace(
                transcript,
                spans=tuple(
                    span
                    for span in transcript.spans
                    if str(span["stable_id"]) in selected_ids
                ),
            )
        return project_transcript(
            transcript,
            source_start_ms=start_ms,
            source_end_ms=end_ms,
        )

    return tuple(
        (
            transcript,
            project_one(transcript),
        )
        for transcript in source_plan.transcripts
    )


def _stage_source_with_core_renderer(
    project: Project,
    source_plan: SplitSourcePlan,
    jobs: list[tuple[NormalizedRange, Path]],
    *,
    cancelled: Callable[[], bool] | None = None,
    materialize=None,
) -> list[StagedSplitClip]:
    """Lease a source blob once, then render all of its ranges sequentially."""

    source = source_plan.source

    async def render(source_path: Path, should_cancel) -> list[StagedSplitClip]:
        staged: list[StagedSplitClip] = []
        for requested, output_path in jobs:
            _check_render_cancelled(should_cancel)
            payload = await stage_media_splice(
                source_path,
                output_path,
                media_kind=media_kind(source),
                start_ms=requested.start_ms,
                end_ms=requested.end_ms,
                source_mime=source.lease.media_type,
                source_filename=source.lease.filename,
                should_cancel=should_cancel,
            )
            projected = _project_staged_clip(
                source_plan,
                requested,
                payload,
                output_path,
            )
            staged.append(projected)
        return staged

    with (materialize or project.materialize_blob)(
        source.lease.blob_hash
    ) as source_path:
        return run_media_sync(
            lambda should_cancel: render(Path(source_path), should_cancel),
            cancelled=cancelled,
        )


def _check_render_cancelled(cancelled: Callable[[], bool] | None) -> None:
    if cancelled is not None and cancelled():
        raise MediaSpliceError("cancelled", "media render was cancelled")


def stage_temporal_split_plan(
    project: Project,
    plan: TemporalSplitPlan,
    scratch: Path,
    *,
    materializer: CoreTemporalMediaMaterializer,
    stage_fn: StageTemporalRange | None = None,
    use_core_batch_renderer: bool = False,
    row_limit: int | None = None,
    cancelled: Callable[[], bool] | None = None,
    materialize=None,
) -> tuple[StagedSplitClip, ...]:
    """Render the admitted prefix outside SQLite, leasing each source once."""

    if row_limit is not None and (type(row_limit) is not int or row_limit < 0):
        raise ValueError("row_limit must be a nonnegative integer")
    if sum(len(source.source.ranges) for source in plan.sources) != plan.output_count:
        raise RuntimeError("temporal Split plan count changed unexpectedly")
    expected = (
        plan.output_count if row_limit is None else min(plan.output_count, row_limit)
    )
    staged: list[StagedSplitClip] = []
    ordinal = 0
    for source_plan in plan.sources:
        if ordinal == expected:
            break
        _check_render_cancelled(cancelled)
        jobs: list[tuple[NormalizedRange, Path]] = []
        for requested in source_plan.source.ranges:
            if ordinal == expected:
                break
            jobs.append(
                (
                    requested,
                    _staged_output_path(
                        scratch,
                        ordinal=ordinal,
                        source=source_plan.source,
                    ),
                )
            )
            ordinal += 1
        if use_core_batch_renderer and stage_fn is None:
            source_staged = _stage_source_with_core_renderer(
                project,
                source_plan,
                jobs,
                cancelled=cancelled,
                materialize=materialize,
            )
            staged.extend(source_staged)
            continue
        for requested, output_path in jobs:
            _check_render_cancelled(cancelled)
            payload = (
                stage_fn(project, source_plan.source, requested, output_path)
                if stage_fn is not None
                else materializer.stage(
                    project,
                    source_plan.source,
                    requested,
                    output_path,
                )
            )
            projected = _project_staged_clip(
                source_plan,
                requested,
                payload,
                output_path,
            )
            staged.append(projected)
    _check_render_cancelled(cancelled)
    if len(staged) != expected:
        raise RuntimeError("temporal Split staging count changed unexpectedly")
    return tuple(staged)


def _clip_column_type(staged: tuple[StagedSplitClip, ...]) -> str:
    media_kinds = {str(item.clip.payload.media_kind) for item in staged}
    if media_kinds == {"audio"}:
        return "audio"
    if media_kinds == {"video"}:
        return "video"
    return "file"


def _projectable_annotations(
    staged: tuple[StagedSplitClip, ...],
) -> tuple[ResolvedTemporalAnnotation, ...]:
    by_column: dict[int, ResolvedTemporalAnnotation] = {}
    for item in staged:
        for annotation in item.annotations:
            if annotation_intersects_source_range(
                annotation,
                source_start_ms=item.clip.resolved_start_ms,
                source_end_ms=item.clip.resolved_end_ms,
            ):
                by_column.setdefault(annotation.column_id, annotation)
    return tuple(by_column.values())


def _source_range_for_staged(item: StagedSplitClip) -> dict[str, Any]:
    requested = item.clip.requested
    metadata = dict(requested.metadata or {})
    if (
        item.clip.resolved_start_ms != requested.start_ms
        or item.clip.resolved_end_ms != requested.end_ms
    ):
        metadata["requested_range"] = {
            "start_ms": requested.start_ms,
            "end_ms": requested.end_ms,
        }
    return source_range_value(
        item.clip.source,
        start_ms=item.clip.resolved_start_ms,
        end_ms=item.clip.resolved_end_ms,
        item_id=requested.item_id,
        label=requested.label,
        metadata=metadata,
    )


def _receipt_inputs(plan: TemporalSplitPlan) -> list[ReceiptIO]:
    inputs: list[ReceiptIO] = []
    for item in plan.sources:
        source = item.source
        inputs.extend(
            [
                ReceiptIO(
                    name=f"source.{source.row_id}", ref=source_snapshot_ref(source)
                ),
                ReceiptIO(
                    name=f"selection.{source.row_id}",
                    ref=selection_snapshot_ref(
                        source,
                        row_id=source.row_id,
                        topic_analysis=item.topic_analysis,
                    ),
                ),
            ]
        )
        for transcript in item.transcripts:
            inputs.append(
                ReceiptIO(
                    name=(
                        f"transcript.{source.row_id}.{transcript.transcript_column_id}"
                    ),
                    ref=transcript_snapshot_ref(transcript),
                )
            )
        for annotation in item.annotations:
            inputs.append(
                ReceiptIO(
                    name=(f"annotation.{source.row_id}.{annotation.column_id}"),
                    ref=annotation.input_ref(),
                )
            )
    return inputs


def _all_warnings(
    plan: TemporalSplitPlan,
    staged: tuple[StagedSplitClip, ...],
) -> list[str]:
    return list(
        dict.fromkeys(
            (
                *plan.warnings,
                *(warning for item in staged for warning in item.warnings),
            )
        )
    )


def publish_split_clip(
    project,
    item,
    stored_clip,
    write,
    output_row_id,
    *,
    clip_name,
    transcript_names,
    annotation_names,
    receipt_id,
    materializer,
    row,
    producer_id,
):
    """Publish one already-rendered occurrence's clock and dependent evidence."""
    clip_evidence = []
    lineage = materializer.attach_lineage(
        project,
        stored_clip,
        output_sheet_id=write.sheet_id,
        output_row_id=output_row_id,
        output_column_id=write.column_ids[clip_name],
        receipt_id=receipt_id,
    )
    clip_evidence.extend(
        [
            ReceiptEvidence(
                ref=staged_receipt_ref(stored_clip),
                retention="materialized",
            ),
            ReceiptEvidence(ref=lineage, retention="materialized"),
        ]
    )
    derived_anchor = resolve_artifact_timeline(project, int(lineage["artifact_id"]))
    annotation_cells = []
    for annotation in item.annotations:
        annotation_name = annotation_names.get(annotation.column_id)
        if annotation_name is None:
            continue
        projection = project_resolved_temporal_annotation(
            annotation,
            source_start_ms=item.clip.resolved_start_ms,
            source_end_ms=item.clip.resolved_end_ms,
            derived_timeline=derived_anchor,
        )
        if projection is None:
            continue
        annotation_column_id = write.column_ids[annotation_name]
        row[annotation_name] = projection.value
        annotation_cells.append(
            BaseCellWrite(
                row_id=output_row_id,
                column_id=annotation_column_id,
                value=projection.value,
            )
        )
        clip_evidence.append(
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
                        "value_hash": canonical_json_hash(projection.value),
                    },
                    "projection": projection.receipt_metadata(),
                },
                retention="materialized",
            )
        )
    initialize_base_cells(project.db, producer_id=producer_id, cells=annotation_cells)
    for transcript, projection in item.transcripts:
        if not projection.segments:
            continue
        transcript_name = transcript_names.get(transcript.transcript_column_id)
        if transcript_name is None:
            continue
        projected = persist_projected_transcript_evidence(
            project,
            transcript=transcript,
            projection=projection,
            derived_artifact_id=int(lineage["artifact_id"]),
            output_sheet_id=write.sheet_id,
            output_row_id=output_row_id,
            transcript_column_id=write.column_ids[transcript_name],
            receipt_id=receipt_id,
            op_id=write.op_id,
        )
        if projected is not None:
            clip_evidence.append(
                ReceiptEvidence(ref=projected, retention="materialized")
            )

    return clip_evidence
