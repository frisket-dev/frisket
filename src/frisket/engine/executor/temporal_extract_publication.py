"""Atomic publication of prepared temporal clips and their grounded evidence."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from frisket.contracts.action import (
    ActionError,
    ActionResult,
    ReceiptEvidence,
    ReceiptIO,
)
from frisket.engine.executor.action_receipts import _result_from_receipt
from frisket.engine.executor.action_reservations import (
    _terminalize_claimless_direct_failure,
)
from frisket.engine.executor.action_support import (
    _failed_result,
)
from frisket.engine.executor.temporal_materialization import (
    CoreTemporalMediaMaterializer,
    ResolvedTemporalMediaSource,
    ResolvedTemporalSource,
    StagedTemporalClip,
    revalidate_action_sources,
    revalidate_temporal_media_source,
    selection_snapshot_ref,
    source_snapshot_ref,
    staged_receipt_ref,
    transcript_snapshot_ref,
)
from frisket.engine.executor.temporal_annotations import (
    ResolvedTemporalAnnotation,
    annotation_intersects_source_range,
    project_resolved_temporal_annotation,
)
from frisket.engine.executor.temporal_finder_provenance import (
    ResolvedTopicAnalysisSidecar,
)
from frisket.engine.store.artifact_timeline import (
    TimelineError,
    canonical_json_hash,
    resolve_artifact_timeline,
)
from frisket.engine.store.cell_writes import EditCellWrite, insert_edits
from frisket.engine.store.receipts import FINISHED_RECEIPT_STATUSES, ReceiptStore


_EXTRACT_KIND = "temporal.extract_range"


@dataclass(frozen=True)
class StagedExtractClip:
    """A rendered clip and its compatible clip-local transcripts."""

    clip: StagedTemporalClip
    transcripts: tuple[tuple[Any, Any], ...]
    annotations: tuple[ResolvedTemporalAnnotation, ...]
    topic_analysis: ResolvedTopicAnalysisSidecar | None = None
    plan_warnings: tuple[str, ...] = ()

    @property
    def warnings(self) -> tuple[str, ...]:
        projected = tuple(
            warning
            for _transcript, projection in self.transcripts
            for warning in projection.warnings
        )
        return tuple(dict.fromkeys((*self.plan_warnings, *projected)))


def _terminalize_extract_failure(
    project: Any,
    *,
    project_id: str,
    receipt_id: str,
    error: ActionError,
    action_kind: str = _EXTRACT_KIND,
) -> ActionResult:
    """Close direct reservations; queued failures belong to action.run."""

    stored = ReceiptStore(project).find_by_id(receipt_id)
    if stored is not None and any(
        item.ref.get("kind") == "queued_action_job" for item in stored.parsed().inputs
    ):
        return _failed_result(
            project_id=project_id,
            action_kind=action_kind,
            error=error,
        )
    return _terminalize_claimless_direct_failure(
        project,
        project_id=project_id,
        action_kind=action_kind,
        stored_receipt=stored,
        error=error,
        project_write_failed_message=(
            "Temporal extract receipt could not be finalized"
        ),
    )


def _reserved_extract_preflight(
    project: Any,
    *,
    receipt_id: str,
    action_id: str,
    params_hash: str,
    action_kind: str = _EXTRACT_KIND,
) -> ActionResult | ActionError | None:
    stored = ReceiptStore(project).find_by_id(receipt_id)
    if stored is None:
        return ActionError(
            code="project_write_failed",
            message="Temporal extract receipt is missing",
            action_kind=action_kind,
        )
    if stored.action_id != action_id or stored.params_hash != params_hash:
        return ActionError(
            code="idempotency_conflict",
            message="Temporal extract reservation does not match the requested action",
            action_kind=action_kind,
            field="idempotency_key",
        )
    if stored.status == "running":
        return None
    prior = stored.parsed()
    if prior.status in FINISHED_RECEIPT_STATUSES:
        return _result_from_receipt(prior)
    return ActionError(
        code="idempotency_in_progress",
        message="Temporal extract receipt is not in its runnable state",
        action_kind=action_kind,
        field="idempotency_key",
        details={"receipt_id": receipt_id, "status": stored.status},
    )


def _extract_output_type(source: ResolvedTemporalSource, rendered: Any) -> str:
    if source.column_type in {"audio", "video"}:
        return source.column_type
    return "audio" if rendered.media_kind == "audio" else "video"


def _extract_column_type(staged: tuple[StagedExtractClip, ...]) -> str:
    output_types = {
        _extract_output_type(item.clip.source, item.clip.payload) for item in staged
    }
    return next(iter(output_types)) if len(output_types) == 1 else "file"


def _extract_plan_annotations(
    plans: tuple[ResolvedTemporalMediaSource, ...],
) -> tuple[ResolvedTemporalAnnotation, ...]:
    by_column: dict[int, ResolvedTemporalAnnotation] = {}
    for plan in plans:
        temporal_range = plan.source.ranges[0]
        for annotation in plan.annotations:
            if annotation_intersects_source_range(
                annotation,
                source_start_ms=temporal_range.start_ms,
                source_end_ms=temporal_range.end_ms,
            ):
                by_column.setdefault(annotation.column_id, annotation)
    return tuple(by_column.values())


def _staged_extract_annotations(
    staged: tuple[StagedExtractClip, ...],
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


def _receipt_input_name(name: str, row_id: int, *, batch: bool) -> str:
    return f"{name}.{row_id}" if batch else name


def _write_extract_in_transaction(
    project: Any,
    bound: Any,
    params: Any,
    staged: tuple[StagedExtractClip, ...],
    *,
    project_id: str,
    action_id: str,
    receipt_id: str,
    params_hash: str,
    materializer: CoreTemporalMediaMaterializer,
) -> ActionResult:
    """Publish every staged map output. Caller owns the transaction boundary."""

    sources = tuple(item.clip.source for item in staged)
    revalidate_action_sources(project, sources)
    for item in staged:
        revalidate_temporal_media_source(
            project,
            ResolvedTemporalMediaSource(
                source=item.clip.source,
                transcripts=tuple(
                    transcript for transcript, _projection in item.transcripts
                ),
                annotations=item.annotations,
                topic_analysis=item.topic_analysis,
                warnings=item.plan_warnings,
            ),
        )
    stored = ReceiptStore(project).find_by_id(receipt_id)
    if stored is None or stored.status != "running":
        raise RuntimeError("temporal extract reservation is not running")
    if stored.params_hash != params_hash or stored.action_id != action_id:
        raise RuntimeError("temporal extract reservation does not match action")

    active_transcripts = {
        transcript.transcript_column_id
        for item in staged
        for transcript, projection in item.transcripts
        if projection.segments
    }
    transcript_names = {
        column_id: name
        for column_id, name in params.transcript_names.items()
        if column_id in active_transcripts
    }
    active_annotations = {
        item.column_id for item in _staged_extract_annotations(staged)
    }
    annotation_names = {
        column_id: name
        for column_id, name in params.annotation_names.items()
        if column_id in active_annotations
    }
    output_names = [
        params.output_name,
        *transcript_names.values(),
        *annotation_names.values(),
    ]
    placeholders = ",".join("?" for _ in output_names)
    duplicate = project.db.execute(
        f"SELECT name FROM columns WHERE sheet_id=? AND name IN ({placeholders}) "
        "ORDER BY name LIMIT 1",
        (params.sheet_id, *output_names),
    ).fetchone()
    if duplicate is not None:
        raise TimelineError(
            "invalid_input_ref",
            f"output column {str(duplicate['name'])!r} already exists",
        )

    committed = tuple(materializer.commit_blob(project, item.clip) for item in staged)
    cur = project.db.cursor()
    cur.execute("UPDATE ops SET status='discarded' WHERE status='undone'")
    op_spec = bound.request.model_dump(mode="json", exclude_none=True)
    op_spec["params_hash"] = params_hash
    cur.execute(
        "INSERT INTO ops (kind, label, spec, undo_info, barrier) "
        "VALUES (?, ?, ?, '{}', 0)",
        (
            bound.action.action_id,
            f"{bound.action.action_id} {params.output_name}",
            json.dumps(op_spec, sort_keys=True, allow_nan=False),
        ),
    )
    op_id = int(cur.lastrowid)
    cur.execute("UPDATE meta SET value=? WHERE key='op_cursor'", (str(op_id),))
    cur.execute(
        "INSERT INTO columns (sheet_id, name, type, position, ai_generated, hidden) "
        "VALUES (?, ?, ?, (SELECT COALESCE(MAX(position), 0) + 1 FROM columns "
        "WHERE sheet_id=?), 1, 0)",
        (
            params.sheet_id,
            params.output_name,
            _extract_column_type(staged),
            params.sheet_id,
        ),
    )
    output_column_id = int(cur.lastrowid)
    insert_edits(
        project.db,
        op_id=op_id,
        edits=[
            EditCellWrite(
                row_id=item.clip.source.row_id,
                column_id=output_column_id,
                value=stored_clip.cell_value,
            )
            for item, stored_clip in zip(staged, committed, strict=True)
        ],
    )
    created_column_ids = [output_column_id]
    transcript_column_ids: dict[int, int] = {}
    for source_column_id, transcript_name in transcript_names.items():
        cur.execute(
            "INSERT INTO columns (sheet_id, name, type, position, ai_generated, hidden) "
            "VALUES (?, ?, 'timestamped_transcript', "
            "(SELECT COALESCE(MAX(position), 0) + 1 "
            "FROM columns WHERE sheet_id=?), 1, 0)",
            (params.sheet_id, transcript_name, params.sheet_id),
        )
        transcript_column_id = int(cur.lastrowid)
        transcript_column_ids[source_column_id] = transcript_column_id
        created_column_ids.append(transcript_column_id)
        transcript_edits: list[EditCellWrite] = []
        for item in staged:
            for transcript, projection in item.transcripts:
                if (
                    transcript.transcript_column_id != source_column_id
                    or not projection.segments
                ):
                    continue
                transcript_edits.append(
                    EditCellWrite(
                        row_id=item.clip.source.row_id,
                        column_id=transcript_column_id,
                        value=projection.text,
                    )
                )
        insert_edits(project.db, op_id=op_id, edits=transcript_edits)
    annotations_by_column = {
        annotation.column_id: annotation
        for annotation in _staged_extract_annotations(staged)
    }
    annotation_column_ids: dict[int, int] = {}
    for source_column_id, annotation_name in annotation_names.items():
        annotation = annotations_by_column[source_column_id]
        cur.execute(
            "INSERT INTO columns (sheet_id, name, type, position, ai_generated, hidden) "
            "VALUES (?, ?, ?, (SELECT COALESCE(MAX(position), 0) + 1 "
            "FROM columns WHERE sheet_id=?), 0, 0)",
            (
                params.sheet_id,
                annotation_name,
                annotation.type_name,
                params.sheet_id,
            ),
        )
        annotation_column_id = int(cur.lastrowid)
        annotation_column_ids[source_column_id] = annotation_column_id
        created_column_ids.append(annotation_column_id)
    cur.execute(
        "UPDATE ops SET undo_info=? WHERE id=?",
        (
            json.dumps({"created_columns": created_column_ids}, sort_keys=True),
            op_id,
        ),
    )

    from frisket.engine.executor.temporal_transcripts import (
        persist_projected_transcript_evidence,
    )

    transcript_row_ids: dict[int, list[int]] = {
        source_column_id: [] for source_column_id in transcript_names
    }
    annotation_row_ids: dict[int, list[int]] = {
        source_column_id: [] for source_column_id in annotation_names
    }
    receipt_evidence: list[ReceiptEvidence] = []
    for item, stored_clip in zip(staged, committed, strict=True):
        source = item.clip.source
        lineage = materializer.attach_lineage(
            project,
            stored_clip,
            output_sheet_id=params.sheet_id,
            output_row_id=source.row_id,
            output_column_id=output_column_id,
            receipt_id=receipt_id,
        )
        receipt_evidence.extend(
            [
                ReceiptEvidence(
                    ref=staged_receipt_ref(stored_clip), retention="materialized"
                ),
                ReceiptEvidence(ref=lineage, retention="materialized"),
            ]
        )
        derived_anchor = resolve_artifact_timeline(project, int(lineage["artifact_id"]))
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
            annotation_column_id = annotation_column_ids[annotation.column_id]
            insert_edits(
                project.db,
                op_id=op_id,
                edits=[
                    EditCellWrite(
                        row_id=source.row_id,
                        column_id=annotation_column_id,
                        value=projection.value,
                    )
                ],
            )
            annotation_row_ids[annotation.column_id].append(source.row_id)
            receipt_evidence.append(
                ReceiptEvidence(
                    ref={
                        "kind": "temporal_annotation_projection",
                        "source": annotation.input_ref(),
                        "output": {
                            "sheet_id": params.sheet_id,
                            "row_id": source.row_id,
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
        for transcript, projection in item.transcripts:
            if not projection.segments:
                continue
            source_column_id = transcript.transcript_column_id
            transcript_column_id = transcript_column_ids[source_column_id]
            transcript_evidence = persist_projected_transcript_evidence(
                project,
                transcript=transcript,
                projection=projection,
                derived_artifact_id=int(lineage["artifact_id"]),
                output_sheet_id=params.sheet_id,
                output_row_id=source.row_id,
                transcript_column_id=transcript_column_id,
                receipt_id=receipt_id,
                op_id=op_id,
            )
            if transcript_evidence is not None:
                receipt_evidence.append(
                    ReceiptEvidence(
                        ref=transcript_evidence,
                        retention="materialized",
                    )
                )
            transcript_row_ids[source_column_id].append(source.row_id)

    output_ref = {
        "kind": "materialized_column",
        "sheet_id": params.sheet_id,
        "column_id": output_column_id,
        "op_id": op_id,
        "row_ids": [item.clip.source.row_id for item in staged],
    }
    transcript_refs = {
        source_column_id: {
            "kind": "materialized_column",
            "sheet_id": params.sheet_id,
            "column_id": transcript_column_ids[source_column_id],
            "op_id": op_id,
            "row_ids": row_ids,
        }
        for source_column_id, row_ids in transcript_row_ids.items()
        if row_ids
    }
    annotation_refs = {
        source_column_id: {
            "kind": "materialized_column",
            "sheet_id": params.sheet_id,
            "column_id": annotation_column_ids[source_column_id],
            "op_id": op_id,
            "row_ids": row_ids,
        }
        for source_column_id, row_ids in annotation_row_ids.items()
        if row_ids
    }
    warnings = list(
        dict.fromkeys(
            [
                *(warning for item in staged for warning in item.clip.source.warnings),
                *(warning for item in staged for warning in item.warnings),
            ]
        )
    )
    prior = stored.parsed()
    receipt_inputs: list[ReceiptIO] = []
    is_batch = len(staged) > 1
    for item in staged:
        source = item.clip.source
        receipt_inputs.extend(
            [
                ReceiptIO(
                    name=_receipt_input_name("source", source.row_id, batch=is_batch),
                    ref=source_snapshot_ref(source),
                ),
                ReceiptIO(
                    name=_receipt_input_name(
                        "selection", source.row_id, batch=is_batch
                    ),
                    ref=selection_snapshot_ref(
                        source,
                        row_id=source.row_id,
                        topic_analysis=item.topic_analysis,
                    ),
                ),
            ]
        )
        for transcript, _projection in item.transcripts:
            receipt_inputs.append(
                ReceiptIO(
                    name=(
                        f"transcript.{source.row_id}.{transcript.transcript_column_id}"
                    ),
                    ref=transcript_snapshot_ref(transcript),
                )
            )
        for annotation in item.annotations:
            receipt_inputs.append(
                ReceiptIO(
                    name=(f"annotation.{source.row_id}.{annotation.column_id}"),
                    ref=annotation.input_ref(),
                )
            )
    receipt_outputs = [
        ReceiptIO(name=params.output_name, ref=output_ref),
        *[
            ReceiptIO(name=transcript_names[source_column_id], ref=ref)
            for source_column_id, ref in transcript_refs.items()
        ],
        *[
            ReceiptIO(name=annotation_names[source_column_id], ref=ref)
            for source_column_id, ref in annotation_refs.items()
        ],
    ]
    receipt = prior.model_copy(
        update={
            "project_id": project_id,
            "action_id": action_id,
            "action_kind": bound.action.action_id,
            "op_ids": [op_id],
            "idempotency_key": bound.request.idempotency_key,
            "params_hash": params_hash,
            "status": "completed",
            "inputs": receipt_inputs,
            "outputs": receipt_outputs,
            "evidence": [
                *prior.evidence,
                ReceiptEvidence(
                    ref={
                        "kind": "temporal_extract_summary",
                        "source_count": len(staged),
                        "output_count": len(staged),
                    },
                    retention="pinned",
                ),
                *receipt_evidence,
            ],
            "warnings": warnings,
            "errors": [],
        }
    )
    updated = ReceiptStore(project).update_body_status(
        receipt,
        require_status="running",
        commit=False,
    )
    if not updated:
        raise RuntimeError("temporal extract receipt update did not land")
    return _result_from_receipt(receipt)
