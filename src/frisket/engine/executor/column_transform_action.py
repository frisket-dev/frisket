"""Atomic runtime for typed, local whole-column transforms."""

from __future__ import annotations

import inspect
import json
import threading
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from frisket.actions.core import ColumnTransform, RowScope
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.types import (
    ColumnTransformContext,
    Outcome,
    Row,
    RowResult,
    Rows,
    discover_references,
)
from frisket.authoring.column_types import is_registered, validate_value
from frisket.contracts.action import (
    ActionError,
    ActionResult,
    Receipt,
    ReceiptEvidence,
    ReceiptIO,
)
from frisket.engine.executor.action_families._errors import receipt_stale_replay_error
from frisket.engine.executor.action_receipts import (
    _receipt_ops_are_applied,
    _receipt_ref,
    _result_from_receipt,
)
from frisket.engine.executor.action_reservations import _receipt_for_idempotency
from frisket.engine.executor.action_support import _failed_result, _new_id
from frisket.engine.executor.map_rows_action import (
    normalized_typed_request_identity,
    typed_request_hash,
)
from frisket.engine.store.output_claims import OutputColumnClaimStore
from frisket.engine.store.cell_writes import (
    BaseCellWrite,
    create_base_cell_producer,
    initialize_base_cells,
    remove_base_cells,
)
from frisket.engine.store.receipts import ReceiptStore
from frisket.engine.runner.preview import PREVIEW_MAX_ROWS, PreviewColumn, PreviewResult

_SOURCE_REF = "typed_column_transform_source"
_OUTPUT_REF = "materialized_column"


@dataclass(frozen=True)
class ColumnTransformPreviewPlan:
    action_kind: str
    total: int
    run: Callable[[threading.Event | None], PreviewResult]


def _error(
    action_id: str,
    code: str,
    message: str,
    *,
    field: str | None = None,
    details: dict[str, Any] | None = None,
) -> ActionError:
    return ActionError(
        code=code,
        message=message,
        action_kind=action_id,
        field=field,
        details=details or {},
    )


def _failed(project_id: str, error: ActionError) -> ActionResult:
    return _failed_result(
        project_id=project_id,
        action_kind=str(error.action_kind),
        error=error,
    )


def _terminal(bound: BoundTypedActionRequest) -> ColumnTransform[Any, Any]:
    terminal = bound.action.definition.run
    if not isinstance(terminal, ColumnTransform):
        raise TypeError("typed action is not a column_transform")
    if bound.action.definition.row_scope is not RowScope.ALL_ROWS:
        raise TypeError("column_transform must own the complete visible row scope")
    if bound.request.scope.row_ids is not None:
        raise ValueError("column_transform does not accept selected rows")
    if len(bound.output_fields) != 1:
        raise TypeError("column_transform must declare exactly one output")
    references = discover_references(bound.params)
    if len(references) != 1:
        raise TypeError("column_transform must declare exactly one source column")
    return terminal


