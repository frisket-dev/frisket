"""Direct and queued action lifecycle execution bodies."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from pydantic import BaseModel

from frisket.contracts.action import (
    ActionError,
    ActionIdentity,
    ActionResult,
    ActionSpec,
    Receipt,
    ReceiptIO,
)
from frisket.engine.executor.action_specs import ResolvedAction
from frisket.ops import persisted_recipe_invocation_halt
from frisket.ops.base import Recipe
from frisket.engine.runner import (
    BatchRowLimitExceeded,
    CostGate,
    EmptyInputColumns,
    InvalidTargetRows,
    InvalidTargetSheet,
    ProviderKeyRefusal,
    NetworkDisabled,
    OutputColumnExists,
    RunProgress,
)
from frisket.engine.runner.confirmation_context import (
    action_confirmation,
    mint_confirmation_hash,
    quoted_usd,
    rate_estimate,
)
from frisket.engine.runner.confirmation_echo import refuse_unless_exact_echo
from frisket.engine.runner.validation import ExecutionResolutionRefused
from frisket.engine.runner.map_runner import ResultEvidenceWriteFailed
from frisket.execution.pricing_policy import default_pricing_policy
from frisket.engine.sandbox.shim import SandboxTeardownError
from frisket.engine.store.output_claims import (
    ClaimLeaseRenewalFailed,
    OutputColumnClaimStore,
)
from frisket.engine.store.result_generations import GenerationDeclarationConflict
from frisket.engine.store.receipts import (
    ReceiptStore,
)
from frisket.execution.attempt import (
    StaleAttemptWriter,
)
from frisket.execution.runtime_binding import (
    ExecutionRouteVerificationFailed,
    RouteBindingUnavailable,
)
from frisket.execution.attempt_authority import (
    DependentChoiceRefusal,
    UnroutedOnlyAuthority,
)
from frisket.execution.provider import (
    ExecutionCompositionContext,
    open_execution_composition,
)

from frisket.engine.executor.action_inventory import (
    _ActionExecutionEnvelope,
    ExecutorContext,
    _ActionCoreSpec,
    _MapRunnerFactory,
    _ReservedMaprunnerActionSpec,
    _apply_action_envelope_to_runner_spec,
)
from frisket.engine.executor.action_receipts import _result_from_receipt
from frisket.engine.executor.action_reservations import (
    _acquire_output_claims_for_runner,
    _delete_reserved_action_receipt,
    _finalize_reserved_action_receipt,
    _mark_running_action_run_prepared,
    _output_claim_token,
    _receipt_for_idempotency,
    _reserved_maprunner_delete_fn,
    _reserved_maprunner_reserve_fn,
    _reserved_maprunner_result_from_existing,
    _reserve_running_action_receipt,
    _runner_output_fields,
    _running_receipt_stale_result,
)
from frisket.engine.executor.action_support import (
    _failed_result,
    _model_cost_requires_confirmation_error,
    _new_id,
    _params_hash,
    action_confirmation_scope,
)


logger = logging.getLogger("frisket.executor")


def _terminalized_sandbox_teardown_progress(
    project: Any, exc: SandboxTeardownError
) -> RunProgress | None:
    """Recover runner identity only from verified terminal durable state."""

    run_id = getattr(exc, "run_id", None)
    if type(run_id) is not int:
        return None
    run = project.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    if run is None or str(run["status"] or "") != "cancelled":
        return None
    halt = persisted_recipe_invocation_halt(run["params"])
    if halt is None:
        return None
    code, detail = halt
    return RunProgress(
        run_id=run_id,
        total=int(run["total_rows"] or 0),
        completed=int(run["completed_rows"] or 0),
        failed=int(run["failed_rows"] or 0),
        cost=float(run["cost_actual"] or 0.0),
        done=True,
        cancelled=True,
        halted_code=code,
        halted_reason=detail,
    )


@dataclass(frozen=True)
class _PreparedMapExecution:
    """A registry-free program resolved directly against one project."""

    runner_spec: Mapping[str, Any]
    output_fields: tuple[Mapping[str, Any], ...]
    program: Recipe
    params_hash: str
    receipt_fn: Callable[[Any, int, str, str], Receipt]
    replay_error_fn: Callable[[Receipt], ActionError | None]
    reservation_kind: str


def _run_action_core_spec(
    project: Any,
    action: _ActionExecutionEnvelope,
    params: BaseModel,
    *,
    spec: _ActionCoreSpec,
    ctx: ExecutorContext,
    map_runner_factory: _MapRunnerFactory | None = None,
) -> ActionResult:
    """The one action lifecycle: idempotency replay -> confirmation gate -> body.

    The shared envelope runs the idempotency replay (`spec.result_from_existing_fn`)
    and the `needs_confirmation` gate, then dispatches on `spec.body_kind` to the
    reserved-maprunner / deterministic child-sheet / model child-sheet body.
    """
    params_hash_fn = spec.params_hash_fn
    params_hash = (
        params_hash_fn(action) if params_hash_fn is not None else _params_hash(action)
    )
    if spec.result_from_existing_fn is not None:
        existing = _receipt_for_idempotency(project, action.idempotency_key)
        if existing is not None:
            return spec.result_from_existing_fn(
                project,
                existing,
                params_hash=params_hash,
                project_id=ctx.project_id,
                action=action,
                params=params,
            )
    if spec.body_kind == "child_sheet_deterministic":
        return _run_child_sheet_deterministic_body(
            project,
            action,
            params,
            spec=spec,
            ctx=ctx,
            params_hash=params_hash,
        )
    if spec.body_kind == "child_sheet_deterministic_owned_write":
        return _run_child_sheet_deterministic_owned_write_body(
            project,
            action,
            params,
            spec=spec,
            ctx=ctx,
            params_hash=params_hash,
        )
    if spec.body_kind == "child_sheet_model":
        return _run_child_sheet_model_body(
            project,
            action,
            params,
            spec=spec,
            ctx=ctx,
            params_hash=params_hash,
        )
    if spec.body_kind == "plain":
        return _run_plain_action_body(
            project,
            action,
            params,
            spec=spec,
            ctx=ctx,
            params_hash=params_hash,
        )
    if map_runner_factory is None:
        raise RuntimeError("reserved-maprunner body requires a map_runner_factory")
    return _run_reserved_maprunner_action(
        project,
        action,
        params,
        project_id=ctx.project_id,
        router=ctx.deps.router,
        edition_run_context=ctx.edition_run_context,
        map_runner_factory=map_runner_factory,
        runner_spec_fn=spec.runner_spec_fn,
        resolve_fn=spec.resolve_fn,
        precheck_fn=spec.precheck_fn,
        reserve_fn=spec.reserve_fn,
        legacy_delete_reservation_fn=spec.delete_reservation_fn,
        write_fn=spec.write_fn,
        resume_run_id_fn=spec.resume_run_id_fn,
        program_fn=spec.program_fn,
        params_hash_fn=spec.params_hash_fn,
        confirmed_fn=spec.confirmed_fn,
        cost_gate_error_fn=spec.cost_gate_error_fn,
        map_error_code=spec.map_error_code,
    )


def _child_sheet_deterministic_result_from_existing(
    replay_validate_fn: Callable[[Any, Receipt], ActionError | None] | None = None,
) -> Callable[..., ActionResult]:
    """The deterministic child-sheet replay: params-hash conflict, optional staleness
    validation of the completed receipt, else replay it. The validation hook is set only
    by the op-owned-write preset (derive.join), whose materialization has an
    op-owned replay; the plain deterministic preset leaves it unset.
    Mirrors the inline `_replay_or_conflict` from the builder."""

    def result_from_existing(
        project: Any,
        existing: Any,
        *,
        params_hash: str,
        project_id: str,
        action: ActionSpec,
        params: Any | None = None,
    ) -> ActionResult:
        if existing["params_hash"] != params_hash:
            return _failed_result(
                project_id=project_id,
                action_kind=action.kind,
                error=ActionError(
                    code="idempotency_conflict",
                    message=(
                        "idempotency_key was already used with different normalized params"
                    ),
                    action_kind=action.kind,
                    field="idempotency_key",
                ),
            )
        receipt = Receipt.model_validate(json.loads(existing["body"]))
        if replay_validate_fn is not None:
            replay_error = replay_validate_fn(project, receipt)
            if replay_error is not None:
                return _failed_result(
                    project_id=project_id,
                    action_kind=action.kind,
                    error=replay_error,
                )
        return _result_from_receipt(receipt)

    return result_from_existing


def _child_sheet_model_result_from_existing(
    spec: _ActionCoreSpec,
) -> Callable[..., ActionResult]:
    """The model child-sheet replay: params-hash conflict, stale-running clearance,
    in-progress error, optional materialized-ref replay validation, else replay.
    Mirrors the inline `_replay` from the reserved-model builder."""

    def result_from_existing(
        project: Any,
        existing: Any,
        *,
        params_hash: str,
        project_id: str,
        action: ActionSpec,
        params: Any | None = None,
    ) -> ActionResult:
        if existing["params_hash"] != params_hash:
            return _failed_result(
                project_id=project_id,
                action_kind=action.kind,
                error=ActionError(
                    code="idempotency_conflict",
                    message="idempotency_key was already used with different normalized params",
                    action_kind=action.kind,
                    field="idempotency_key",
                ),
            )
        stale = _running_receipt_stale_result(
            project,
            existing,
            params_hash=params_hash,
            project_id=project_id,
            action=action,
        )
        if stale is not None:
            return stale
        if existing["status"] == "running":
            return _failed_result(
                project_id=project_id,
                action_kind=action.kind,
                error=ActionError(
                    code="idempotency_in_progress",
                    message=(
                        f"idempotency_key is already reserved by a running {action.kind} action"
                    ),
                    action_kind=action.kind,
                    field="idempotency_key",
                    details={"receipt_id": existing["id"]},
                ),
            )
        receipt = Receipt.model_validate(json.loads(existing["body"]))
        if spec.child_sheet_replay_validate_fn is not None:
            replay_error = spec.child_sheet_replay_validate_fn(project, receipt)
            if replay_error is not None:
                return _failed_result(
                    project_id=project_id,
                    action_kind=action.kind,
                    error=replay_error,
                )
        return _result_from_receipt(receipt)

    return result_from_existing


def _child_sheet_model_reserve_fn(
    spec: _ActionCoreSpec,
) -> Callable[..., dict[str, str] | ActionResult]:
    """Reserve the model child-sheet running receipt: BEGIN IMMEDIATE, re-check
    idempotency inside the txn (replaying on a winner), else insert the running
    receipt. Mirrors the inline `_reserve` from the reserved-model builder."""
    result_from_existing_fn = _child_sheet_model_result_from_existing(spec)

    def reserve(
        project: Any,
        action: ActionSpec,
        *,
        params_hash: str,
        project_id: str,
    ) -> dict[str, str] | ActionResult:
        action_id = _new_id("act")
        receipt_id = _new_id("receipt")
        receipt = Receipt(
            receipt_id=receipt_id,
            project_id=project_id,
            action_id=action_id,
            action_kind=action.kind,
            idempotency_key=action.idempotency_key,
            params_hash=params_hash,
            status="running",
            inputs=[
                ReceiptIO(
                    name="idempotency",
                    ref={
                        "kind": spec.reservation_kind,
                        "params_hash": params_hash,
                    },
                )
            ],
        )
        owns_transaction = not project.db.in_transaction
        try:
            project.db.execute("BEGIN IMMEDIATE")
            existing = _receipt_for_idempotency(project, action.idempotency_key)
            if existing is not None:
                project.db.rollback()
                return result_from_existing_fn(
                    project,
                    existing,
                    params_hash=params_hash,
                    project_id=project_id,
                    action=action,
                )
            ReceiptStore(project).insert_running(receipt, commit=False)
            project.db.commit()
        except BaseException as exc:
            try:
                if owns_transaction:
                    project.db.rollback()
            finally:
                if not isinstance(exc, Exception):
                    raise exc
            logger.debug(
                "action_failed",
                exc_info=True,
                extra={"event": "action_failed", "action_kind": action.kind},
            )
            return _failed_result(
                project_id=project_id,
                action_kind=action.kind,
                error=ActionError(
                    code="project_write_failed",
                    message="project write failed",
                    action_kind=action.kind,
                ),
            )
        return {"action_id": action_id, "receipt_id": receipt_id}

    return reserve


def _child_sheet_model_delete_fn(spec: _ActionCoreSpec) -> Callable[[Any, str], None]:
    """Literal move of the reserved-model builder's `_delete_reservation`: delete the
    running reservation and debug-log a missed delete via `status_by_id`. NOT the
    reserved-maprunner `_delete_reserved_action_receipt` (logging/behaviour differs)."""

    def delete(project: Any, receipt_id: str) -> None:
        owns_transaction = not project.db.in_transaction
        try:
            project.db.execute("BEGIN IMMEDIATE")
            deleted = ReceiptStore(project).delete_running(receipt_id, commit=False)
            if not deleted:
                status = ReceiptStore(project).status_by_id(receipt_id)
                logger.debug(
                    "reservation_delete_missed",
                    extra={
                        "event": "reservation_delete_missed",
                        "action_kind": spec.kind,
                        "receipt_id": receipt_id,
                        "status": status,
                    },
                )
            project.db.commit()
        except BaseException as exc:
            try:
                if owns_transaction:
                    project.db.rollback()
            finally:
                if not isinstance(exc, Exception):
                    raise exc
            logger.debug(
                "action_failed",
                exc_info=True,
                extra={"event": "action_failed", "action_kind": spec.kind},
            )

    return delete


def _run_child_sheet_deterministic_body(
    project: Any,
    action: ActionSpec,
    params: BaseModel,
    *,
    spec: _ActionCoreSpec,
    ctx: ExecutorContext,
    params_hash: str,
) -> ActionResult:
    """The deterministic child-sheet body (derive.table_from_list): resolve outside the
    txn, duplicate precheck, then BEGIN IMMEDIATE -> idempotency + duplicate re-check ->
    write_single_parent_child_sheet -> present -> receipt -> commit. A mechanical move of
    the inline flow from `build_child_sheet_run_fn`."""
    from frisket.engine.store.materialization import (
        SingleParentChildSheetPlan,
        write_single_parent_child_sheet,
    )

    project_id = ctx.project_id

    def _replay_or_conflict(existing: Any) -> ActionResult:
        return spec.result_from_existing_fn(
            project,
            existing,
            params_hash=params_hash,
            project_id=project_id,
            action=action,
            params=params,
        )

    resolved = spec.child_sheet_resolve_fn(project, params)
    if isinstance(resolved, ActionError):
        return _failed_result(
            project_id=project_id,
            action_kind=action.kind,
            error=resolved,
        )
    target_name = spec.child_sheet_target_name_fn(params)
    duplicate = spec.child_sheet_duplicate_error_fn(project, target_name)
    if duplicate is not None:
        return _failed_result(
            project_id=project_id,
            action_kind=action.kind,
            error=duplicate,
        )

    cur = project.db.cursor()
    owns_transaction = not project.db.in_transaction
    try:
        cur.execute("BEGIN IMMEDIATE")
        existing = _receipt_for_idempotency(project, action.idempotency_key)
        if existing is not None:
            project.db.rollback()
            return _replay_or_conflict(existing)
        duplicate = spec.child_sheet_duplicate_error_fn(project, target_name)
        if duplicate is not None:
            project.db.rollback()
            return _failed_result(
                project_id=project_id,
                action_kind=action.kind,
                error=duplicate,
            )
        action_id = _new_id("act")
        receipt_id = _new_id("receipt")
        write = write_single_parent_child_sheet(
            cur,
            SingleParentChildSheetPlan(
                action_kind=spec.kind,
                label=f"{spec.kind} {target_name}",
                target_sheet_name=target_name,
                parent_sheet_id=int(resolved["parent_sheet_id"]),
                op_spec=spec.child_sheet_op_spec_fn(
                    action, params, params_hash, resolved
                ),
                columns=resolved["columns"],
                rows=resolved["rows"],
            ),
        )
        if spec.child_sheet_row_evidence_fn is not None:
            spec.child_sheet_row_evidence_fn(project, resolved, write)
        prov = spec.child_sheet_present_fn(resolved, write)
        receipt = Receipt(
            receipt_id=receipt_id,
            project_id=project_id,
            action_id=action_id,
            action_kind=action.kind,
            op_ids=[write.op_id],
            idempotency_key=action.idempotency_key,
            params_hash=params_hash,
            status=prov.status,
            inputs=prov.inputs,
            outputs=spec.child_sheet_receipt_outputs_fn(
                target_name, int(resolved["parent_sheet_id"]), write
            ),
            evidence=prov.evidence,
        )
        ReceiptStore(project).insert_completed(receipt, commit=False)
        project.db.commit()
        return ActionResult(
            action=ActionIdentity(kind=action.kind, action_id=action_id),
            status="completed",
            project_id=project_id,
            op_ids=[write.op_id],
            outputs=spec.child_sheet_action_outputs_fn(target_name, write),
            receipt_id=receipt_id,
        )
    except BaseException as exc:
        try:
            if owns_transaction:
                project.db.rollback()
        finally:
            if not isinstance(exc, Exception):
                raise exc
        logger.debug(
            "action_failed",
            exc_info=True,
            extra={"event": "action_failed", "action_kind": action.kind},
        )
        return _failed_result(
            project_id=project_id,
            action_kind=action.kind,
            error=ActionError(
                code="project_write_failed",
                message="project write failed",
                action_kind=action.kind,
            ),
        )


def _run_in_txn_idempotent_perform(
    project: Any,
    action: _ActionExecutionEnvelope,
    *,
    project_id: str,
    params_hash: str,
    perform_fn: Callable[..., ActionResult],
    pre_perform_fn: Callable[[Any], ActionResult | None] | None = None,
    result_from_existing_fn: Callable[..., ActionResult] | None = None,
    exception_error_fn: Callable[[_ActionExecutionEnvelope], ActionError] | None = None,
    cleanup_resolved_fn: Callable[[Any], None] | None = None,
    post_commit_fn: Callable[[Any], None] | None = None,
    persist_failure: bool = False,
    resolved: Any = None,
) -> ActionResult:
    """The shared in-txn skeleton for the deterministic op-owned bodies (plain +
    owned-write child-sheet): BEGIN IMMEDIATE -> re-check idempotency in-txn (params-hash
    conflict -> idempotency_conflict, else replay the completed receipt) ->
    optional `pre_perform_fn` guard (the owned-write body's in-txn duplicate re-check) ->
    the op-owned `perform_fn` (which does any project writes, inserts the receipt, and
    returns the result) -> commit on a completed result, else rollback. On any exception,
    rollback and surface the error: `exception_error_fn(action)` when set, else the fixed
    project_write_failed ActionError. Non-Exception interruptions roll back and clean up
    identically, then propagate unchanged rather than becoming an action failure.

    When supplied, `result_from_existing_fn` reuses the outer replay policy at
    the in-txn race check, including artifact validation. Otherwise replay is
    plain, preserving the deterministic-aggregate policy. The helper owns
    the txn boundary and allocates the action/receipt ids;
    `perform_fn`/`pre_perform_fn` must not commit or rollback themselves.

    `cleanup_resolved_fn` (with `resolved`) is the optional cleanup-on-rollback seam for
    ops that stage out-of-txn side effects in `perform_fn` (filesystem artifacts moved into
    place): it runs after rollback on every path where the perform's writes do NOT commit
    (the in-txn idempotency-race short-circuit, the exception path, the non-completed-result
    rollback), and never on the committed-success path. `perform_fn` records what it moved
    on the mutable `resolved` so the cleanup undoes exactly the staged side effects.
    Cleanup also owns already-resolved staging when transaction acquisition fails;
    database rollback never touches a caller's pre-existing transaction.

    `post_commit_fn` (with `resolved`) is the optional post-commit success-finalize seam: it
    runs ONLY on the committed-success path, immediately after the commit succeeds, and never
    on any non-commit path. It is the mirror of `cleanup_resolved_fn` for the side effect that
    a committed receipt should finalize rather than undo (the local exports' .bak removal).

    `persist_failure` (default False) widens the commit predicate for background/scheduled ops
    (source.poll): when True, a `failed` result also commits (so the perform's failed receipt +
    failure trace become a durable record) instead of rolling back. `post_commit_fn` still fires
    only on the `completed` path, and `cleanup_resolved_fn` still fires only on the rollback
    paths; the failed-but-committed path runs neither (no op pairs persist_failure with those
    out-of-txn-side-effect seams). With persist_failure False the predicate is unchanged, so
    every existing plain/owned-write op stays byte-equal."""
    cur = project.db.cursor()
    owns_transaction = not project.db.in_transaction
    try:
        cur.execute("BEGIN IMMEDIATE")
        existing = _receipt_for_idempotency(project, action.idempotency_key)
        if existing is not None:
            replay = (
                result_from_existing_fn(
                    project,
                    existing,
                    params_hash=params_hash,
                    project_id=project_id,
                    action=action,
                )
                if result_from_existing_fn is not None
                else None
            )
            project.db.rollback()
            if cleanup_resolved_fn is not None:
                cleanup_resolved_fn(resolved)
            if replay is not None:
                return replay
            if existing["params_hash"] != params_hash:
                return _failed_result(
                    project_id=project_id,
                    action_kind=action.kind,
                    error=ActionError(
                        code="idempotency_conflict",
                        message=(
                            "idempotency_key was already used with different "
                            "normalized params"
                        ),
                        action_kind=action.kind,
                        field="idempotency_key",
                    ),
                )
            return _result_from_receipt(
                Receipt.model_validate(json.loads(existing["body"]))
            )
        if pre_perform_fn is not None:
            early = pre_perform_fn(cur)
            if early is not None:
                project.db.rollback()
                return early
        action_id = _new_id("act")
        receipt_id = _new_id("receipt")
        result = perform_fn(cur, action_id=action_id, receipt_id=receipt_id)
        committed = result.status == "completed" or (
            persist_failure and result.status == "failed"
        )
        if committed:
            project.db.commit()
        else:
            project.db.rollback()
            if cleanup_resolved_fn is not None:
                cleanup_resolved_fn(resolved)
    except BaseException as exc:
        try:
            if owns_transaction:
                project.db.rollback()
            if cleanup_resolved_fn is not None:
                cleanup_resolved_fn(resolved)
        finally:
            if not isinstance(exc, Exception):
                raise exc
        logger.debug(
            "action_failed",
            exc_info=True,
            extra={"event": "action_failed", "action_kind": action.kind},
        )
        error = (
            exception_error_fn(action)
            if exception_error_fn is not None
            else ActionError(
                code="project_write_failed",
                message="project write failed",
                action_kind=action.kind,
            )
        )
        return _failed_result(
            project_id=project_id,
            action_kind=action.kind,
            error=error,
        )

    if committed and result.status == "completed" and post_commit_fn is not None:
        try:
            post_commit_fn(resolved)
        except Exception:
            # Commit already owns the outcome. Cleanup failure cannot undo its
            # artifacts or make the persisted receipt disagree with the result.
            logger.warning(
                "action_post_commit_cleanup_failed",
                exc_info=True,
                extra={
                    "event": "action_post_commit_cleanup_failed",
                    "action_kind": action.kind,
                },
            )
    return result


def _run_child_sheet_deterministic_owned_write_body(
    project: Any,
    action: ActionSpec,
    params: BaseModel,
    *,
    spec: _ActionCoreSpec,
    ctx: ExecutorContext,
    params_hash: str,
) -> ActionResult:
    """The deterministic op-owned-write child-sheet body (derive.join): a read-only
    resolve outside the txn, duplicate precheck, then the shared in-txn idempotent perform
    (BEGIN IMMEDIATE -> idempotency re-check -> in-txn duplicate re-check -> the op-owned
    in-txn write -> commit). The op owns the write (it materializes a multi-parent
    aggregate sheet, updates the op spec with the materialized refs, and inserts the
    receipt); the core allocates the action/receipt ids and owns the txn boundary."""
    project_id = ctx.project_id

    resolved = spec.child_sheet_resolve_fn(project, params)
    if isinstance(resolved, ActionError):
        return _failed_result(
            project_id=project_id,
            action_kind=action.kind,
            error=resolved,
        )
    target_name = spec.child_sheet_target_name_fn(params)
    duplicate = spec.child_sheet_duplicate_error_fn(project, target_name)
    if duplicate is not None:
        return _failed_result(
            project_id=project_id,
            action_kind=action.kind,
            error=duplicate,
        )

    def _pre_perform(cur: Any) -> ActionResult | None:
        duplicate = spec.child_sheet_duplicate_error_fn(project, target_name)
        if duplicate is None:
            return None
        return _failed_result(
            project_id=project_id,
            action_kind=action.kind,
            error=duplicate,
        )

    def _perform(cur: Any, *, action_id: str, receipt_id: str) -> ActionResult:
        return spec.child_sheet_deterministic_write_in_txn_fn(
            project,
            cur,
            action,
            params,
            project_id=project_id,
            action_id=action_id,
            receipt_id=receipt_id,
            params_hash=params_hash,
            target_name=target_name,
            resolved=resolved,
        )

    return _run_in_txn_idempotent_perform(
        project,
        action,
        project_id=project_id,
        params_hash=params_hash,
        perform_fn=_perform,
        pre_perform_fn=_pre_perform,
    )


def _run_plain_action_body(
    project: Any,
    action: _ActionExecutionEnvelope,
    params: BaseModel,
    *,
    spec: _ActionCoreSpec,
    ctx: ExecutorContext,
    params_hash: str,
) -> ActionResult:
    """The plain action body (cluster.values): an optional best-effort pre-txn resolve
    (early validation; None for receipt-only ops that resolve in-txn), then the shared
    in-txn idempotent perform. The op-owned `plain_perform_in_txn_fn` does the in-txn
    reads/validation, any project writes, the receipt insert, and returns the result; on a
    non-completed result the shared helper rolls back so no receipt is persisted. No
    target-name or duplicate concepts. The core allocates the action/receipt ids and owns
    the txn boundary; the outer `result_from_existing_fn` owns idempotency replay +
    staleness. When the perform stages out-of-txn side effects (filesystem artifacts), the
    optional `plain_cleanup_resolved_fn` undoes them on every non-commit path (in-txn
    idempotency-race skip, exception/rollback, non-completed rollback), and the optional
    `plain_post_commit_fn` finalizes them on the committed-success path (the local exports'
    .bak removal after the receipt commits)."""
    project_id = ctx.project_id

    resolved: Any = None
    if spec.plain_resolve_fn is not None:
        resolve_kwargs: dict[str, Any] = {}
        if spec.plain_resolve_needs_router:
            resolve_kwargs["router"] = ctx.deps.router
        if spec.plain_resolve_needs_edition_context:
            resolve_kwargs["edition_run_context"] = ctx.edition_run_context
        resolved = spec.plain_resolve_fn(project, params, **resolve_kwargs)
        if isinstance(resolved, ActionError):
            return _failed_result(
                project_id=project_id,
                action_kind=action.kind,
                error=resolved,
            )

    def _perform(cur: Any, *, action_id: str, receipt_id: str) -> ActionResult:
        return spec.plain_perform_in_txn_fn(
            project,
            cur,
            action,
            params,
            project_id=project_id,
            action_id=action_id,
            receipt_id=receipt_id,
            params_hash=params_hash,
            resolved=resolved,
        )

    return _run_in_txn_idempotent_perform(
        project,
        action,
        project_id=project_id,
        params_hash=params_hash,
        perform_fn=_perform,
        exception_error_fn=spec.plain_exception_error_fn,
        cleanup_resolved_fn=spec.plain_cleanup_resolved_fn,
        post_commit_fn=spec.plain_post_commit_fn,
        persist_failure=spec.plain_persist_failure,
        result_from_existing_fn=(
            (
                lambda *args, **kwargs: spec.result_from_existing_fn(
                    *args, params=params, **kwargs
                )
            )
            if spec.result_from_existing_fn is not None
            else None
        ),
        resolved=resolved,
    )


def _run_child_sheet_model_body(
    project: Any,
    action: ActionSpec,
    params: BaseModel,
    *,
    spec: _ActionCoreSpec,
    ctx: ExecutorContext,
    params_hash: str,
) -> ActionResult:
    """The model/reservation/cost-gated child-sheet body (reduce.group_summary,
    join.semantic): resolve -> duplicate precheck -> cost-gate -> reserve -> async
    compute -> op-owned write -> reservation cleanup. A mechanical move of the inline
    flow from `build_reserved_model_child_sheet_run_fn`; the cost-gate stays here as a
    `status="failed"` `model_cost_requires_confirmation` (no needs_confirmation route)."""
    from frisket.engine.jobs.runs import project_scoped_router

    project_id = ctx.project_id

    # Model-spending path: same credential authority as every other one
    # (see `project_scoped_router`), never a bare env-only router.
    router_obj = project_scoped_router(project, ctx.deps.router)
    resolved = spec.child_sheet_model_resolve_fn(project, params, router_obj)
    if isinstance(resolved, ActionError):
        return _failed_result(
            project_id=project_id,
            action_kind=action.kind,
            error=resolved,
        )
    target_name = spec.child_sheet_target_name_fn(params)
    duplicate = spec.child_sheet_duplicate_error_fn(project, target_name)
    if duplicate is not None:
        return _failed_result(
            project_id=project_id,
            action_kind=action.kind,
            error=duplicate,
        )

    estimate = rate_estimate(
        spec.child_sheet_estimate_cost_fn(params, resolved),
        policy=default_pricing_policy(),
    )
    # The BILLED figure gates, not the provider's: a child-sheet build whose
    # provider cost sits under the threshold but which this deployment bills
    # above it must still ask. Identical under the identity policy.
    quoted = quoted_usd(estimate)
    from frisket.execution.consent_coverage import effective_consent_coverage

    coverage = effective_consent_coverage(project, ctx.deps.consent_coverage)
    requires_confirmation = quoted is None or quoted > float(coverage.threshold_usd)
    context_hash = mint_confirmation_hash(
        action_confirmation(
            family_kind=action.kind,
            scope=action_confirmation_scope(action),
            estimate=estimate,
        )
    )

    def _cost_gate_refusal() -> ActionResult:
        gate = CostGate(
            quoted, estimate_details=estimate, gate_usd=float(coverage.threshold_usd)
        )
        gate.promise_set_hash = context_hash
        # (B) the pre-flight cost gate pauses for confirmation; it is not a failure.
        # Matches the reserved pre-flight needs_confirmation gate (research.web_search/answer).
        return ActionResult(
            action=ActionIdentity(kind=action.kind, action_id=_new_id("act")),
            status="needs_confirmation",
            project_id=project_id,
            errors=[_model_cost_requires_confirmation_error(action, gate)],
        )

    if requires_confirmation:
        refusal = refuse_unless_exact_echo(
            confirmed=params.confirmed,
            echoed_hash=getattr(params, "consented_promise_set_hash", None),
            expected_hash=context_hash,
            refuse=_cost_gate_refusal,
        )
        if refusal is not None:
            return refusal

    reservation = spec.reserve_fn(
        project,
        action,
        params_hash=params_hash,
        project_id=project_id,
    )
    if isinstance(reservation, ActionResult):
        return reservation

    try:
        computations = asyncio.run(
            spec.child_sheet_compute_fn(
                project,
                params,
                resolved,
                router_obj,
            )
        )
    except StaleAttemptWriter as exc:
        return _failed_result(
            project_id=project_id,
            action_kind=action.kind,
            error=ActionError(
                code=exc.code,
                message=str(exc),
                action_kind=action.kind,
            ),
        )
    except Exception as exc:
        spec.delete_reservation_fn(project, reservation["receipt_id"])
        return _failed_result(
            project_id=project_id,
            action_kind=action.kind,
            error=ActionError(
                code="model_run_failed",
                message=str(exc),
                action_kind=action.kind,
            ),
        )
    if isinstance(computations, ActionError):
        spec.delete_reservation_fn(project, reservation["receipt_id"])
        return _failed_result(
            project_id=project_id,
            action_kind=action.kind,
            error=computations,
        )

    try:
        result = spec.child_sheet_write_result_fn(
            project,
            action,
            params,
            params_hash=params_hash,
            project_id=project_id,
            action_id=reservation["action_id"],
            receipt_id=reservation["receipt_id"],
            resolved=resolved,
            estimate=estimate,
            computations=computations,
        )
    except StaleAttemptWriter as exc:
        return _failed_result(
            project_id=project_id,
            action_kind=action.kind,
            error=ActionError(
                code=exc.code,
                message=str(exc),
                action_kind=action.kind,
            ),
        )
    except Exception as exc:
        spec.delete_reservation_fn(project, reservation["receipt_id"])
        return _failed_result(
            project_id=project_id,
            action_kind=action.kind,
            error=ActionError(
                code="project_write_failed",
                message=str(exc),
                action_kind=action.kind,
            ),
        )
    if result.status == "failed" and result.receipt_id is None:
        spec.delete_reservation_fn(project, reservation["receipt_id"])
    return result


def _run_reserved_maprunner_action_spec(
    project: Any,
    action: ActionSpec,
    params: BaseModel,
    *,
    spec: _ReservedMaprunnerActionSpec,
    ctx: ExecutorContext,
    map_runner_factory: _MapRunnerFactory,
) -> ActionResult:
    """The reserved-maprunner preset: fill the core seams, then run the core."""
    write_fn = spec.write_fn
    if ctx.deps.reserved_maprunner_write_overrides is not None:
        write_fn = ctx.deps.reserved_maprunner_write_overrides.get(
            action.kind, write_fn
        )

    cost_gate_error_fn = None
    if spec.cost_gate_error_fn is not None:
        cost_gate_error_spec_fn = spec.cost_gate_error_fn

        def cost_gate_error_fn(exc: CostGate) -> ActionError:
            return cost_gate_error_spec_fn(action, exc)

    core_spec = _ActionCoreSpec(
        kind=spec.kind,
        params_model=spec.params_model,
        params_hash_fn=spec.params_hash_fn,
        needs_confirmation_error_fn=spec.needs_confirmation_error_fn,
        confirmed_fn=spec.confirmed_fn,
        result_from_existing_fn=_reserved_maprunner_result_from_existing(spec),
        reserve_fn=_reserved_maprunner_reserve_fn(spec),
        delete_reservation_fn=_reserved_maprunner_delete_fn(spec),
        runner_spec_fn=spec.runner_spec_fn,
        resolve_fn=spec.resolve_fn,
        precheck_fn=spec.precheck_fn,
        reservation_kind=spec.reservation_kind,
        write_fn=write_fn,
        resume_run_id_fn=spec.resume_run_id_fn,
        program_fn=spec.program_fn,
        cost_gate_error_fn=cost_gate_error_fn,
        map_error_code=spec.map_error_code,
        log_missed_delete=spec.log_missed_delete,
    )
    return _run_action_core_spec(
        project,
        action,
        params,
        spec=core_spec,
        ctx=ctx,
        map_runner_factory=map_runner_factory,
    )


def preview_map_runner_factory(project: Any, router: Any | None) -> Any:
    # Preview runs a ≤20-row sample; the default
    # bounded concurrency keeps the wall time ≈ slowest row without the
    # serialize-everything concurrency=1 the persisted run path uses.
    from frisket.engine.jobs.runs import project_scoped_router
    from frisket.engine.runner import MapRunner

    # Same credential authority as the persisted run (see
    # `project_scoped_router`): a preview that priced and spent against a
    # different key than the run it is previewing is two surfaces, two
    # answers -- on money.
    effective_router = project_scoped_router(project, router)
    return MapRunner(
        project,
        effective_router,
        authority=UnroutedOnlyAuthority(project),
        execution_composition=open_execution_composition(
            project,
            effective_router,
            ExecutionCompositionContext.direct(),
        ),
    )


def resolve_maprunner_runner_spec(
    project: Any,
    action: ActionSpec,
    params: BaseModel,
    *,
    runner_spec_fn: Callable[[Any], dict[str, Any]],
    resolve_fn: Callable[[Any, Any, dict[str, Any]], dict[str, Any] | ActionError],
    precheck_fn: Callable[[Any, Any, dict[str, Any]], ActionError | None],
    program_fn: Callable[[Any, dict[str, Any]], Recipe | ActionError | None]
    | None = None,
) -> (
    tuple[
        dict[str, Any],
        dict[str, Any],
        list[dict[str, Any]] | None,
        Recipe | None,
    ]
    | ActionError
):
    """The resolve+precheck prefix of ``_run_reserved_maprunner_action``
    Build the runner spec, run the per-action
    ``resolve_fn`` then ``precheck_fn``, and return the spec, resolution, and
    (for routed actions) the one precomputed output descriptor list, or the
    first ``ActionError``. Shared by the run path and the in-memory preview
    service, so preview reuses ALL per-action resolve logic and swaps only the
    persistence tail."""
    runner_spec = _apply_action_envelope_to_runner_spec(action, runner_spec_fn(params))
    confirmed = getattr(params, "confirmed", None)
    if confirmed is True:
        # Retry-flow plumbing, not action identity: preview_precheck receives
        # only the runner spec (unlike the durable path, which also gets the
        # boolean argument), so dropping this half of the exact confirmation
        # pair made every valid preview retry re-402 forever. The action's
        # params_hash_fn excludes both confirmation fields.
        runner_spec["confirmed"] = True
    consented_hash = getattr(params, "consented_promise_set_hash", None)
    if consented_hash:
        runner_spec["consented_promise_set_hash"] = consented_hash
    resolved = resolve_fn(project, params, runner_spec)
    if isinstance(resolved, ActionError):
        return resolved
    program = program_fn(project, runner_spec) if program_fn is not None else None
    if isinstance(program, ActionError):
        return program
    from frisket.engine.runner.validation import recipe_for_spec

    recipe = program if program is not None else recipe_for_spec(runner_spec)

    precomputed_output_fields = (
        [dict(field) for field in recipe.output_fields(runner_spec)]
        if recipe.consumes_resolution
        else None
    )
    if precomputed_output_fields is None:
        output_precheck = precheck_fn(project, params, runner_spec)
    else:
        output_precheck = precheck_fn(
            project,
            params,
            runner_spec,
            output_fields=precomputed_output_fields,
        )
    if output_precheck is not None:
        return output_precheck
    return runner_spec, resolved, precomputed_output_fields, program


def _terminalize_unclaimed_prepared_run(
    project: Any,
    prepared_run: Any | None,
    *,
    claim_token: str | None = None,
) -> None:
    """Close a prepared run when its runner failed before taking dispatch.

    Canonical direct actions publish the run/output family before invoking
    ``MapRunner.run``.  A runner-construction/test-double error can therefore
    escape without ever reaching MapRunner's own pre-dispatch finalizer.  The
    receipt/claim cleanup alone must not strand that run as ``running`` with
    an ``admitted`` attempt forever.

    A live ``dispatching`` attempt is categorically not ours to close here:
    returned-accounting/reconciliation owns it.  With no dispatching attempt,
    terminalize the one created/admitted attempt (if present) together with
    the failed run and zero-success output visibility.
    """

    if prepared_run is None:
        return
    run_id = int(prepared_run.run_id)
    from frisket.engine.runner.finalization import _finalize_run_in_transaction
    from frisket.engine.store.result_generations import ResultGenerationStore
    from frisket.engine.store.runs import RunResultStore

    try:
        # The state check and terminal write share one write lock. Checking
        # first and opening the finalizer transaction afterwards would let a
        # worker claim this attempt in between and have its live dispatch
        # revoked as "pre-dispatch."
        project.db.execute("BEGIN IMMEDIATE")
        run = project.db.execute(
            "SELECT status FROM runs WHERE id=?",
            (run_id,),
        ).fetchone()
        if run is None or str(run["status"]) != "running":
            project.db.rollback()
            return
        open_attempts = project.db.execute(
            "SELECT id, state FROM execution_attempts "
            "WHERE run_id=? AND state IN ('created','admitted','dispatching') "
            "ORDER BY seq",
            (run_id,),
        ).fetchall()
        if any(str(row["state"]) == "dispatching" for row in open_attempts):
            project.db.rollback()
            return
        if len(open_attempts) > 1:
            # Ambiguous ownership is not evidence that either attempt is safe
            # to revoke. Leave the durable state for explicit recovery.
            project.db.rollback()
            return
        terminal_attempt_id = (
            str(open_attempts[0]["id"]) if len(open_attempts) == 1 else None
        )
        unbound_managed_outputs = not ResultGenerationStore(project).bindings_for_run(
            run_id
        )
        _finalize_run_in_transaction(
            project,
            RunResultStore(project),
            op_id=int(prepared_run.op_id),
            run_id=run_id,
            status="failed",
            out_cols=dict(prepared_run.out_cols),
            generation_claim_token=claim_token,
            carry_forward_atomic_family=bool(prepared_run.recipe.atomic_output_columns),
            keep_zero_success_output_columns=bool(
                prepared_run.recipe.keep_zero_success_output_columns
            ),
            terminal_attempt_id=terminal_attempt_id,
            terminal_attempt_state=(
                "halted" if terminal_attempt_id is not None else None
            ),
            never_point_column_ids=(
                frozenset(
                    int(column_id) for column_id in prepared_run.out_cols.values()
                )
                if unbound_managed_outputs
                else frozenset()
            ),
        )
        project.db.commit()
    except BaseException:
        project.db.rollback()
        raise
    project.refresh_pending_review_summary()


def _has_live_managed_writer(project: Any, prepared_run: Any | None) -> bool:
    """Return whether cleanup must preserve a managed reconciliation tuple."""

    if prepared_run is None:
        return False
    run_id = int(prepared_run.run_id)
    open_generation = project.db.execute(
        "SELECT 1 FROM run_output_generations "
        "WHERE run_id=? AND state IN ('active','staged') LIMIT 1",
        (run_id,),
    ).fetchone()
    if open_generation is None:
        return False
    return (
        project.db.execute(
            "SELECT 1 FROM execution_attempts "
            "WHERE run_id=? AND state='dispatching' LIMIT 1",
            (run_id,),
        ).fetchone()
        is not None
    )


def _run_reserved_maprunner_action(
    project: Any,
    action: _ActionExecutionEnvelope,
    params: BaseModel,
    *,
    project_id: str,
    router: Any | None,
    map_runner_factory: _MapRunnerFactory,
    edition_run_context: Mapping[str, Any] | None = None,
    resolved_execution: _PreparedMapExecution | None = None,
    runner_spec_fn: Callable[[Any], dict[str, Any]] | None = None,
    resolve_fn: (
        Callable[[Any, Any, dict[str, Any]], dict[str, Any] | ActionError] | None
    ) = None,
    precheck_fn: Callable[..., ActionError | None] | None = None,
    reserve_fn: Callable[..., dict[str, str] | ActionResult] | None = None,
    legacy_delete_reservation_fn: Callable[[Any, str], None] | None = None,
    write_fn: Callable[..., ActionResult] | None = None,
    resume_run_id_fn: Callable[[Any, Any, dict[str, Any]], int | None] | None = None,
    program_fn: Callable[[Any, dict[str, Any]], Recipe | ActionError | None]
    | None = None,
    params_hash_fn: Callable[[_ActionExecutionEnvelope], str] | None = None,
    confirmed_fn: Callable[[Any], bool] | None = None,
    cost_gate_error_fn: Callable[[CostGate], ActionError] | None = None,
    map_error_code: str = "map_run_failed",
    output_claim_error_field: str = "params.output_name",
) -> ActionResult:
    prepared_execution = resolved_execution
    params_hash = (
        prepared_execution.params_hash
        if prepared_execution is not None
        else params_hash_fn(action)
        if params_hash_fn is not None
        else _params_hash(action)
    )

    def delete_reservation_fn(current_project: Any, receipt_id: str) -> None:
        if prepared_execution is not None:
            _delete_reserved_action_receipt(
                current_project, receipt_id, action_kind=action.kind
            )
            return
        assert legacy_delete_reservation_fn is not None
        legacy_delete_reservation_fn(current_project, receipt_id)

    program = None
    if prepared_execution is None:
        assert runner_spec_fn is not None
        assert resolve_fn is not None
        assert precheck_fn is not None
        resolution = resolve_maprunner_runner_spec(
            project,
            action,
            params,
            runner_spec_fn=runner_spec_fn,
            resolve_fn=resolve_fn,
            precheck_fn=precheck_fn,
            program_fn=program_fn,
        )
        if isinstance(resolution, ActionError):
            return _failed_result(
                project_id=project_id,
                action_kind=action.kind,
                error=resolution,
            )
        runner_spec, resolved, precomputed_output_fields, program = resolution
        resume_run_id = (
            resume_run_id_fn(project, params, resolved)
            if resume_run_id_fn is not None
            else None
        )
    else:
        runner_spec = dict(prepared_execution.runner_spec)
        precomputed_output_fields = [
            dict(field) for field in prepared_execution.output_fields
        ]
        program = prepared_execution.program
        resume_run_id = None
    # This is a host decision derived from the admitted implementation, never
    # an author Params flag (nor a caller-supplied runner-spec override).
    runner_spec.pop("deferred_publication", None)
    if getattr(program, "defer_generation_seal", False) is True:
        runner_spec["deferred_publication"] = True
    defer_generation_seal = runner_spec.get("deferred_publication") is True
    from frisket.engine.runner import validation as runner_validation

    recipe = (
        program
        if program is not None
        else runner_validation.recipe_for_spec(runner_spec)
    )
    output_fields = (
        precomputed_output_fields
        if precomputed_output_fields is not None
        else [dict(field) for field in program.output_fields(runner_spec)]
        if program is not None
        else _runner_output_fields(runner_spec)
    )
    atomic_publication = resume_run_id is None and recipe.consumes_resolution
    owns_atomic_transaction = atomic_publication and not project.db.in_transaction
    try:
        if owns_atomic_transaction:
            from frisket.engine.store.execution_routes import instance_principal

            instance_principal(project)
        if atomic_publication:
            project.db.execute("BEGIN IMMEDIATE")
        if prepared_execution is not None:
            reservation = _reserve_running_action_receipt(
                project,
                action,
                params_hash=params_hash,
                project_id=project_id,
                reservation_kind=prepared_execution.reservation_kind,
                replay_error_fn=prepared_execution.replay_error_fn,
                edition_run_context=edition_run_context,
                commit=not atomic_publication,
            )
        else:
            assert reserve_fn is not None
            reservation = reserve_fn(
                project,
                action,
                params_hash=params_hash,
                project_id=project_id,
                commit=not atomic_publication,
                **(
                    {"edition_run_context": edition_run_context}
                    if edition_run_context is not None
                    else {}
                ),
            )
        if isinstance(reservation, ActionResult):
            if atomic_publication:
                project.db.rollback()
            return reservation

        claim_token = _output_claim_token(reservation["receipt_id"])
        claim_error = _acquire_output_claims_for_runner(
            project,
            action,
            runner_spec,
            receipt_id=reservation["receipt_id"],
            output_fields=output_fields,
            error_field=output_claim_error_field,
            commit=not atomic_publication,
        )
    except BaseException:
        if owns_atomic_transaction:
            project.db.rollback()
        raise
    if claim_error is not None:
        if atomic_publication:
            project.db.rollback()
        else:
            delete_reservation_fn(project, reservation["receipt_id"])
        return _failed_result(
            project_id=project_id,
            action_kind=action.kind,
            error=claim_error,
        )

    try:
        if prepared_execution is not None:
            post_claim_precheck = None
        elif precomputed_output_fields is None:
            assert precheck_fn is not None
            post_claim_precheck = precheck_fn(project, params, runner_spec)
        else:
            assert precheck_fn is not None
            post_claim_precheck = precheck_fn(
                project,
                params,
                runner_spec,
                output_fields=output_fields,
            )
    except BaseException:
        if atomic_publication:
            project.db.rollback()
        else:
            OutputColumnClaimStore(project).release(
                claim_token=claim_token, status="failed"
            )
            delete_reservation_fn(project, reservation["receipt_id"])
        raise
    if post_claim_precheck is not None:
        if atomic_publication:
            project.db.rollback()
        else:
            OutputColumnClaimStore(project).release(
                claim_token=claim_token, status="failed"
            )
            delete_reservation_fn(project, reservation["receipt_id"])
        return _failed_result(
            project_id=project_id,
            action_kind=action.kind,
            error=post_claim_precheck,
        )

    try:
        deferred_terminal_close = bool(
            action.kind in {"join.semantic", "map.extract"}
            or runner_spec.get("deferred_publication") is True
        )
        confirmed = (
            confirmed_fn(params)
            if confirmed_fn is not None
            else bool(getattr(params, "confirmed", False))
        )
    except BaseException:
        if atomic_publication:
            project.db.rollback()
        raise
    prepared_run = None
    admitted_attempt = None

    def fail_unclaimed(error: ActionError) -> ActionResult:
        _terminalize_unclaimed_prepared_run(
            project,
            prepared_run,
            claim_token=claim_token,
        )
        OutputColumnClaimStore(project).release(
            claim_token=claim_token, status="failed"
        )
        delete_reservation_fn(project, reservation["receipt_id"])
        return _failed_result(
            project_id=project_id,
            action_kind=action.kind,
            error=error,
        )

    try:
        try:
            runner = map_runner_factory(project, router)
            if atomic_publication:
                prepared_run, admitted_attempt = runner.prepare_admitted_run(
                    runner_spec,
                    program=program,
                    confirmed=confirmed,
                    output_fields=output_fields,
                )
                _mark_running_action_run_prepared(
                    project,
                    receipt_id=reservation["receipt_id"],
                    run_id=prepared_run.run_id,
                    attempt_id=admitted_attempt.attempt_id,
                    output_fields=output_fields,
                    program=program,
                    defer_publication=defer_generation_seal,
                    commit=False,
                )
                project.db.commit()
            else:
                prepared_run = runner._prepare(
                    runner_spec,
                    program=program,
                    confirmed=confirmed,
                    resume_run_id=resume_run_id,
                    precomputed_output_fields=output_fields,
                )
                _mark_running_action_run_prepared(
                    project,
                    receipt_id=reservation["receipt_id"],
                    run_id=prepared_run.run_id,
                    attempt_id=None,
                    output_fields=output_fields,
                    program=program,
                    defer_publication=defer_generation_seal,
                    commit=True,
                )
        except BaseException:
            if atomic_publication and project.db.in_transaction:
                project.db.rollback()
            raise
        # No confirmed_fn is NOT consent: the
        # fallback reads the params' own confirmed field, defaulting False —
        # a blanket True here disarmed the MapRunner cost gate for every
        # direct-dispatch op that never wired confirmation (the audit's
        # concrete exposure: enrich.census_demographics). Free ops are
        # unaffected — a $0 estimate never gates.
        progress = asyncio.run(
            runner.run(
                runner_spec,
                program=program,
                confirmed=confirmed,
                claim_token=claim_token,
                prepared_run=prepared_run,
                admitted_attempt=admitted_attempt,
                defer_attempt_close=deferred_terminal_close,
                defer_generation_seal=defer_generation_seal,
                **({} if resume_run_id is None else {"resume_run_id": resume_run_id}),
            )
        )
    except CostGate as exc:
        OutputColumnClaimStore(project).discard_for_receipt(
            receipt_id=reservation["receipt_id"]
        )
        delete_reservation_fn(project, reservation["receipt_id"])
        error = (
            cost_gate_error_fn(exc)
            if cost_gate_error_fn is not None
            else ActionError(
                code=map_error_code,
                message=str(exc),
                action_kind=action.kind,
            )
        )
        return _failed_result(
            project_id=project_id,
            action_kind=action.kind,
            error=error,
        )
    except ProviderKeyRefusal as exc:
        # Named, typed error at run-confirm time — see the matching catch in
        # frisket.server.action_enqueue for the queued-project-run twin of
        # this reserved-map-runner path. Catches the BASE so a new provider-key
        # refusal needs no edit here.
        OutputColumnClaimStore(project).release(
            claim_token=claim_token, status="failed"
        )
        delete_reservation_fn(project, reservation["receipt_id"])
        return _failed_result(
            project_id=project_id,
            action_kind=action.kind,
            error=ActionError(
                code=exc.error_code,
                message=exc.action_message(),
                action_kind=action.kind,
                field=exc.field,
                details=dict(exc.details),
            ),
        )
    except NetworkDisabled as exc:
        # Egress gate: the direct-dispatch twin of the catch in
        # frisket.server.action_enqueue. status="failed", never the
        # needs_confirmation envelope — off is off.
        #
        # Same managed-writer guard as the ValueError/RuntimeError and
        # generic handlers below: none of the first-slice managed producers
        # can raise this mid-run today (no remote capability on the
        # allowlist), but releasing the claim with a staged generation would
        # make the run permanently unterminalizable, so fail loudly instead
        # if that ever becomes reachable.
        if _has_live_managed_writer(project, prepared_run):
            raise
        return fail_unclaimed(
            ActionError(
                code="network_disabled",
                message=str(exc),
                action_kind=action.kind,
                details={"capability": exc.capability},
            ),
        )
    except ExecutionResolutionRefused as exc:
        OutputColumnClaimStore(project).discard_for_receipt(
            receipt_id=reservation["receipt_id"]
        )
        delete_reservation_fn(project, reservation["receipt_id"])
        return _failed_result(
            project_id=project_id,
            action_kind=action.kind,
            error=ActionError(
                code=exc.family,
                message=str(exc),
                action_kind=action.kind,
            ),
        )
    except OutputColumnExists as exc:
        OutputColumnClaimStore(project).release(
            claim_token=claim_token, status="failed"
        )
        delete_reservation_fn(project, reservation["receipt_id"])
        return _failed_result(
            project_id=project_id,
            action_kind=action.kind,
            error=ActionError(
                code="output_column_exists",
                message=str(exc),
                action_kind=action.kind,
                field=output_claim_error_field,
                details={"columns": exc.columns},
            ),
        )
    except GenerationDeclarationConflict as exc:
        return fail_unclaimed(
            ActionError(
                code="output_column_exists",
                message=str(exc),
                action_kind=action.kind,
                field=output_claim_error_field,
            ),
        )
    except InvalidTargetSheet as exc:
        OutputColumnClaimStore(project).release(
            claim_token=claim_token, status="failed"
        )
        delete_reservation_fn(project, reservation["receipt_id"])
        return _failed_result(
            project_id=project_id,
            action_kind=action.kind,
            error=ActionError(
                code="invalid_input_ref",
                message=str(exc),
                action_kind=action.kind,
                field=(
                    "row_scope.sheet_id"
                    if action.row_scope is not None
                    else "params.sheet_id"
                ),
                details={"sheet_id": exc.sheet_id},
            ),
        )
    except InvalidTargetRows as exc:
        OutputColumnClaimStore(project).release(
            claim_token=claim_token, status="failed"
        )
        delete_reservation_fn(project, reservation["receipt_id"])
        return _failed_result(
            project_id=project_id,
            action_kind=action.kind,
            error=ActionError(
                code="invalid_input_ref",
                message=str(exc),
                action_kind=action.kind,
                field=(
                    "row_scope.selector.membership.row_ids"
                    if action.row_scope is not None
                    else "params.row_ids"
                ),
                details={"missing": exc.missing},
            ),
        )
    except EmptyInputColumns as exc:
        # Refuse at request time — before any `runs` row exists — instead of queuing
        # N rows that would each fail identically. See the matching catch in
        # frisket.server.action_enqueue for the queued-project-run twin.
        OutputColumnClaimStore(project).release(
            claim_token=claim_token, status="failed"
        )
        delete_reservation_fn(project, reservation["receipt_id"])
        return _failed_result(
            project_id=project_id,
            action_kind=action.kind,
            error=ActionError(
                code="empty_input_column",
                message=str(exc),
                action_kind=action.kind,
                field=recipe.empty_input_error_field,
                details={"columns": exc.columns},
            ),
        )
    except BatchRowLimitExceeded as exc:
        OutputColumnClaimStore(project).release(
            claim_token=claim_token, status="failed"
        )
        delete_reservation_fn(project, reservation["receipt_id"])
        return _failed_result(
            project_id=project_id,
            action_kind=action.kind,
            error=ActionError(
                code="batch_row_limit_exceeded",
                message=str(exc),
                action_kind=action.kind,
                details={"row_count": exc.row_count, "max_rows": exc.max_rows},
            ),
        )
    except SandboxTeardownError as exc:
        terminal_progress = _terminalized_sandbox_teardown_progress(project, exc)
        if terminal_progress is None:
            OutputColumnClaimStore(project).release(
                claim_token=claim_token, status="failed"
            )
            delete_reservation_fn(project, reservation["receipt_id"])
            raise
        # MapRunner deliberately re-raises this infrastructure failure after
        # terminalizing the run. The action boundary now has enough durable
        # identity to write the normal typed failed/resumable receipt instead
        # of deleting its reservation as a generic RuntimeError.
        logger.error(
            "reserved_maprunner_sandbox_teardown_terminalized",
            exc_info=True,
            extra={
                "event": "reserved_maprunner_sandbox_teardown_terminalized",
                "action_kind": action.kind,
                "run_id": terminal_progress.run_id,
                "receipt_id": reservation["receipt_id"],
            },
        )
        progress = terminal_progress
    except RouteBindingUnavailable as exc:
        # F6: a persisted route whose pinned target no longer derefs is a
        # DURABLE dispatch refusal (§0.3: an unavailable pin halts, never
        # falls back) — the direct-dispatch twin of the queued handler's
        # ``no_live_target`` mapping (engine/jobs/runs.py). Caught BEFORE
        # the generic (ValueError, RuntimeError) below so it never degrades
        # to the generic map_error_code. The run row itself needs no work
        # here: the MapRunner's pre-dispatch close-out already put a resume
        # back where admission found it, and TERMINALIZED a fresh launch
        # (failed + finished_at + the typed code in its markers) rather than
        # leaving it at 'running' with nothing to revert to.
        OutputColumnClaimStore(project).release(
            claim_token=claim_token, status="failed"
        )
        delete_reservation_fn(project, reservation["receipt_id"])
        return _failed_result(
            project_id=project_id,
            action_kind=action.kind,
            error=ActionError(
                code="no_live_target",
                message=exc.remedy,
                action_kind=action.kind,
            ),
        )
    except ExecutionRouteVerificationFailed as exc:
        # The direct-dispatch twin of the queued handler's typed
        # verification branch (engine/jobs/runs.py). Worker-side route
        # verification refusing on a DIRECT dispatch (run.backfill is the
        # live one) used to fall into the generic (ValueError, RuntimeError)
        # arm below and surface as ``map_run_failed`` — an untyped failure
        # with no remedy, over a run the MapRunner had already reverted to
        # its pre-resume state. The receipt now carries the typed code
        # (``consent_missing`` / ``no_live_target`` / ``stale_head``) and the
        # remedy, so the refusal is reconsent-reachable instead of opaque.
        # The run row itself needs no work here: the MapRunner's pre-dispatch
        # close-out reverted a resume to where admission found it, or
        # terminalized a fresh launch as failed with the same typed code.
        OutputColumnClaimStore(project).release(
            claim_token=claim_token, status="failed"
        )
        delete_reservation_fn(project, reservation["receipt_id"])
        return _failed_result(
            project_id=project_id,
            action_kind=action.kind,
            error=ActionError(
                code=exc.code,
                message=str(exc),
                action_kind=action.kind,
            ),
        )
    except DependentChoiceRefusal as exc:
        # §1.4's dependent-choice refusal: a routed run reached a surface
        # whose attempt authority can never authorize one. Typed symmetrically
        # with the branch above (never a generic map_error_code) — it is an
        # internal composition defect, and the receipt must say so rather than
        # reading as a transcription failure. It never advertises
        # ``consent_missing``, which would send the operator to reconsent — a
        # remedy that cannot work, since no confirmation repairs wiring.
        OutputColumnClaimStore(project).release(
            claim_token=claim_token, status="failed"
        )
        delete_reservation_fn(project, reservation["receipt_id"])
        return _failed_result(
            project_id=project_id,
            action_kind=action.kind,
            error=ActionError(
                code=exc.code,
                message=(
                    f"{exc.remedy} This is an internal composition defect in "
                    "Frisket, not a consent gap: re-running or re-confirming "
                    "will refuse identically until the dispatch path is fixed."
                ),
                action_kind=action.kind,
            ),
        )
    except StaleAttemptWriter as exc:
        return _failed_result(
            project_id=project_id,
            action_kind=action.kind,
            error=ActionError(
                code=exc.code,
                message=str(exc),
                action_kind=action.kind,
            ),
        )
    except ClaimLeaseRenewalFailed:
        # A failed renewal rolled its effect batch back. Do not release the
        # still-authoritative claim or terminalize/delete its run receipt;
        # the caller/queue may retry the invocation.
        raise
    except ResultEvidenceWriteFailed as exc:
        from frisket.engine.executor.project_run_terminalization import (
            CurrentWriterTerminalAuthority,
            terminalize_project_run,
        )

        if exc.writer_attempt_id is None:
            # Do not guess ownership for an unbound/plugin invocation.
            raise
        error = ActionError(
            code="project_write_failed",
            message="project write failed",
            action_kind=action.kind,
        )
        settled = terminalize_project_run(
            project,
            run_id=exc.run_id,
            receipt_id=reservation["receipt_id"],
            status="failed",
            authority=CurrentWriterTerminalAuthority(
                writer_attempt_id=exc.writer_attempt_id,
                claim_token=claim_token,
            ),
            errors=[error],
        )
        if settled.disposition not in {"terminalized", "already_terminal"}:
            # In particular, a cancelled sibling's reserved provider checkpoint
            # is ambiguous. Preserve the writer tuple for reconciliation.
            raise
        stored = ReceiptStore(project).find_by_id(reservation["receipt_id"])
        assert stored is not None
        return _result_from_receipt(stored.parsed())
    except (ValueError, RuntimeError) as exc:
        if _has_live_managed_writer(project, prepared_run):
            raise
        return fail_unclaimed(
            ActionError(
                code=map_error_code,
                message=str(exc),
                action_kind=action.kind,
            ),
        )
    except Exception as exc:
        if _has_live_managed_writer(project, prepared_run):
            raise
        return fail_unclaimed(
            ActionError(
                code=map_error_code,
                message=str(exc),
                action_kind=action.kind,
            ),
        )

    try:
        write_kwargs: dict[str, Any] = {}
        if deferred_terminal_close:
            write_kwargs.update(
                {
                    "writer_attempt_id": progress.writer_attempt_id,
                    "claim_token": claim_token,
                }
            )
        if prepared_execution is not None and write_fn is None:
            receipt = prepared_execution.receipt_fn(
                project,
                progress.run_id,
                reservation["action_id"],
                reservation["receipt_id"],
            )
            finalization = _finalize_reserved_action_receipt(
                project,
                action,
                params_hash=params_hash,
                project_id=project_id,
                receipt=receipt,
                reservation_lost_message=f"{action.kind} reservation was lost",
                update_failed_message=f"{action.kind} receipt update failed",
                replay_error_fn=prepared_execution.replay_error_fn,
            )
            result = finalization or _result_from_receipt(receipt)
        else:
            assert write_fn is not None
            result = write_fn(
                project,
                action,
                params,
                runner_spec=runner_spec,
                params_hash=params_hash,
                project_id=project_id,
                action_id=reservation["action_id"],
                receipt_id=reservation["receipt_id"],
                run_id=progress.run_id,
                resolved=ResolvedAction.from_resolve_dict(
                    {} if prepared_execution is not None else resolved
                ),
                **write_kwargs,
            )
    except StaleAttemptWriter as exc:
        return _failed_result(
            project_id=project_id,
            action_kind=action.kind,
            error=ActionError(
                code=exc.code,
                message=str(exc),
                action_kind=action.kind,
            ),
        )
    except Exception as exc:
        if deferred_terminal_close and progress.writer_attempt_id is not None:
            # The materialization/checkpoint/receipt transaction rolled back.
            # Preserve its live writer tuple; deleting the reservation or
            # releasing the claim here would authorize a paid replay without
            # first reconciling the returned effect.
            raise
        return fail_unclaimed(
            ActionError(
                code="project_write_failed",
                message=str(exc),
                action_kind=action.kind,
            ),
        )
    if deferred_terminal_close and progress.writer_attempt_id is not None:
        if result.receipt_id == reservation["receipt_id"]:
            receipt_state = project.db.execute(
                "SELECT status FROM receipts WHERE id=?", (result.receipt_id,)
            ).fetchone()
            if receipt_state is not None and receipt_state["status"] == "running":
                # The fenced finalizer refused to close an unresolved effect.
                # Retain its writer authority for reconciliation; a failed
                # response alone is not evidence that publication terminalized.
                return result
        attempt_row = project.db.execute(
            "SELECT state FROM execution_attempts WHERE id=?",
            (progress.writer_attempt_id,),
        ).fetchone()
        if attempt_row is not None and attempt_row["state"] == "dispatching":
            try:
                OutputColumnClaimStore(project).finish_current_writer(
                    run_id=progress.run_id,
                    writer_attempt_id=progress.writer_attempt_id,
                    claim_token=claim_token,
                    attempt_state="effected",
                    claim_status=(
                        "failed" if result.status == "failed" else "released"
                    ),
                )
            except StaleAttemptWriter as exc:
                return _failed_result(
                    project_id=project_id,
                    action_kind=action.kind,
                    error=ActionError(
                        code=exc.code,
                        message=str(exc),
                        action_kind=action.kind,
                    ),
                )
    if result.status == "failed" and result.receipt_id is None:
        OutputColumnClaimStore(project).release(
            claim_token=claim_token, status="failed"
        )
        delete_reservation_fn(project, reservation["receipt_id"])
    else:
        OutputColumnClaimStore(project).release(
            claim_token=claim_token,
            status="released" if result.status != "failed" else "failed",
        )
    return result
