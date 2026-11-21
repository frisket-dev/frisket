"""Reserved execution of plain callables; primitives own their atomicity."""

from __future__ import annotations

import asyncio
import json
import math
import uuid
from dataclasses import dataclass, fields, is_dataclass
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from frisket.engine.executor.action_inventory import _typed_params_envelope
from frisket.actions.core import _ProjectAction
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.entity_package_types import (
    FollowTheMoneyImporter,
    FollowTheMoneyExporter,
)
from frisket.actions.types import (
    InvocationContext,
    PluginSecrets,
    QueryPreviewer,
    SheetCsvExporter,
    SheetJsonlExporter,
    SheetParquetExporter,
    WorkLogExporter,
)
from frisket.contracts.action import ActionError, ActionResult, Receipt
from frisket.engine.executor.action_inventory import ExecutorDeps, _TypedProjectEnvelope
from frisket.engine.executor.action_receipts import _result_from_receipt
from frisket.engine.executor.action_reservations import (
    _finalize_reserved_action_receipt,
    _finalize_reserved_action_receipt_in_transaction,
    _reserve_running_action_receipt,
)
from frisket.engine.store.transactions import require_transaction
from frisket.engine.executor.action_support import ActionCtx
from frisket.engine.executor.invocation_context import HostInvocationContext
from frisket.engine.executor.map_rows_action import typed_request_hash
from frisket.engine.executor.query_action import QueryPreviewCapability
from frisket.engine.executor.action_families.exports import (
    SheetCsvExportCapability,
    SheetJsonlExportCapability,
    SheetParquetExportCapability,
    WorkLogExportCapability,
    _export_artifact_replay_error,
    _emit_export_notification,
)
from frisket.engine.store.receipts import ReceiptStore
from frisket.engine.sandbox.shim import SandboxTeardownError
from frisket.preview.query import QueryPreviewError
from frisket.engine.executor.entity_package import (
    FollowTheMoneyImportCapability,
    FollowTheMoneyExportCapability,
    entity_effect_replay_error,
)


class CallablePrimitiveError(Exception):
    def __init__(self, error: ActionError):
        super().__init__(error.message)
        self.error = error


_CALLABLE_CAPABILITIES = {
    FollowTheMoneyImporter: FollowTheMoneyImportCapability,
    FollowTheMoneyExporter: FollowTheMoneyExportCapability,
    QueryPreviewer: QueryPreviewCapability,
    SheetCsvExporter: SheetCsvExportCapability,
    SheetJsonlExporter: SheetJsonlExportCapability,
    SheetParquetExporter: SheetParquetExportCapability,
    WorkLogExporter: WorkLogExportCapability,
}


def _require_finite_domain_value(value: Any, seen: set[int] | None = None) -> None:
    """Check native structured values before an inferred serializer can null NaN."""
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("callable return contains a non-finite number")
    seen = set() if seen is None else seen
    if id(value) in seen:
        return  # The serializer still owns cycle/unsupported-value refusal.
    seen.add(id(value))
    if isinstance(value, BaseModel):
        children = (
            *value.__dict__.values(),
            *(value.__pydantic_extra__ or {}).values(),
        )
    elif is_dataclass(value) and not isinstance(value, type):
        children = (getattr(value, item.name) for item in fields(value))
    elif isinstance(value, dict):
        children = (*value.keys(), *value.values())
    elif isinstance(value, (list, tuple, set, frozenset)):
        children = value
    else:
        return
    for child in children:
        _require_finite_domain_value(child, seen)


def _publication_json(terminal: _ProjectAction, value: Any) -> str:
    _require_finite_domain_value(value)
    serialized = terminal.return_serializer.to_python(
        value, mode="json", by_alias=True, warnings="error"
    )
    return json.dumps(serialized, allow_nan=False, sort_keys=True)


def _existing_result(project, existing, *, params_hash, project_id, action, **_ignored):
    if existing["params_hash"] != params_hash:
        error = ActionError(
            code="idempotency_conflict",
            message="This key was used with different parameters.",
            action_kind=action.kind,
            field="idempotency_key",
        )
    elif existing["status"] in {"running", "queued"}:
        # Even an old reservation may follow a completed filesystem effect.
        # Never clear it or replay arbitrary authored code automatically.
        error = ActionError(
            code="idempotency_in_progress",
            message="This callable is running or was interrupted; its effects cannot be safely repeated.",
            action_kind=action.kind,
            field="idempotency_key",
            details={"receipt_id": existing["id"]},
        )
    else:
        receipt = Receipt.model_validate_json(existing["body"])
        error = entity_effect_replay_error(project, receipt)
        if error is None and any(
            ref.get("kind") != "export_project_file" for ref in receipt.exports
        ):
            error = _export_artifact_replay_error(receipt)
        if error is None:
            return _result_from_receipt(receipt)
    return ActionCtx(project_id, action.kind).failed(error)


