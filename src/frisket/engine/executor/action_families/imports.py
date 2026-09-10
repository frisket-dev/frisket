"""Import action-family handlers and implementations."""

from __future__ import annotations

import hashlib
import json
from typing import Any


from frisket.authoring import column_types
from frisket.contracts.action import (
    ActionError,
    ActionIdentity,
    ActionOutput,
    ActionResult,
    ActionSpec,
    ImportRowsParams,
    Receipt,
    ReceiptEvidence,
    ReceiptIO,
    canonical_column_type,
)
from frisket.engine.executor.action_support import (
    _failed_result,
)
from frisket.engine.store import Project
from frisket.engine.store.cell_writes import (
    BaseCellWrite,
    create_base_cell_producer,
    initialize_base_cells,
)
from frisket.engine.store.artifact_timeline import TimelineError
from frisket.engine.store.receipts import ReceiptStore
from frisket.features.temporal_ingress import preflight_typed_rows_for_persistence


_TEMPORAL_PERSISTENCE_ERROR_MESSAGES = {
    "invalid_temporal_value": "The temporal value is malformed.",
    "timeline_not_found": "The temporal value refers to a missing timeline.",
    "timeline_stale": "The temporal value timeline anchor is stale.",
    "timeline_duration_required": "The temporal timeline duration is not finalized.",
    "ambiguous_time_mapping": "The temporal value timeline mapping is ambiguous.",
    "range_out_of_bounds": "The temporal value exceeds its timeline duration.",
}


def _resolve_import_rows_project_types(
    project: Project,
    params: ImportRowsParams,
    *,
    action_kind: str,
) -> ActionError | None:
    from frisket.authoring.workbench.plugin_runtime_capabilities import (
        project_allows_plugin_column_type,
    )

    for index, column in enumerate(params.columns):
        type_name = canonical_column_type(column.type)
        type_spec = column_types.get_column_type(type_name)
        if type_spec is None:
            return ActionError(
                code="invalid_column_type",
                message=f"{action_kind} column type is not registered",
                action_kind=action_kind,
                field=f"params.columns[{index}].type",
                details={"type": column.type},
            )
        if not project_allows_plugin_column_type(project, type_spec):
            return ActionError(
                code="invalid_column_type",
                message=f"{action_kind} plugin type is not enabled for this project",
                action_kind=action_kind,
                field=f"params.columns[{index}].type",
                details={"type": column.type, "plugin": type_spec.plugin},
            )
    return None


def _temporal_rows_action_error(
    exc: TimelineError,
    *,
    action_kind: str,
    field: str,
) -> ActionError:
    code = (
        exc.code
        if exc.code in _TEMPORAL_PERSISTENCE_ERROR_MESSAGES
        else "invalid_temporal_value"
    )
    return ActionError(
        code=code,
        message=_TEMPORAL_PERSISTENCE_ERROR_MESSAGES[code],
        action_kind=action_kind,
        field=field,
        details={"reason": exc.message},
    )


