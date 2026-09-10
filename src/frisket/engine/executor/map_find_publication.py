"""Atomic publication and terminal outcomes for ``map.find``."""

from __future__ import annotations

import hashlib
from typing import Any

from frisket.contracts.action import (
    ActionError,
    ActionIdentity,
    ActionOutput,
    ActionResult,
    Receipt,
    ReceiptEvidence,
    ReceiptIO,
)
from frisket.engine.executor.find_plan import FindOperation
from frisket.engine.executor.action_inventory import _TypedProjectEnvelope
from frisket.engine.executor.map_find_scan import (
    AnyFindMatch,
    FindMatch,
    FindScanResult,
    VisualFindMatch,
)
from frisket.engine.executor.map_find_source import (
    _find_target_snapshot,
    _snapshot_payload,
    resolve_find_sources,
)


def _model_calls(
    params: FindOperation, wire_calls: tuple[Any, ...]
) -> list[dict[str, Any]]:
    from frisket.engine.runner.row_execution import _wire_accounting_meta

    accounting = _wire_accounting_meta(params.model, list(wire_calls))
    return [
        dict(call)
        for call in accounting.get("model_calls", [])
        if isinstance(call, dict)
    ]


def _find_admission_evidence(project: Any, receipt_id: str) -> list[ReceiptEvidence]:
    from frisket.engine.store.receipts import ReceiptStore

    receipt = ReceiptStore(project).parsed_by_id(receipt_id)
    if receipt is None:
        raise RuntimeError("Find lost its admitted receipt before publication")
    return [
        item for item in receipt.evidence if item.ref.get("kind") == "find_admission"
    ]


def _failure_after_egress(
    project: Any,
    action: _TypedProjectEnvelope,
    params: FindOperation,
    *,
    project_id: str,
    action_id: str,
    receipt_id: str,
    params_hash: str,
    error: ActionError,
    wire_calls: tuple[Any, ...],
) -> ActionResult:
    from frisket.engine.store.receipts import ReceiptStore
    from frisket.engine.store.runs import RunResultStore

    calls = _model_calls(params, wire_calls)
    try:
        project.db.execute("BEGIN IMMEDIATE")
        RunResultStore(project).write_unscoped_model_calls(
            calls, row_id=None, column_id=None, commit=False
        )
        receipt = Receipt(
            receipt_id=receipt_id,
            project_id=project_id,
            action_id=action_id,
            action_kind=action.kind,
            idempotency_key=action.idempotency_key,
            params_hash=params_hash,
            status="failed",
            inputs=[
                ReceiptIO(
                    name="source",
                    ref={
                        "kind": "map_find_source_scope",
                        "sheet_id": params.sheet_id,
                        "source_column": params.source_column,
                        "row_ids": params.row_ids,
                    },
                )
            ],
            provider_use=[
                {
                    "capability": "llm.complete",
                    "model": params.model,
                    "model_call_ids": [str(call["id"]) for call in calls],
                }
            ],
            evidence=[
                *_find_admission_evidence(project, receipt_id),
                ReceiptEvidence(
                    ref={
                        "kind": "map_find_coverage",
                        "status": "incomplete",
                    },
                    retention="pinned",
                ),
            ],
            errors=[error],
        )
        updated = ReceiptStore(project).update_body_status(
            receipt, require_status="running", commit=False
        )
        if not updated:
            raise RuntimeError("map.find running receipt was lost")
        project.db.commit()
    except Exception:
        project.db.rollback()
        raise
    return ActionResult(
        action=ActionIdentity(kind=action.kind, action_id=action_id),
        status="failed",
        project_id=project_id,
        receipt_id=receipt_id,
        errors=[error],
    )