def preflight_column_transform_source(
    project: Any,
    *,
    action_id: str,
    terminal: ColumnTransform[Any, Any],
    params: Any,
    sheet_id: int,
) -> dict[str, Any] | ActionError:
    """Resolve the one admitted source and run its type-dependent preflight."""

    sheet = project.db.execute(
        "SELECT id, name FROM sheets WHERE id=? AND hidden=0", (sheet_id,)
    ).fetchone()
    if sheet is None:
        return _error(
            action_id,
            "invalid_input_ref",
            "source sheet does not exist",
            field="scope.sheet_id",
            details={"sheet_id": sheet_id},
        )
    references = discover_references(params)
    if len(references) != 1:
        return _error(
            action_id,
            "invalid_input_ref",
            "column transform must declare exactly one source column",
            field="params",
        )
    [reference] = references
    column = project.db.execute(
        "SELECT id, name, type FROM columns WHERE sheet_id=? AND name=? AND hidden=0",
        (sheet_id, reference.column),
    ).fetchone()
    if column is None:
        return _error(
            action_id,
            "invalid_input_ref",
            "source column does not exist",
            field="params",
            details={"column": reference.column},
        )
    source_type = str(column["type"])
    accepted = reference.accepted_column_types
    if accepted is not None and source_type not in accepted:
        return _error(
            action_id,
            "invalid_input_ref",
            "source column has an incompatible type",
            field="params",
            details={
                "column": reference.column,
                "actual_type": source_type,
                "accepted_column_types": list(accepted),
            },
        )
    column_id = int(column["id"])
    descriptor = {
        "sheet_id": sheet_id,
        "sheet_name": str(sheet["name"]),
        "column_id": column_id,
        "column_name": reference.column,
        "column_type": source_type,
    }
    if terminal.preflight is not None:
        try:
            terminal.preflight(
                params,
                ColumnTransformContext(source_type=source_type),
            )
        except (TypeError, ValueError) as exc:
            return _error(action_id, "invalid_params", str(exc), field="params")
    return descriptor


def _source_descriptor(
    project: Any, bound: BoundTypedActionRequest
) -> dict[str, Any] | ActionError:
    return preflight_column_transform_source(
        project,
        action_id=bound.action.action_id,
        terminal=_terminal(bound),
        params=bound.params,
        sheet_id=bound.request.scope.sheet_id,
    )


def _source(
    project: Any, bound: BoundTypedActionRequest
) -> dict[str, Any] | ActionError:
    descriptor = _source_descriptor(project, bound)
    if isinstance(descriptor, ActionError):
        return descriptor
    sheet_id = int(descriptor["sheet_id"])
    column_id = int(descriptor["column_id"])
    row_ids = [int(row_id) for row_id in project.visible_row_ids(sheet_id)]
    stored = project.get_values(sheet_id, column_id)
    values_by_row = {row_id: stored.get(row_id) for row_id in row_ids}
    return {
        **descriptor,
        "row_ids": row_ids,
        "values_by_row": values_by_row,
    }


def _compute(
    bound: BoundTypedActionRequest,
    source: Mapping[str, Any],
) -> tuple[dict[int, Any], str]:
    terminal = _terminal(bound)
    rows = Rows(
        {
            row_id: Row({source["column_name"]: source["values_by_row"][row_id]})
            for row_id in source["row_ids"]
        }
    )
    context = ColumnTransformContext(source_type=str(source["column_type"]))
    args = (
        (bound.params, rows, context)
        if terminal.takes_context
        else (bound.params, rows)
    )
    raw = terminal.handler(*args)
    if inspect.isawaitable(raw):
        raise TypeError("column_transform handlers must be synchronous")
    if not isinstance(raw, Mapping) or set(raw) != set(source["row_ids"]):
        raise ValueError(
            "column_transform must return exactly one result per source row"
        )

    output_key = terminal.output_fields[0].key
    typed: dict[int, RowResult[Any]] = {}
    values: dict[int, Any] = {}
    for row_id in source["row_ids"]:
        result = raw[row_id]
        if not isinstance(result, RowResult):
            raise TypeError("column_transform returned a non-RowResult value")
        output = terminal.output_model.model_validate(result.output)
        value = getattr(output, output_key)
        if isinstance(value, Outcome):
            raise TypeError("column_transform outputs must be plain values")
        typed[row_id] = RowResult(output=output)
        values[row_id] = value

    output_field = terminal.resolved_output_field(bound.params, context, typed)
    preferred_type = output_field.column_type
    if not is_registered(preferred_type):
        raise TypeError(f"unknown output column type {preferred_type!r}")
    # Imported source columns may already contain exceptional values (for
    # example ``"n/a"`` in a number column). A transform may preserve those
    # exact cells. Dynamic transforms choose the narrowest declared/fallback
    # type that accepts every value they actually changed.
    changed_values = [
        value
        for row_id, value in values.items()
        if not (
            type(value) is type(source["values_by_row"][row_id])
            and value == source["values_by_row"][row_id]
        )
    ]
    candidates = [preferred_type]
    if terminal.output_type is not None:
        candidates.extend(
            candidate for candidate in ("number", "text") if candidate not in candidates
        )
    output_type = next(
        (
            candidate
            for candidate in candidates
            if all(validate_value(candidate, value) for value in changed_values)
        ),
        None,
    )
    if output_type is None:
        raise ValueError(
            "column_transform returned values incompatible with its output type"
        )
    if not all(
        (
            type(value) is type(source["values_by_row"][row_id])
            and value == source["values_by_row"][row_id]
        )
        or validate_value(output_type, value)
        for row_id, value in values.items()
    ):
        raise ValueError(
            "column_transform returned values incompatible with its output type"
        )
    return values, output_type


