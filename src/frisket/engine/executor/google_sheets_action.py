"""Admission and queue binding for one pure Google Sheets export producer."""

from __future__ import annotations

from typing import Any, Mapping

from frisket.actions.core import GoogleSheetsExport
from frisket.actions.google_sheets_types import GoogleSheetsExportRequest
from frisket.actions.system import BoundTypedActionRequest, typed_action_for_request
from frisket.contracts.action import ActionError, ActionResult, ReceiptIO
from frisket.engine.executor.action_inventory import (
    ExecutorContext,
    ExecutorDeps,
    _TypedProjectEnvelope,
)
from frisket.engine.executor.action_jobs import (
    ActionJobEnvelope,
    probe_queued_action_job_receipt,
    reserve_queued_action_job_receipt,
)
from frisket.engine.executor.action_support import _failed_result
from frisket.engine.executor.map_rows_action import typed_request_hash
from frisket.engine.store.receipts import ReceiptStore

_INTENT_KIND = "google_sheets_export_intent"


def _identity(bound: BoundTypedActionRequest) -> _TypedProjectEnvelope:
    return _TypedProjectEnvelope(
        kind=bound.action.action_id,
        idempotency_key=bound.request.idempotency_key,
        params=bound.params.model_dump(mode="json"),
    )


def _prepare(bound: BoundTypedActionRequest) -> GoogleSheetsExportRequest:
    terminal = bound.action.definition.run
    if not isinstance(terminal, GoogleSheetsExport):
        raise TypeError("Google Sheets export requires its typed terminal")
    returned = terminal.handler(bound.params)
    if not isinstance(returned, GoogleSheetsExportRequest):
        raise TypeError("export producer must return GoogleSheetsExportRequest")
    # Own the complete validated value; no mutable producer object becomes authority.
    return GoogleSheetsExportRequest.model_validate(returned.model_dump(mode="json"))


def _producer_error(bound: BoundTypedActionRequest, project_id: str) -> ActionResult:
    return _failed_result(
        project_id=project_id,
        action_kind=bound.action.action_id,
        error=ActionError(
            code="invalid_params",
            message="The Google Sheets export producer returned invalid intent.",
            action_kind=bound.action.action_id,
        ),
    )


def run_typed_google_sheets_export(
    project: Any,
    project_id: str,
    bound: BoundTypedActionRequest,
    *,
    deps: ExecutorDeps | None = None,
) -> ActionResult:
    from frisket.engine.executor.action_families.exports import (
        _run_export_google_sheets,
        _emit_export_notification,
    )
    from frisket.engine.executor.action_reservations import (
        _receipt_for_idempotency,
        _reserved_receipt_result_from_existing,
    )

    identity = _identity(bound)
    request_hash = typed_request_hash(bound)
    existing = _receipt_for_idempotency(project, identity.idempotency_key)
    if existing is not None:
        replay = _reserved_receipt_result_from_existing(
            project,
            existing,
            action=identity,
            project_id=project_id,
            params_hash=request_hash,
        )
        _emit_export_notification(project, replay)
        return replay
    try:
        intent = _prepare(bound)
    except Exception:
        return _producer_error(bound, project_id)
    result = _run_export_google_sheets(
        project,
        identity,
        intent,
        project_id=project_id,
        ctx=ExecutorContext(project_id=project_id, deps=deps or ExecutorDeps()),
        params_hash=request_hash,
        confirmation=bound.request.confirmation,
    )
    _emit_export_notification(project, result)
    return result