def _materialize_text_spans(
    project: Any,
    match: FindMatch,
    *,
    artifact_cache: dict[tuple[int, int], dict[str, Any]],
) -> list[dict[str, Any]]:
    from frisket.engine.store.evidence import (
        record_source_artifact,
        record_source_span,
        record_text_surface,
    )

    source = match.source
    values = project.get_values(
        source.sheet_id, source.column_id, row_ids=[source.row_id]
    )
    text = values.get(source.row_id)
    if not isinstance(text, str):
        raise ValueError("map.find text source changed before publication")
    content_hash = "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
    surface = record_text_surface(
        project,
        surface_kind="cell",
        content_hash=content_hash,
        offset_unit="unicode_codepoint",
        text_sheet_id=source.sheet_id,
        text_row_id=source.row_id,
        text_column_id=source.column_id,
        value_ref=source.value_ref,
    )
    cache_key = (source.row_id, source.column_id)
    artifact = artifact_cache.get(cache_key)
    if artifact is None:
        artifact = record_source_artifact(
            project,
            artifact_kind="row",
            media_type="application/vnd.frisket.row+json",
            source_sheet_id=source.sheet_id,
            source_row_id=source.row_id,
            source_column_id=source.column_id,
            metadata={"source_column": source.column_id},
        )
        artifact_cache[cache_key] = artifact
    if len(match.span_ids) != 2 or any(span_id >= 0 for span_id in match.span_ids):
        raise ValueError("invalid virtual text span")
    start = -match.span_ids[0] - 1
    end = -match.span_ids[1] - 1
    span = record_source_span(
        project,
        artifact_id=int(artifact["id"]),
        span_kind="text",
        char_start=start,
        char_end=end,
        text_layer_hash=content_hash,
        text_surface_id=int(surface["id"]),
        quote=text[start:end],
        snippet=match.candidate.match,
        metadata={
            "source_snapshot": source.source_snapshot,
            "source_unit_ids": list(match.candidate.source_unit_ids),
        },
    )
    return [{"span_id": int(span["id"]), "rank": 0, "required": True}]


def _materialize_visual_span(
    project: Any,
    match: VisualFindMatch,
    *,
    artifact_cache: dict[tuple[int, int], dict[str, Any]],
) -> list[dict[str, Any]]:
    from frisket.engine.store.evidence import (
        record_source_artifact,
        record_source_span,
    )

    source = match.source
    image = source.image
    if image is None or source.blob_hash is None:
        raise ValueError("map.find visual match lost its source image")
    cache_key = (source.row_id, source.column_id)
    artifact = artifact_cache.get(cache_key)
    if artifact is None:
        artifact = record_source_artifact(
            project,
            artifact_kind="file",
            media_type=source.media_type or "image/unknown",
            blob_hash=source.blob_hash,
            filename=source.filename,
            page_count=1,
            source_sheet_id=source.sheet_id,
            source_row_id=source.row_id,
            source_column_id=source.column_id,
            metadata={
                "page_images": {
                    "1": {
                        "blob_hash": source.blob_hash,
                        "width": image.display_width,
                        "height": image.display_height,
                    }
                },
                "grounding_method": match.region.provenance.grounding_method,
                "region_locator": {
                    "engine_ref": match.region.provenance.engine_ref,
                    "profile_id": match.region.provenance.profile_id,
                    "prompt_version": match.region.provenance.prompt_version,
                    "inference_image_sha256": image.sha256,
                    "inference_width": image.width,
                    "inference_height": image.height,
                },
            },
        )
        artifact_cache[cache_key] = artifact
    span = record_source_span(
        project,
        artifact_id=int(artifact["id"]),
        span_kind="region",
        page_start=1,
        page_end=1,
        bbox=[match.region.bbox],
        selector={
            "profile_id": match.region.provenance.profile_id,
            "prompt_version": match.region.provenance.prompt_version,
            "raw_bbox": list(match.region.raw_bbox),
        },
        quote=match.region.match,
        snippet=match.region.match,
        metadata={
            "source_snapshot": source.source_snapshot,
            "grounding_method": match.region.provenance.grounding_method,
            "raw": {
                "grounding_method": match.region.provenance.grounding_method,
                "engine_ref": match.region.provenance.engine_ref,
                "profile_id": match.region.provenance.profile_id,
                "prompt_version": match.region.provenance.prompt_version,
            },
        },
    )
    return [{"span_id": int(span["id"]), "rank": 0, "required": True}]