def _perform_import_rows_in_txn(
    project: Project,
    cur: Any,
    action: ActionSpec,
    params: Any,
    *,
    project_id: str,
    action_id: str,
    receipt_id: str,
    params_hash: str,
    resolved: Any,
) -> ActionResult:
    """Atomically publish one normalized table for typed or delegated imports."""
    materialization = (
        resolved.get("materialization", params) if resolved is not None else params
    )
    validated_rows = _preflight_table_rows(
        project,
        materialization,
        action_kind=action.kind,
        field="params"
        if resolved is not None and "reads" in resolved
        else "params.rows",
    )
    if isinstance(validated_rows, ActionError):
        return _failed_result(
            project_id=project_id, action_kind=action.kind, error=validated_rows
        )

    source = (
        materialization.source.model_dump(mode="json")
        if materialization.source is not None
        else {}
    )

    cur.execute("UPDATE ops SET status='discarded' WHERE status='undone'")
    op_spec = resolved.get("op_spec") if resolved is not None else None
    if op_spec is None:
        op_spec = _op_spec(action, materialization, params_hash)
    op_kind = (
        resolved.get("op_kind", action.kind) if resolved is not None else action.kind
    )
    cur.execute(
        "INSERT INTO sheets (name, position, parent_sheet_id, parent_op_id) "
        "VALUES (?, (SELECT COALESCE(MAX(position),0)+1 FROM sheets), NULL, NULL)",
        (materialization.sheet_name,),
    )
    sheet_id = int(cur.lastrowid)
    undo_info = {"created_sheets": [sheet_id]}
    cur.execute(
        "INSERT INTO ops (kind, label, spec, undo_info, barrier) VALUES (?, ?, ?, ?, 0)",
        (
            op_kind,
            f"{op_kind} {materialization.sheet_name}",
            json.dumps(op_spec, sort_keys=True),
            json.dumps(undo_info, sort_keys=True),
        ),
    )
    op_id = int(cur.lastrowid)
    cur.execute("UPDATE meta SET value=? WHERE key='op_cursor'", (str(op_id),))
    producer_id = create_base_cell_producer(
        project.db, stage_id=f"op:{op_id}", op_id=op_id
    )

    column_ids: dict[str, int] = {}
    for idx, column in enumerate(materialization.columns, start=1):
        cur.execute(
            "INSERT INTO columns (sheet_id, name, type, position, "
            "ai_generated, hidden, format) VALUES (?, ?, ?, ?, 0, ?, ?)",
            (
                sheet_id,
                column.name,
                canonical_column_type(column.type),
                idx,
                int(column.hidden),
                column.format,
            ),
        )
        column_ids[column.name] = int(cur.lastrowid)

    row_ids: list[int] = []
    cell_writes: list[BaseCellWrite] = []
    for idx, row in enumerate(validated_rows, start=1):
        cur.execute(
            "INSERT INTO rows (sheet_id, position, parent_row_id) VALUES (?, ?, NULL)",
            (sheet_id, idx),
        )
        row_id = int(cur.lastrowid)
        row_ids.append(row_id)
        cell_writes.extend(
            BaseCellWrite(row_id, column_ids[name], value)
            for name, value in row.items()
            if value is not None
        )
    initialize_base_cells(project.db, producer_id=producer_id, cells=cell_writes)

    result_outputs = _import_rows_outputs(
        sheet_name=materialization.sheet_name,
        sheet_id=sheet_id,
        column_ids=column_ids,
        row_ids=row_ids,
        op_id=op_id,
    )
    result = ActionResult(
        action=ActionIdentity(kind=action.kind, action_id=action_id),
        status="completed",
        project_id=project_id,
        op_ids=[op_id],
        outputs=result_outputs,
        receipt_id=receipt_id,
    )
    receipt = _import_rows_receipt(
        action=action,
        action_id=action_id,
        project_id=project_id,
        receipt_id=receipt_id,
        params_hash=params_hash,
        op_id=op_id,
        sheet_name=materialization.sheet_name,
        sheet_id=sheet_id,
        column_ids=column_ids,
        row_ids=row_ids,
        source=source,
    )
    if resolved is not None and "reads" in resolved:
        reads = resolved["reads"]
        table_ref = {
            **receipt.outputs[0].ref,
            "row_count": len(row_ids),
            "columns": column_ids,
            "reads": reads,
        }
        receipt.outputs[0].ref = table_ref
        result.outputs[0].ref = table_ref
        receipt.inputs.extend(
            ReceiptIO(name=f"read.{index}", ref=fact)
            for index, fact in enumerate(reads)
        )
    if resolved is not None and (blob_plan := resolved.get("blob_plan")) is not None:
        from frisket.engine.store.import_blobs import publish_import_blobs

        published = publish_import_blobs(
            project,
            blob_plan.bind_row_ordinals(row_ids),
            sheet_id=sheet_id,
            column_ids=column_ids,
            op_id=op_id,
            receipt_id=receipt_id,
        )
        receipt.evidence.extend(ReceiptEvidence(ref=ref) for ref in published)
    if resolved is not None:
        receipt.warnings = list(resolved.get("warnings", ()))
        result.warnings = list(receipt.warnings)
    ReceiptStore(project).insert_completed(receipt, commit=False)
    return result


def _import_rows_outputs(
    *,
    sheet_name: str,
    sheet_id: int,
    column_ids: dict[str, int],
    row_ids: list[int],
    op_id: int,
) -> list[ActionOutput]:
    outputs = [
        ActionOutput(
            kind="sheet",
            name=sheet_name,
            sheet_id=sheet_id,
            ref={"kind": "materialized_sheet", "sheet_id": sheet_id, "op_id": op_id},
        )
    ]
    outputs.extend(
        ActionOutput(
            kind="column",
            name=name,
            sheet_id=sheet_id,
            column_id=column_id,
            ref={
                "kind": "source_column",
                "sheet_id": sheet_id,
                "column_id": column_id,
                "op_id": op_id,
            },
        )
        for name, column_id in column_ids.items()
    )
    outputs.append(
        ActionOutput(
            kind="rows",
            name="rows",
            sheet_id=sheet_id,
            row_ids=row_ids,
            ref=_compact_import_rowset_ref(
                sheet_id=sheet_id, row_ids=row_ids, op_id=op_id
            ),
        )
    )
    return outputs


def _compact_import_rowset_ref(
    *, sheet_id: int, row_ids: list[int], op_id: int
) -> dict[str, Any]:
    """Bounded identity for an ordered imported rowset.

    The live action result retains ``ActionOutput.row_ids`` for existing local
    callers. Durable receipt refs carry only the count and a stable digest, so
    a large import does not duplicate every row id into outputs and evidence.
    """

    digest = hashlib.sha256()
    for row_id in row_ids:
        digest.update(str(int(row_id)).encode("ascii"))
        digest.update(b"\n")
    return {
        "kind": "source_rows",
        "sheet_id": sheet_id,
        "row_count": len(row_ids),
        "rowset_hash": f"sha256:{digest.hexdigest()}",
        "first_row_id": row_ids[0] if row_ids else None,
        "last_row_id": row_ids[-1] if row_ids else None,
        "op_id": op_id,
    }


