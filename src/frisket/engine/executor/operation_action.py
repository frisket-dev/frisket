"""Host-owned execution for typed undo and redo actions."""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Literal, Mapping

from frisket.actions.core import _ProjectAction
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.types import (
    OperationRedoer,
    OperationTransition,
    OperationUndoer,
)
from frisket.contracts.action import (
    ActionError,
    ActionIdentity,
    ActionOutput,
    ActionResult,
    Receipt,
    ReceiptEvidence,
    ReceiptIO,
)
from frisket.engine.executor.action_inventory import (
    ExecutorContext,
    ExecutorDeps,
    _ActionCoreSpec,
    _TypedProjectEnvelope,
)
from frisket.engine.executor.action_lifecycle import (
    _child_sheet_deterministic_result_from_existing,
    _run_action_core_spec,
)
from frisket.engine.executor.action_support import _failed_result
from frisket.engine.executor.map_rows_action import typed_request_hash
from frisket.engine.store.op_log import (
    ClaimedOperation,
    CorruptOperation,
    IrreversibleOperation,
    OperationMismatch,
    OperationUnavailable,
    operation_affected_refs,
    step_operation,
)
from frisket.engine.store.receipts import ReceiptStore


logger = logging.getLogger("frisket.executor")


class _Refusal(Exception):
    def __init__(self, error: ActionError):
        self.error = error
        super().__init__(error.message)


def _refuse(
    code: str,
    message: str,
    *,
    action_kind: str,
    field: str | None = None,
    details: Mapping[str, Any] | None = None,
) -> None:
    raise _Refusal(
        ActionError(
            code=code,
            message=message,
            action_kind=action_kind,
            field=field,
            details=dict(details or {}),
        )
    )