def _match_span_refs(
    project: Any,
    match: AnyFindMatch,
    *,
    artifact_cache: dict[tuple[int, int], dict[str, Any]],
) -> list[dict[str, Any]]:
    if isinstance(match, VisualFindMatch):
        return _materialize_visual_span(project, match, artifact_cache=artifact_cache)
    if match.span_ids and all(span_id > 0 for span_id in match.span_ids):
        return [
            {"span_id": span_id, "rank": rank, "required": True}
            for rank, span_id in enumerate(dict.fromkeys(match.span_ids))
        ]
    return _materialize_text_spans(project, match, artifact_cache=artifact_cache)


def _match_value(match: AnyFindMatch) -> str:
    if isinstance(match, VisualFindMatch):
        return match.region.match
    return match.candidate.match


def _match_details(match: AnyFindMatch) -> dict[str, Any]:
    if isinstance(match, VisualFindMatch):
        return match.region.details
    return match.candidate.details


def _action_outputs(params: FindOperation, write: Any) -> list[ActionOutput]:
    outputs = [
        ActionOutput(
            kind="sheet",
            name=params.target_sheet_name,
            sheet_id=write.sheet_id,
            ref=write.materialized_sheet_ref,
        )
    ]
    outputs.extend(
        ActionOutput(
            kind="column",
            name=name,
            sheet_id=write.sheet_id,
            column_id=column_id,
            ref=write.materialized_column_refs[name],
        )
        for name, column_id in write.column_ids.items()
    )
    outputs.append(
        ActionOutput(
            kind="rows",
            name="rows",
            sheet_id=write.sheet_id,
            row_ids=write.row_ids,
            ref=write.materialized_rows_ref,
        )
    )
    return outputs


def _receipt_outputs(params: FindOperation, write: Any) -> list[ReceiptIO]:
    return [
        ReceiptIO(name=params.target_sheet_name, ref=write.materialized_sheet_ref),
        *[
            ReceiptIO(name=f"column.{name}", ref=ref)
            for name, ref in write.materialized_column_refs.items()
        ],
        ReceiptIO(name="rows", ref=write.materialized_rows_ref),
    ]


class _FindPublicationRefusal(Exception):
    def __init__(self, error: ActionError) -> None:
        super().__init__(error.message)
        self.error = error