def _artifact_target(ref: dict[str, Any]) -> str | None:
    if ref.get("kind") == "export_artifact" and isinstance(ref.get("path"), str):
        return ref["path"]
    return None


@dataclass(frozen=True)
class CallableRecordUpdate:
    """Observed receipt fields to record inside a caller-owned transaction."""

    inputs: tuple[Any, ...] = ()
    outputs: tuple[Any, ...] = ()
    citations: tuple[Any, ...] = ()
    provider_use: tuple[Any, ...] = ()
    exports: tuple[Any, ...] = ()
    action_ids: tuple[int, ...] = ()


class CallableInvocation:
    """Only host primitives see this receipt state; authored code never does."""

    RecordUpdate = CallableRecordUpdate

    def __init__(self, project, action, receipt, deps):
        self.project = project
        self.action = action
        self.receipt = receipt
        self.deps = deps
        self.context = HostInvocationContext(deps.cancelled)

    def _raise_if_failure(self, failure) -> None:
        if failure is not None:
            error = (
                failure.errors[0]
                if failure.errors
                else ActionError(
                    code="project_write_failed",
                    message="Callable reservation was lost.",
                    action_kind=self.action.kind,
                )
            )
            raise CallablePrimitiveError(error)

    def _persist_owning(self, receipt, *, before_commit=None):
        was_open = self.project.db.in_transaction
        try:
            failure = _finalize_reserved_action_receipt(
                self.project,
                self.action,
                params_hash=receipt.params_hash,
                project_id=receipt.project_id,
                receipt=receipt,
                result_from_existing_fn=_existing_result,
                before_commit=before_commit,
            )
        except BaseException:
            # Cancellation bypasses the shared helper's ordinary error channel.
            if not was_open and self.project.db.in_transaction:
                self.project.db.rollback()
            raise
        self._raise_if_failure(failure)
        self.receipt = receipt

    def _persist_in_transaction(self, receipt, *, before_commit=None):
        require_transaction(self.project.db, "callable invocation record")
        savepoint = f"callable_record_{uuid.uuid4().hex}"
        self.project.db.execute(f"SAVEPOINT {savepoint}")
        try:
            failure = _finalize_reserved_action_receipt_in_transaction(
                self.project,
                self.action,
                receipt=receipt,
                result_from_existing_fn=_existing_result,
                before_commit=before_commit,
            )
            self._raise_if_failure(failure)
        except BaseException:
            self.project.db.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            self.project.db.execute(f"RELEASE SAVEPOINT {savepoint}")
            raise
        self.project.db.execute(f"RELEASE SAVEPOINT {savepoint}")

    def _build_observed_receipt(self, update: CallableRecordUpdate) -> Receipt:
        current_outputs = list(self.receipt.outputs)
        current_exports = list(self.receipt.exports)
        for output in update.outputs:
            target = _artifact_target(output.ref)
            if target is not None:
                current_outputs = [
                    item
                    for item in current_outputs
                    if _artifact_target(item.ref) != target
                ]
            current_outputs.append(output)
        for artifact in update.exports:
            target = _artifact_target(artifact)
            if target is not None:
                current_exports = [
                    item for item in current_exports if _artifact_target(item) != target
                ]
            current_exports.append(artifact)
        return self.receipt.model_copy(
            update={
                "inputs": [*self.receipt.inputs, *update.inputs],
                "outputs": current_outputs,
                "evidence": [*self.receipt.evidence, *update.citations],
                "provider_use": [*self.receipt.provider_use, *update.provider_use],
                "exports": current_exports,
                "op_ids": list(dict.fromkeys((*self.receipt.op_ids, *update.action_ids))),
            },
        ).model_copy(deep=True)

    def record(
        self,
        *,
        inputs=(),
        outputs=(),
        evidence=(),
        provider_use=(),
        exports=(),
        op_ids=(),
        before_commit=None,
    ):
        observed = self._build_observed_receipt(
            CallableRecordUpdate(
                inputs=tuple(inputs),
                outputs=tuple(outputs),
                citations=tuple(evidence),
                provider_use=tuple(provider_use),
                exports=tuple(exports),
                action_ids=tuple(op_ids),
            )
        )
        self._persist_owning(observed, before_commit=before_commit)
        return observed

    def record_in_transaction(
        self,
        update: CallableRecordUpdate,
        *,
        before_commit=None,
    ):
        observed = self._build_observed_receipt(update)
        self._persist_in_transaction(observed, before_commit=before_commit)
        return observed

    def finish(self, *, status, value=None, error=None):
        terminal = self.receipt.model_copy(
            deep=True,
            update={
                "status": status,
                "value": value,
                "errors": [error] if error is not None else [],
            },
        )
        self._persist_owning(terminal)
        return _result_from_receipt(self.receipt)