def _text_hash(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


class _OperationStep:
    direction: Literal["undo", "redo"]

    def __init__(self, project: Any, action: _TypedProjectEnvelope):
        self._project = project
        self._action = action
        self._called = False
        self.result: OperationTransition | None = None
        self.target: Any = None
        self.undo_info: dict[str, Any] | None = None

    def _step(self, *, expected_op_id: int | None) -> OperationTransition:
        if self._called:
            raise RuntimeError("operation capability may be called only once")
        self._called = True
        try:
            transition = step_operation(
                self._project,
                self.direction,
                expected_op_id=expected_op_id,
            )
        except OperationUnavailable:
            _refuse(
                "operation_unavailable",
                f"No operation is available to {self.direction}",
                action_kind=self._action.kind,
                details={
                    "direction": self.direction,
                    "cursor": self._project.op_cursor,
                },
            )
        except OperationMismatch as exc:
            _refuse(
                "operation_mismatch",
                "Available operation does not match expected_op_id",
                action_kind=self._action.kind,
                field="params.expected_op_id",
                details={
                    "direction": self.direction,
                    "expected_op_id": exc.expected_op_id,
                    "actual_op_id": exc.actual_op_id,
                },
            )
        except IrreversibleOperation as exc:
            target = exc.args[0]
            _refuse(
                "irreversible_barrier",
                "Target operation is an irreversible barrier",
                action_kind=self._action.kind,
                details={
                    "op_id": int(target["id"]),
                    "operation_kind": target["kind"],
                },
            )
        except CorruptOperation as exc:
            details: dict[str, Any] = {"op_id": exc.op_id}
            if isinstance(exc.__cause__, json.JSONDecodeError):
                details["error"] = str(exc.__cause__)
            _refuse(
                "project_state_corrupt",
                str(exc),
                action_kind=self._action.kind,
                details=details,
            )
        except ClaimedOperation as exc:
            claim = exc.claim
            _refuse(
                "output_column_busy",
                "The target output column is claimed by a running action.",
                action_kind=self._action.kind,
                field="params.expected_op_id",
                details={
                    "direction": self.direction,
                    "op_id": exc.op_id,
                    "column_id": claim["column_id"],
                    "output_name": claim["output_name"],
                    "run_id": claim["run_id"],
                    "receipt_id": claim["receipt_id"],
                    "job_id": claim["job_id"],
                    "claim_id": claim["id"],
                    "claim_action_kind": claim["action_kind"],
                    "requires_recovery": True,
                },
            )
        result = OperationTransition(
            op_id=int(transition.target["id"]),
            operation_kind=str(transition.target["kind"]),
            direction=transition.direction,
            status_before=transition.status_before,
            status_after=transition.status_after,
            cursor_before=transition.cursor_before,
            cursor_after=transition.cursor_after,
            affected_refs=tuple(operation_affected_refs(transition.undo_info)),
        )
        self.target = transition.target
        self.undo_info = transition.undo_info
        self.result = result
        return result


class _Undoer(_OperationStep):
    direction = "undo"

    def undo(self, *, expected_op_id: int | None) -> OperationTransition:
        return self._step(expected_op_id=expected_op_id)


class _Redoer(_OperationStep):
    direction = "redo"

    def redo(self, *, expected_op_id: int | None) -> OperationTransition:
        return self._step(expected_op_id=expected_op_id)


_CAPABILITY_IMPL = {OperationUndoer: _Undoer, OperationRedoer: _Redoer}


def supports_typed_operation_action(terminal: object) -> bool:
    return isinstance(terminal, _ProjectAction) and any(
        terminal.capabilities == (cap,) for cap in _CAPABILITY_IMPL
    )


def _transition_ref(transition: OperationTransition) -> dict[str, Any]:
    return {
        "kind": "operation_status_transition",
        "direction": transition.direction,
        "op_id": transition.op_id,
        "operation_kind": transition.operation_kind,
        "status_before": transition.status_before,
        "status_after": transition.status_after,
        "cursor_before": transition.cursor_before,
        "cursor_after": transition.cursor_after,
        "affected_refs": [dict(ref) for ref in transition.affected_refs],
    }


def _result_and_receipt(
    capability: _OperationStep,
    *,
    action: _TypedProjectEnvelope,
    project_id: str,
    action_id: str,
    receipt_id: str,
    params_hash: str,
) -> tuple[ActionResult, Receipt]:
    transition = capability.result
    target = capability.target
    undo_info = capability.undo_info
    if transition is None or target is None or undo_info is None:
        raise RuntimeError("operation capability did not produce receipt facts")
    transition_ref = _transition_ref(transition)
    output_name = f"{transition.direction}.{transition.op_id}"
    result = ActionResult(
        action=ActionIdentity(kind=action.kind, action_id=action_id),
        status="completed",
        project_id=project_id,
        op_ids=[transition.op_id],
        outputs=[ActionOutput(kind="operation", name=output_name, ref=transition_ref)],
        receipt_id=receipt_id,
    )
    receipt = Receipt(
        receipt_id=receipt_id,
        project_id=project_id,
        action_id=action_id,
        action_kind=action.kind,
        op_ids=[transition.op_id],
        idempotency_key=action.idempotency_key,
        params_hash=params_hash,
        status="completed",
        inputs=[
            ReceiptIO(
                name="target_operation",
                ref={
                    "kind": "target_operation",
                    "op_id": transition.op_id,
                    "operation_kind": transition.operation_kind,
                    "label": target["label"],
                    "barrier": bool(target["barrier"]),
                    "status_before": transition.status_before,
                    "cursor_before": transition.cursor_before,
                },
            )
        ],
        outputs=[ReceiptIO(name=output_name, ref=transition_ref)],
        evidence=[
            ReceiptEvidence(
                ref={
                    "kind": "operation_cursor",
                    "direction": transition.direction,
                    "op_id": transition.op_id,
                    "cursor_before": transition.cursor_before,
                    "cursor_after": transition.cursor_after,
                    "status_before": transition.status_before,
                    "status_after": transition.status_after,
                }
            ),
            ReceiptEvidence(
                ref={
                    "kind": "operation_undo_info",
                    "op_id": transition.op_id,
                    "operation_kind": transition.operation_kind,
                    "undo_info": undo_info,
                },
                retention="pinned",
            ),
            ReceiptEvidence(
                ref={
                    "kind": "operation_snapshot",
                    "op_id": transition.op_id,
                    "operation_kind": transition.operation_kind,
                    "label": target["label"],
                    "barrier": bool(target["barrier"]),
                    "status_before": transition.status_before,
                    "spec_hash": _text_hash(target["spec"] or "{}"),
                    "undo_info_hash": _text_hash(target["undo_info"] or "{}"),
                },
                retention="pinned",
            ),
        ],
    )
    return result, receipt


def _perform(
    project: Any,
    cur: Any,
    action: _TypedProjectEnvelope,
    params: Any,
    *,
    terminal: _ProjectAction[Any, Any],
    project_id: str,
    action_id: str,
    receipt_id: str,
    params_hash: str,
    resolved: Any,
) -> ActionResult:
    del cur, resolved
    capability = _CAPABILITY_IMPL[terminal.single_capability()](project, action)
    try:
        returned = terminal.handler(params, capability)
    except _Refusal as exc:
        return _failed_result(
            project_id=project_id,
            action_kind=action.kind,
            error=exc.error,
        )
    if capability.result is None or returned != capability.result:
        raise TypeError("operation handler must return its capability result")
    result, receipt = _result_and_receipt(
        capability,
        action=action,
        project_id=project_id,
        action_id=action_id,
        receipt_id=receipt_id,
        params_hash=params_hash,
    )
    ReceiptStore(project).insert_completed(receipt, commit=False)
    return result


def run_typed_operation_action(
    project: Any, project_id: str, bound: BoundTypedActionRequest
) -> ActionResult:
    terminal = bound.action.definition.run
    if not supports_typed_operation_action(terminal):
        raise TypeError("typed operation executor requires undo or redo")
    envelope = _TypedProjectEnvelope(
        kind=bound.action.action_id,
        idempotency_key=bound.request.idempotency_key,
        params=bound.params.model_dump(mode="json"),
    )
    params_hash = typed_request_hash(bound)
    spec = _ActionCoreSpec(
        kind=envelope.kind,
        params_model=terminal.params_model,
        body_kind="plain",
        params_hash_fn=lambda _action: params_hash,
        result_from_existing_fn=_child_sheet_deterministic_result_from_existing(),
        plain_perform_in_txn_fn=lambda project_, cur, action, params, **kwargs: (
            _perform(project_, cur, action, params, terminal=terminal, **kwargs)
        ),
        plain_exception_error_fn=lambda action: ActionError(
            code="project_write_failed",
            message=f"{action.kind} could not write operation transition and receipt",
            action_kind=action.kind,
        ),
    )
    result = _run_action_core_spec(
        project,
        envelope,
        bound.params,
        spec=spec,
        ctx=ExecutorContext(project_id=project_id, deps=ExecutorDeps()),
    )
    if result.status == "completed":
        try:
            project.refresh_pending_review_summary()
        except Exception:
            logger.warning(
                "operation_pending_review_refresh_failed",
                exc_info=True,
                extra={
                    "event": "operation_pending_review_refresh_failed",
                    "action_kind": envelope.kind,
                    "project_id": project_id,
                },
            )
    return result