def write_find_result(
    project: Any,
    action: _TypedProjectEnvelope,
    params: FindOperation,
    scan: FindScanResult,
    *,
    project_id: str,
    action_id: str,
    receipt_id: str,
    params_hash: str,
    admitted: dict[str, Any],
) -> ActionResult:
    from frisket.engine.store.evidence import record_evidence_link
    from frisket.engine.store.materialization import (
        AggregateColumnSpec,
        AggregateMaterializedRow,
        AggregateSheetPlan,
        write_aggregate_sheet,
    )
    from frisket.engine.store.receipts import ReceiptStore
    from frisket.engine.store.runs import RunResultStore

    columns = [
        AggregateColumnSpec(
            name=params.output_names["match"], type="text", ai_generated=True
        )
    ]
    columns.extend(
        AggregateColumnSpec(
            name=params.output_names[field.name],
            type=field.column_type,
            ai_generated=True,
        )
        for field in params.fields
    )
    rows = [
        AggregateMaterializedRow(
            values={
                params.output_names["match"]: _match_value(match),
                **{
                    params.output_names[key]: value
                    for key, value in _match_details(match).items()
                },
            },
            source_row_ids=[match.source.row_id],
        )
        for match in scan.matches
    ]
    calls = _model_calls(params, scan.wire_calls)
    op_spec = dict(params.request)
    op_spec["params"] = dict(op_spec["params"])
    op_spec["params"].pop("confirmed", None)
    op_spec["params"].pop("consented_promise_set_hash", None)
    op_spec["params"]["params_hash"] = params_hash
    op_spec["coverage"] = {
        "status": "complete" if not scan.issues else "incomplete",
        "window_count": len(scan.plans),
        "source_snapshots": admitted["sources"],
    }

    try:
        project.db.execute("BEGIN IMMEDIATE")
        current = resolve_find_sources(project, params)
        if isinstance(current, ActionError) or _snapshot_payload(
            current
        ) != admitted.get("sources"):
            raise _FindPublicationRefusal(
                ActionError(
                    code="source_changed",
                    message="The selected source changed during the exhaustive scan",
                    action_kind=action.kind,
                    field="params.source",
                )
            )
        target = _find_target_snapshot(project, params)
        if isinstance(target, ActionError):
            raise _FindPublicationRefusal(target)
        if target != admitted.get("target"):
            raise _FindPublicationRefusal(
                ActionError(
                    code="find_target_changed",
                    message="The managed findings target changed during the scan",
                    action_kind=action.kind,
                    field="sheet_name",
                )
            )
        plan = AggregateSheetPlan(
            action_kind=action.kind,
            label=f"map.find {params.target_sheet_name}",
            target_sheet_name=params.target_sheet_name,
            parent_sheet_id=params.sheet_id,
            op_spec=op_spec,
            columns=columns,
            rows=rows,
        )
        replacing = target["kind"] == "managed"
        if replacing:
            superseded_sheet_id = int(target["sheet_id"])
            superseded_name = (
                f"{params.target_sheet_name} "
                f"(superseded:{superseded_sheet_id}:{target['parent_op_id']})"
            )
            project.db.execute(
                "UPDATE evidence_links SET status='stale', "
                "stale_reason='superseded_find_generation', "
                "stale_at=datetime('now') WHERE sheet_id=? AND status='active'",
                (superseded_sheet_id,),
            )
            project.db.execute(
                "UPDATE sheets SET hidden=1, name=? WHERE id=?",
                (superseded_name, superseded_sheet_id),
            )
        write = write_aggregate_sheet(project.db.cursor(), plan)
        if replacing:
            project.db.execute("UPDATE ops SET barrier=1 WHERE id=?", (write.op_id,))
        artifact_cache: dict[tuple[int, int], dict[str, Any]] = {}
        link_ids: list[str] = []
        for child_row_id, match in zip(write.row_ids, scan.matches, strict=True):
            spans = _match_span_refs(project, match, artifact_cache=artifact_cache)
            if not spans:
                raise ValueError("map.find match lost all evidence spans")
            link = record_evidence_link(
                project,
                subject_kind="row",
                subject_ref={
                    "kind": "materialized_row",
                    "sheet_id": write.sheet_id,
                    "row_id": child_row_id,
                },
                spans=spans,
                sheet_id=write.sheet_id,
                row_id=child_row_id,
                op_id=write.op_id,
                receipt_id=receipt_id,
                link_role="primary_support",
                pinned=True,
                producer={
                    "action_kind": action.kind,
                    "source_row_id": match.source.row_id,
                },
                metadata={
                    "schema_version": "frisket.map_find_match.v1",
                    "source_snapshot": match.source.source_snapshot,
                    **(
                        {
                            "grounding_method": (
                                match.region.provenance.grounding_method
                            ),
                            "region_profile_id": match.region.provenance.profile_id,
                            "region_prompt_version": (
                                match.region.provenance.prompt_version
                            ),
                        }
                        if isinstance(match, VisualFindMatch)
                        else {"source_unit_ids": list(match.candidate.source_unit_ids)}
                    ),
                },
            )
            link_ids.append(str(link["stable_id"]))
        RunResultStore(project).write_unscoped_model_calls(
            calls, row_id=None, column_id=None, commit=False
        )
        outputs = _action_outputs(params, write)
        result_status = "completed" if not scan.issues else "partial"
        receipt = Receipt(
            receipt_id=receipt_id,
            project_id=project_id,
            action_id=action_id,
            action_kind=action.kind,
            op_ids=[write.op_id],
            idempotency_key=action.idempotency_key,
            params_hash=params_hash,
            status=result_status,
            inputs=[
                ReceiptIO(
                    name="source",
                    ref={
                        "kind": "map_find_source_scope",
                        "sheet_id": params.sheet_id,
                        "column_id": scan.plans[0].source.column_id,
                        "row_ids": [source["row_id"] for source in admitted["sources"]],
                        "source_snapshots": admitted["sources"],
                    },
                )
            ],
            outputs=_receipt_outputs(params, write),
            provider_use=[
                {
                    "capability": "llm.complete",
                    "model": params.model,
                    "model_call_ids": [str(call["id"]) for call in calls],
                }
            ],
            evidence=[
                *_find_admission_evidence(project, receipt_id),
                ReceiptEvidence(
                    ref={
                        "kind": "map_find_coverage",
                        "status": ("complete" if not scan.issues else "incomplete"),
                        "window_count": len(scan.plans),
                        "match_count": len(scan.matches),
                        "resolved_prompt_hash": admitted["resolved_prompt_hash"],
                    },
                    retention="pinned",
                ),
                ReceiptEvidence(
                    ref={
                        "kind": "map_find_match_evidence",
                        "evidence_link_ids": link_ids,
                    },
                    retention="pinned",
                ),
                ReceiptEvidence(
                    ref=write.materialized_row_sources_ref,
                    retention="pinned",
                ),
            ],
            errors=list(scan.issues),
        )
        updated = ReceiptStore(project).update_body_status(
            receipt, require_status="running", commit=False
        )
        if not updated:
            raise RuntimeError("map.find running receipt was lost")
        project.db.commit()
    except _FindPublicationRefusal as exc:
        project.db.rollback()
        return _failure_after_egress(
            project,
            action,
            params,
            project_id=project_id,
            action_id=action_id,
            receipt_id=receipt_id,
            params_hash=params_hash,
            error=exc.error,
            wire_calls=scan.wire_calls,
        )
    except Exception:
        project.db.rollback()
        raise

    return ActionResult(
        action=ActionIdentity(kind=action.kind, action_id=action_id),
        status="completed" if not scan.issues else "partial",
        project_id=project_id,
        op_ids=[write.op_id],
        outputs=outputs,
        receipt_id=receipt_id,
        errors=list(scan.issues),
    )