def _output_gate(
    project: Any,
    cur: Any,
    *,
    action_id: str,
    sheet_id: int,
    output_name: str,
) -> ActionError | None:
    existing = cur.execute(
        "SELECT id FROM columns WHERE sheet_id=? AND name=? AND hidden=0",
        (sheet_id, output_name),
    ).fetchone()
    if existing is not None:
        return _error(
            action_id,
            "output_column_exists",
            "output column already exists",
            field="output_names",
            details={"sheet_id": sheet_id, "output_name": output_name},
        )
    claim = OutputColumnClaimStore(project).active_for_output_name(
        sheet_id=sheet_id, output_name=output_name
    )
    if claim is None:
        return None
    return _error(
        action_id,
        "output_column_busy",
        "The target output column is claimed by a running action.",
        field="output_names",
        details={"sheet_id": sheet_id, "output_name": output_name},
    )


def _preview_output_gate(
    project: Any,
    *,
    action_id: str,
    sheet_id: int,
    output_name: str,
) -> ActionError | None:
    """Read-only form of the run gate for preview planning."""

    existing = project.db.execute(
        "SELECT id FROM columns WHERE sheet_id=? AND name=? AND hidden=0",
        (sheet_id, output_name),
    ).fetchone()
    if existing is not None:
        return _error(
            action_id,
            "output_column_exists",
            "output column already exists",
            field="output_names",
            details={"sheet_id": sheet_id, "output_name": output_name},
        )
    claim = OutputColumnClaimStore(project).active_for_output_name(
        sheet_id=sheet_id,
        output_name=output_name,
        recover_stale=False,
    )
    if claim is None:
        return None
    return _error(
        action_id,
        "output_column_busy",
        "The target output column is claimed by a running action.",
        field="output_names",
        details={"sheet_id": sheet_id, "output_name": output_name},
    )


def preflight_column_transform_request(
    project: Any, bound: BoundTypedActionRequest
) -> dict[str, Any] | ActionError:
    """Run the cheap source and output gates shared by estimate and preview."""

    source = _source_descriptor(project, bound)
    if isinstance(source, ActionError):
        return source
    [field] = bound.output_fields
    output_name = bound.request.output_names.get(field.key, field.key)
    gate = _preview_output_gate(
        project,
        action_id=bound.action.action_id,
        sheet_id=int(source["sheet_id"]),
        output_name=output_name,
    )
    return gate if gate is not None else source