def _import_rows_receipt(
    *,
    action: ActionSpec,
    action_id: str,
    project_id: str,
    receipt_id: str,
    params_hash: str,
    op_id: int,
    sheet_name: str,
    sheet_id: int,
    column_ids: dict[str, int],
    row_ids: list[int],
    source: dict[str, Any],
) -> Receipt:
    outputs = [
        ReceiptIO(
            name=sheet_name,
            ref={"kind": "materialized_sheet", "sheet_id": sheet_id, "op_id": op_id},
        ),
        *[
            ReceiptIO(
                name=f"column.{name}",
                ref={
                    "kind": "source_column",
                    "sheet_id": sheet_id,
                    "column_id": column_id,
                    "op_id": op_id,
                },
            )
            for name, column_id in column_ids.items()
        ],
        ReceiptIO(
            name="rows",
            ref=_compact_import_rowset_ref(
                sheet_id=sheet_id, row_ids=row_ids, op_id=op_id
            ),
        ),
    ]
    evidence = [
        ReceiptEvidence(
            ref=_compact_import_rowset_ref(
                sheet_id=sheet_id, row_ids=row_ids, op_id=op_id
            )
        )
    ]
    source_ref = dict(source or {})
    source_kind = source_ref.get("kind")
    if action.kind == "import.ndjson" and source_kind == "file":
        input_name = "ndjson_rows"
        input_ref = {
            "kind": "ndjson_rows",
            "params_hash": params_hash,
            "path": source_ref.get("path"),
            "fingerprint": source_ref.get("fingerprint"),
            "request_hash": source_ref.get("request_hash"),
            "importer": source_ref.get("importer") or "ndjson",
            "line_count": source_ref.get("line_count"),
        }
    elif source_kind == "file":
        input_name = "file_rows"
        input_ref = {
            "kind": "file_rows",
            "params_hash": params_hash,
            "path": source_ref.get("path"),
            "fingerprint": source_ref.get("fingerprint"),
            "label": source_ref.get("label"),
            "importer": source_ref.get("importer"),
            "line_count": source_ref.get("line_count"),
            "request_hash": source_ref.get("request_hash"),
            "skipped_features": source_ref.get("skipped_features", []),
        }
    else:
        input_name = "inline_rows"
        input_ref = {"kind": "inline_rows", "params_hash": params_hash}
    if source:
        source_ref.pop("kind", None)
        source_ref.pop("blob_refs", None)
        if action.kind == "import.ndjson" and source_kind == "file":
            source_ref["transport"] = "file"
            source_ref.setdefault("importer", "ndjson")
            source_kind = "ndjson"
        evidence.append(
            ReceiptEvidence(
                ref={"kind": "import_source", "source_kind": source_kind, **source_ref}
            )
        )
    if row_ids and column_ids:
        first_name = next(iter(column_ids))
        evidence.append(
            ReceiptEvidence(
                ref={
                    "kind": "source_cell",
                    "sheet_id": sheet_id,
                    "row_id": row_ids[0],
                    "column_id": column_ids[first_name],
                    "op_id": op_id,
                }
            )
        )
    return Receipt(
        receipt_id=receipt_id,
        project_id=project_id,
        action_id=action_id,
        action_kind=action.kind,
        op_ids=[op_id],
        idempotency_key=action.idempotency_key,
        params_hash=params_hash,
        status="completed",
        inputs=[
            ReceiptIO(
                name=input_name,
                ref=input_ref,
            )
        ],
        outputs=outputs,
        evidence=evidence,
    )


def _op_spec(
    action: ActionSpec, params: ImportRowsParams, params_hash: str
) -> dict[str, Any]:
    payload = action.model_dump(mode="json")
    payload["params"] = dict(payload["params"])
    payload["params"].pop("rows", None)
    payload["params"]["row_count"] = len(params.rows)
    payload["params"]["params_hash"] = params_hash
    return payload


def _preflight_table_rows(
    project: Project,
    materialization: ImportRowsParams,
    *,
    action_kind: str,
    field: str = "params",
) -> list[dict[str, Any]] | ActionError:
    """Shared persistence validation for source-free and derived tables."""
    try:
        return preflight_typed_rows_for_persistence(
            project,
            column_types_by_name={
                column.name: canonical_column_type(column.type)
                for column in materialization.columns
            },
            rows=materialization.rows,
        )
    except TimelineError as exc:
        return _temporal_rows_action_error(exc, action_kind=action_kind, field=field)