def _failure_before_egress(
    project: Any,
    envelope: Any,
    error: ActionError,
) -> ActionResult:
    """Terminalize a Find refusal that provably preceded provider egress."""

    from frisket.engine.store.receipts import ReceiptStore

    receipts = ReceiptStore(project)
    stored = receipts.find_by_id(envelope.receipt_id)
    if stored is None:
        raise RuntimeError("map.find pre-egress failure lost its reserved receipt")
    receipt = stored.parsed()
    failed = receipt.model_copy(
        update={
            "status": "failed",
            "errors": [error],
            "provider_use": [],
            "evidence": [
                *receipt.evidence,
                ReceiptEvidence(
                    ref={
                        "kind": "map_find_effect_outcome",
                        "schema_version": "frisket.map_find_effect_outcome.v1",
                        "external_effect": "none",
                        "reason": error.code,
                    },
                    retention="pinned",
                ),
            ],
        }
    )
    try:
        project.db.execute("BEGIN IMMEDIATE")
        if not receipts.update_body_status(
            failed,
            require_status="running",
            commit=False,
        ):
            raise RuntimeError("map.find pre-egress failure lost its running receipt")
        project.db.commit()
    except Exception:
        project.db.rollback()
        raise
    return ActionResult(
        action=ActionIdentity(
            kind=envelope.action_kind,
            action_id=envelope.action_id,
        ),
        status="failed",
        project_id=envelope.project_id,
        receipt_id=envelope.receipt_id,
        errors=[error],
    )