def _write_column(
    cur: Any,
    *,
    action_id: str,
    sheet_id: int,
    source_name: str,
    output_name: str,
    output_type: str,
    values: Mapping[int, Any],
    op_spec: Mapping[str, Any],
) -> tuple[int, int]:
    cur.execute("UPDATE ops SET status='discarded' WHERE status='undone'")
    cur.execute(
        "INSERT INTO ops (kind, label, spec, undo_info, barrier) VALUES (?, ?, ?, ?, 0)",
        (
            action_id,
            f"{action_id.rsplit('.', 1)[-1].removesuffix('_missing')} "
            f"{source_name} → {output_name}",
            json.dumps(op_spec, sort_keys=True),
            "{}",
        ),
    )
    op_id = int(cur.lastrowid)
    cur.execute("UPDATE meta SET value=? WHERE key='op_cursor'", (str(op_id),))
    db = cur.connection
    producer_id = create_base_cell_producer(db, stage_id=f"op:{op_id}", op_id=op_id)
    hidden = cur.execute(
        "SELECT id FROM columns WHERE sheet_id=? AND name=? AND hidden=1",
        (sheet_id, output_name),
    ).fetchone()
    if hidden is None:
        position = int(
            cur.execute(
                "SELECT COALESCE(MAX(position),0)+1 FROM columns WHERE sheet_id=?",
                (sheet_id,),
            ).fetchone()[0]
        )
        cur.execute(
            "INSERT INTO columns (sheet_id, name, type, position, ai_generated, hidden, format) "
            "VALUES (?, ?, ?, ?, 1, 0, NULL)",
            (sheet_id, output_name, output_type, position),
        )
        column_id = int(cur.lastrowid)
    else:
        column_id = int(hidden["id"])
        cur.execute(
            "UPDATE columns SET hidden=0, type=?, ai_generated=1, current_run_id=NULL "
            "WHERE id=?",
            (output_type, column_id),
        )
        remove_base_cells(
            db,
            producer_id=producer_id,
            column_ids=[column_id],
        )
    initialize_base_cells(
        db,
        producer_id=producer_id,
        cells=(
            BaseCellWrite(row_id, column_id, value)
            for row_id, value in values.items()
            if value is not None
        ),
    )
    cur.execute(
        "UPDATE ops SET undo_info=? WHERE id=?",
        (json.dumps({"created_columns": [column_id]}, sort_keys=True), op_id),
    )
    return op_id, column_id


def _receipt(
    bound: BoundTypedActionRequest,
    *,
    project_id: str,
    action_instance_id: str,
    receipt_id: str,
    params_hash: str,
    source: Mapping[str, Any],
    output_name: str,
    output_type: str,
    output_column_id: int,
    op_id: int,
) -> Receipt:
    source_ref = {
        "kind": _SOURCE_REF,
        "sheet_id": source["sheet_id"],
        "sheet_name": source["sheet_name"],
        "column_id": source["column_id"],
        "name": source["column_name"],
        "type": source["column_type"],
    }
    output_ref = {
        "kind": _OUTPUT_REF,
        "sheet_id": source["sheet_id"],
        "column_id": output_column_id,
        "name": output_name,
        "type": output_type,
        "op_id": op_id,
        "row_ids": list(source["row_ids"]),
        "row_count": len(source["row_ids"]),
    }
    request_ref = {
        "kind": "typed_action_request",
        **normalized_typed_request_identity(bound),
        "params_hash": params_hash,
        "op_id": op_id,
    }
    return Receipt(
        receipt_id=receipt_id,
        project_id=project_id,
        action_id=action_instance_id,
        action_kind=bound.action.action_id,
        op_ids=[op_id],
        idempotency_key=bound.request.idempotency_key,
        params_hash=params_hash,
        status="completed",
        inputs=[
            ReceiptIO(name="source_column", kind="column", ref=source_ref),
        ],
        outputs=[ReceiptIO(name=output_name, ref=output_ref)],
        provider_use=[
            {
                "provider": "local",
                "service": f"frisket.{bound.action.action_id}",
                "external_api": False,
                "cost_actual": 0.0,
            }
        ],
        evidence=[
            ReceiptEvidence(ref=request_ref),
            ReceiptEvidence(ref=source_ref),
            ReceiptEvidence(ref=output_ref),
            ReceiptEvidence(
                ref={
                    "kind": "column_transform_counts",
                    "total_rows": len(source["row_ids"]),
                    "completed_rows": len(source["row_ids"]),
                    "failed_rows": 0,
                    "op_id": op_id,
                }
            ),
        ],
    )