def prepare_google_sheets_action_job(
    project: Any,
    project_id: str,
    bound: BoundTypedActionRequest,
    *,
    deps: ExecutorDeps,
    edition_run_context: Mapping[str, Any] | None = None,
) -> ActionJobEnvelope | ActionResult:
    from frisket.engine.executor.action_families.exports import google_sheets_admission

    identity = _identity(bound)
    request_hash = typed_request_hash(bound)
    replay = probe_queued_action_job_receipt(
        project, identity, project_id=project_id, params_hash=request_hash
    )
    if replay is not None:
        return replay
    try:
        intent = _prepare(bound)
    except Exception:
        return _producer_error(bound, project_id)
    admission = google_sheets_admission(
        identity,
        intent,
        deps,
        params_hash=request_hash,
        confirmation=bound.request.confirmation,
    )
    if isinstance(admission, ActionError):
        return _failed_result(
            project_id=project_id, action_kind=identity.kind, error=admission
        )
    pinned = {
        "kind": _INTENT_KIND,
        "params_hash": request_hash,
        "intent": intent.model_dump(mode="json"),
    }
    reservation = reserve_queued_action_job_receipt(
        project,
        identity,
        project_id=project_id,
        params_hash=request_hash,
        edition_run_context=edition_run_context,
        prepared_input=ReceiptIO(name=_INTENT_KIND, ref=pinned),
    )
    if isinstance(reservation, ActionResult):
        return reservation
    return ActionJobEnvelope(
        action_kind=identity.kind,
        action_id=reservation["action_id"],
        receipt_id=reservation["receipt_id"],
        params_hash=request_hash,
        idempotency_key=bound.request.idempotency_key,
        project_id=project_id,
        action=bound.request.model_dump(mode="json", exclude_none=True),
        resolved_snapshot=pinned,
        resolve_phase="launch",
    )


def run_google_sheets_action_job(
    project: Any,
    envelope: ActionJobEnvelope,
    *,
    deps: ExecutorDeps | None = None,
) -> ActionResult:
    from frisket.engine.executor.action_families.exports import (
        _run_export_google_sheets,
        _emit_export_notification,
    )

    try:
        bound = typed_action_for_request(dict(envelope.action))
        if (
            not isinstance(bound.action.definition.run, GoogleSheetsExport)
            or bound.request.action_id != envelope.action_kind
            or bound.request.idempotency_key != envelope.idempotency_key
            or typed_request_hash(bound) != envelope.params_hash
            or envelope.resolve_phase != "launch"
        ):
            raise ValueError("invalid export job binding")
        receipt = ReceiptStore(project).parsed_by_id(envelope.receipt_id)
        if receipt is None or (
            receipt.action_id != envelope.action_id
            or receipt.action_kind != envelope.action_kind
            or receipt.project_id != envelope.project_id
            or receipt.idempotency_key != envelope.idempotency_key
            or receipt.params_hash != envelope.params_hash
        ):
            raise ValueError("export job receipt mismatch")
        pins = [
            item.ref for item in receipt.inputs if item.ref.get("kind") == _INTENT_KIND
        ]
        snapshot = dict(envelope.resolved_snapshot or {})
        snapshot.pop("job_id", None)  # generic dispatcher adds transport bookkeeping
        if (
            len(pins) != 1
            or pins[0] != snapshot
            or pins[0].get("params_hash") != envelope.params_hash
        ):
            raise ValueError("export intent does not match its reserved receipt")
        intent = GoogleSheetsExportRequest.model_validate(pins[0]["intent"])
    except (KeyError, TypeError, ValueError):
        return _failed_result(
            project_id=envelope.project_id,
            action_kind=envelope.action_kind,
            error=ActionError(
                code="invalid_action_request",
                message="Google Sheets job intent does not match its reservation.",
                action_kind=envelope.action_kind,
            ),
        )
    result = _run_export_google_sheets(
        project,
        _identity(bound),
        intent,
        project_id=envelope.project_id,
        ctx=ExecutorContext(
            project_id=envelope.project_id, deps=deps or ExecutorDeps()
        ),
        params_hash=envelope.params_hash,
        confirmation=bound.request.confirmation,
        reserved_action_id=envelope.action_id,
        reserved_receipt_id=envelope.receipt_id,
        skip_replay=True,
    )
    if result.status == "needs_confirmation" and result.errors:
        result = _failed_result(
            project_id=envelope.project_id,
            action_kind=envelope.action_kind,
            error=result.errors[0].model_copy(update={"needs_confirmation": False}),
        )
    _emit_export_notification(project, result)
    return result