def run_typed_callable_action(
    project: Any,
    project_id: str,
    bound: BoundTypedActionRequest,
    *,
    deps: ExecutorDeps | None = None,
    edition_run_context=None,
) -> ActionResult:
    terminal = bound.action.definition.run
    if not isinstance(terminal, _ProjectAction) or not terminal.callable_host:
        raise TypeError(
            "callable executor requires admitted plain-callable capabilities"
        )
    missing = set(terminal.capabilities) - _CALLABLE_CAPABILITIES.keys()
    if missing:
        raise TypeError("plain-callable capability has no host adapter")
    action = _typed_params_envelope(bound)
    plugin_secrets = None
    if PluginSecrets in terminal.injections:
        from frisket.authoring.workbench.native_plugin_secrets import HostPluginSecrets

        plugin_secrets = HostPluginSecrets.from_binding(project, bound.runtime_binding)

    def invoke(invocation):
        capabilities = {
            cap: _CALLABLE_CAPABILITIES[cap](invocation)
            for cap in terminal.capabilities
        }
        args = [
            invocation.context
            if kind is InvocationContext
            else capabilities[kind]
            if kind is not PluginSecrets
            else plugin_secrets
            for kind in terminal.injections
        ]
        return terminal.handler(bound.params, *args)

    def validate_return(returned):
        published = _publication_json(terminal, returned)
        # Existing instances store native field names, not validation aliases.
        # Capture publication first: authored validation must not change it.
        validated = terminal.return_validator.validate_python(
            returned, strict=True, by_name=True
        )
        if _publication_json(terminal, validated) != published:
            raise ValueError("callable revalidation changed its published value")
        return json.loads(published)

    return run_callable_invocation(
        project,
        project_id,
        action,
        params_hash=typed_request_hash(bound),
        invoke=invoke,
        validate_return=validate_return,
        deps=deps,
        edition_run_context=edition_run_context,
    )


def run_callable_invocation(
    project: Any,
    project_id: str,
    action: _TypedProjectEnvelope,
    *,
    params_hash: str,
    invoke: Callable[[CallableInvocation], Any],
    validate_return: Callable[[Any], Any],
    deps: ExecutorDeps | None = None,
    edition_run_context=None,
) -> ActionResult:
    """Run an admitted host invocation through the one callable receipt lifecycle.

    Both callbacks are host-owned: ``invoke`` receives the reserved primitive
    context; ``validate_return`` checks the declared return and yields its JSON
    value. Neither callback supplies receipt, admission, or replay policy.
    """
    reservation = _reserve_running_action_receipt(
        project,
        action,
        params_hash=params_hash,
        project_id=project_id,
        reservation_kind="plain_callable",
        result_from_existing_fn=_existing_result,
        edition_run_context=edition_run_context,
    )
    if isinstance(reservation, ActionResult):
        return reservation
    receipt = ReceiptStore(project).parsed_by_id(reservation["receipt_id"])
    assert receipt is not None
    invocation = CallableInvocation(project, action, receipt, deps or ExecutorDeps())
    error = None
    status = "completed"
    value = None
    try:
        invocation.context.check_cancelled()
        returned = invoke(invocation)
        invocation.context.check_cancelled()
        try:
            value = json.loads(json.dumps(validate_return(returned), allow_nan=False))
        except (TypeError, ValueError) as exc:
            raise CallablePrimitiveError(
                ActionError(
                    code="invalid_action_result",
                    message="The callable return does not match its declared finite JSON result.",
                    action_kind=action.kind,
                )
            ) from exc
    except SandboxTeardownError:
        raise
    except asyncio.CancelledError:
        status = "cancelled"
        error = ActionError(
            code="action_cancelled",
            message="Callable execution was cancelled.",
            action_kind=action.kind,
        )
    except CallablePrimitiveError as exc:
        status, error = (
            "failed",
            exc.error.model_copy(update={"action_kind": action.kind}),
        )
    except QueryPreviewError as exc:
        status = "failed"
        error = ActionError(
            code=exc.code, message=exc.message, action_kind=action.kind, field=exc.field
        )
    except Exception:
        status = "failed"
        error = ActionError(
            code="action_failed",
            message="The callable could not complete.",
            action_kind=action.kind,
        )
    try:
        result = invocation.finish(status=status, value=value, error=error)
    except CallablePrimitiveError as exc:
        return ActionCtx(project_id, action.kind).failed(exc.error)
    if invocation.receipt.exports:
        _emit_export_notification(project, result)
    return result