def column_transform_replay_error(project: Any, receipt: Receipt) -> ActionError | None:
    if not receipt.op_ids or not _receipt_ops_are_applied(project, receipt.op_ids):
        return receipt_stale_replay_error(
            receipt, "column transform commit is no longer applied"
        )
    output_ref = _receipt_ref(receipt, _OUTPUT_REF)
    source_ref = _receipt_ref(receipt, _SOURCE_REF)
    if not all(isinstance(ref, dict) for ref in (output_ref, source_ref)):
        return receipt_stale_replay_error(
            receipt, "column transform receipt is missing replay evidence"
        )
    assert isinstance(output_ref, dict)
    assert isinstance(source_ref, dict)
    output = project.db.execute(
        "SELECT id, name, type, hidden, sheet_id FROM columns WHERE id=?",
        (output_ref.get("column_id"),),
    ).fetchone()
    if (
        output is None
        or output["hidden"]
        or output["name"] != output_ref.get("name")
        or output["type"] != output_ref.get("type")
        or output["sheet_id"] != output_ref.get("sheet_id")
    ):
        return receipt_stale_replay_error(
            receipt, "column transform output column changed"
        )
    sheet_id = source_ref.get("sheet_id")
    column_id = source_ref.get("column_id")
    source = project.db.execute(
        "SELECT id, name, type, hidden FROM columns WHERE id=? AND sheet_id=?",
        (column_id, sheet_id),
    ).fetchone()
    sheet = project.db.execute(
        "SELECT id FROM sheets WHERE id=? AND hidden=0", (sheet_id,)
    ).fetchone()
    if (
        sheet is None
        or source is None
        or source["hidden"]
        or source["name"] != source_ref.get("name")
        or source["type"] != source_ref.get("type")
    ):
        return receipt_stale_replay_error(
            receipt, "column transform source column changed"
        )
    return None


def _replay(
    project: Any,
    existing: Any,
    *,
    project_id: str,
    bound: BoundTypedActionRequest,
    params_hash: str,
) -> ActionResult:
    if existing["params_hash"] != params_hash:
        return _failed(
            project_id,
            _error(
                bound.action.action_id,
                "idempotency_conflict",
                "idempotency key was already used for different work",
                field="idempotency_key",
            ),
        )
    receipt = Receipt.model_validate(json.loads(existing["body"]))
    if replay_error := column_transform_replay_error(project, receipt):
        return _failed(project_id, replay_error)
    return _result_from_receipt(receipt)


def run_typed_column_transform_action(
    project: Any,
    project_id: str,
    bound: BoundTypedActionRequest,
) -> ActionResult:
    """Compute and publish a typed whole-column transform under one write lock."""

    try:
        _terminal(bound)
    except (TypeError, ValueError) as exc:
        return _failed(
            project_id,
            _error(bound.action.action_id, "invalid_action_request", str(exc)),
        )
    params_hash = typed_request_hash(bound)
    with project.read_snapshot() as snapshot:
        existing = _receipt_for_idempotency(snapshot, bound.request.idempotency_key)
        if existing is not None:
            return _replay(
                snapshot,
                existing,
                project_id=project_id,
                bound=bound,
                params_hash=params_hash,
            )

    cur = project.db.cursor()
    try:
        cur.execute("BEGIN IMMEDIATE")
        existing = _receipt_for_idempotency(project, bound.request.idempotency_key)
        if existing is not None:
            result = _replay(
                project,
                existing,
                project_id=project_id,
                bound=bound,
                params_hash=params_hash,
            )
            project.db.rollback()
            return result
        source = _source(project, bound)
        if isinstance(source, ActionError):
            project.db.rollback()
            return _failed(project_id, source)
        output_key = bound.output_fields[0].key
        output_name = bound.request.output_names.get(output_key, output_key)
        gate = _output_gate(
            project,
            cur,
            action_id=bound.action.action_id,
            sheet_id=int(source["sheet_id"]),
            output_name=output_name,
        )
        if gate is not None:
            project.db.rollback()
            return _failed(project_id, gate)
        try:
            values, output_type = _compute(bound, source)
        except (TypeError, ValueError) as exc:
            project.db.rollback()
            return _failed(
                project_id,
                _error(
                    bound.action.action_id,
                    "invalid_params",
                    str(exc),
                    field="params",
                ),
            )
        request_identity = normalized_typed_request_identity(bound)
        op_spec = {
            "action_id": bound.action.action_id,
            "request": request_identity,
            "params_hash": params_hash,
            "source": {
                "sheet_id": source["sheet_id"],
                "column_id": source["column_id"],
                "column_name": source["column_name"],
                "column_type": source["column_type"],
            },
            "output_name": output_name,
        }
        op_id, output_column_id = _write_column(
            cur,
            action_id=bound.action.action_id,
            sheet_id=int(source["sheet_id"]),
            source_name=str(source["column_name"]),
            output_name=output_name,
            output_type=output_type,
            values=values,
            op_spec=op_spec,
        )
        action_instance_id = _new_id("act")
        receipt_id = _new_id("receipt")
        receipt = _receipt(
            bound,
            project_id=project_id,
            action_instance_id=action_instance_id,
            receipt_id=receipt_id,
            params_hash=params_hash,
            source=source,
            output_name=output_name,
            output_type=output_type,
            output_column_id=output_column_id,
            op_id=op_id,
        )
        result = _result_from_receipt(receipt)
        ReceiptStore(project).insert_completed(receipt, commit=False)
        project.db.commit()
        return result
    except Exception:
        project.db.rollback()
        return _failed(
            project_id,
            _error(
                bound.action.action_id,
                "project_write_failed",
                "column transform failed without changing the project",
            ),
        )


def build_column_transform_preview_plan(
    project: Any,
    bound: BoundTypedActionRequest,
) -> ColumnTransformPreviewPlan | ActionError:
    """Plan a full-column computation with a bounded display result."""

    try:
        _terminal(bound)
    except (TypeError, ValueError) as exc:
        return _error(
            bound.action.action_id,
            "invalid_params",
            str(exc),
            field="params",
        )
    with project.read_snapshot() as snapshot:
        source = preflight_column_transform_request(snapshot, bound)
        if isinstance(source, ActionError):
            return source
        total = int(
            snapshot.db.execute(
                "SELECT COUNT(*) FROM rows WHERE sheet_id=? AND hidden=0",
                (source["sheet_id"],),
            ).fetchone()[0]
        )
    [field] = bound.output_fields
    output_name = bound.request.output_names.get(field.key, field.key)

    def run(cancel_event: threading.Event | None = None) -> PreviewResult:
        if cancel_event is not None and cancel_event.is_set():
            raise ValueError("preview cancelled")
        with project.read_snapshot() as snapshot:
            source = _source(snapshot, bound)
            if isinstance(source, ActionError):
                raise ValueError(f"{source.code}: {source.message}")
            if cancel_event is not None and cancel_event.is_set():
                raise ValueError("preview cancelled")
            values, output_type = _compute(bound, source)
            if cancel_event is not None and cancel_event.is_set():
                raise ValueError("preview cancelled")
            row_ids = list(source["row_ids"])
        sample_ids = row_ids[:PREVIEW_MAX_ROWS]
        return PreviewResult(
            sheet_id=int(source["sheet_id"]),
            columns=[
                PreviewColumn(
                    name=output_name,
                    column_type=output_type,
                    overwrites_column_id=None,
                )
            ],
            values={
                row_id: {output_name: {"value": values[row_id]}}
                for row_id in sample_ids
            },
            row_ids=sample_ids,
            sampled=len(sample_ids),
            total=len(row_ids),
        )

    return ColumnTransformPreviewPlan(
        action_kind=bound.action.action_id,
        total=total,
        run=run,
    )
