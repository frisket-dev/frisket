"""Direct and queued action lifecycle execution bodies."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from functools import partial
from uuid import uuid4
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from pydantic import BaseModel

from frisket.contracts.action import (
    ActionError,
    ActionResult,
    ActionSpec,
    Receipt,
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
from frisket.actions.types import RowError
from frisket.engine.runner.validation import ExecutionResolutionRefused
from frisket.engine.runner.map_runner import ResultEvidenceWriteFailed
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
from frisket.engine.executor.action_receipts import (
    _result_from_receipt,
    receipt_status_from_failures,
)
from frisket.engine.executor.action_reservations import (
    _acquire_output_claims_for_runner,
    _delete_reserved_action_receipt,
    _finalize_reserved_action_receipt,
    _mark_running_action_run_prepared,
    _reserved_receipt_result_from_existing,
    _output_claim_token,
    _receipt_for_idempotency,
    _reserved_maprunner_delete_fn,
    _reserved_maprunner_reserve_fn,
    _reserved_maprunner_result_from_existing,
    _reserve_running_action_receipt,
    _runner_output_fields,
)
from frisket.engine.executor.action_support import (
    ActionCtx,
    RefusalSite,
    _new_id,
    _params_hash,
    refusal_error,
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
    reservation: Mapping[str, str] | None = None
    queued_reservation_owner: bool = False


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
    plain or reserved-maprunner body.
    """
    params_hash_fn = spec.params_hash_fn
    params_hash = (
        params_hash_fn(action) if params_hash_fn is not None else _params_hash(action)
    )
    if spec.result_from_existing_fn is not None:
        existing = _receipt_for_idempotency(project, action.idempotency_key)
        if existing is not None:
            existing_result = spec.result_from_existing_fn(
                project,
                existing,
                params_hash=params_hash,
                project_id=ctx.project_id,
                action=action,
                params=params,
            )
            if existing_result is not None:
                return existing_result
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
    """The deterministic replay: params-hash conflict, optional staleness
    validation of the completed receipt, else replay it."""

    def result_from_existing(
        project: Any,
        existing: Any,
        *,
        params_hash: str,
        project_id: str,
        action: ActionSpec,
        params: Any | None = None,
    ) -> ActionResult:
        return _reserved_receipt_result_from_existing(
            project,
            existing,
            params_hash=params_hash,
            project_id=project_id,
            action=action,
            replay_error_fn=None
            if replay_validate_fn is None
            else partial(replay_validate_fn, project),
            guard_running=False,
        )

    return result_from_existing


def _run_in_txn_idempotent_perform(
    project: Any,
    action: _ActionExecutionEnvelope,
    *,
    project_id: str,
    params_hash: str,
    perform_fn: Callable[..., ActionResult],
    result_from_existing_fn: Callable[..., ActionResult] | None = None,
    exception_error_fn: Callable[[_ActionExecutionEnvelope], ActionError] | None = None,
    cleanup_resolved_fn: Callable[[Any], None] | None = None,
    post_commit_fn: Callable[[Any], None] | None = None,
    persist_failure: bool = False,
    resolved: Any = None,
) -> ActionResult:
    """The shared in-txn skeleton for the deterministic op-owned bodies: BEGIN IMMEDIATE
    -> re-check idempotency in-txn (params-hash conflict -> idempotency_conflict, else
    replay the completed receipt) -> the op-owned `perform_fn` (which does any project writes, inserts the receipt, and
    returns the result) -> commit on a completed result, else rollback. On any exception,
    rollback and surface the error: `exception_error_fn(action)` when set, else the fixed
    project_write_failed ActionError. Non-Exception interruptions roll back and clean up
    identically, then propagate unchanged rather than becoming an action failure.

    When supplied, `result_from_existing_fn` reuses the outer replay policy at
    the in-txn race check, including artifact validation. Otherwise replay is
    plain, preserving the deterministic-aggregate policy. The helper owns
    the txn boundary and allocates the action/receipt ids;
    `perform_fn` must not commit or rollback itself.

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
    out-of-txn-side-effect seams)."""
    ctx = ActionCtx(project_id, action.kind)
    cur = project.db.cursor()
    owns_transaction = not project.db.in_transaction
    if not owns_transaction and post_commit_fn is not None:
        raise RuntimeError("an action with post-commit work must own its transaction")
    savepoint = f"plain_action_write_{uuid4().hex}"
    perform_started = False

    def rollback_write():
        if owns_transaction:
            project.db.rollback()
        else:
            try:
                cur.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            except sqlite3.OperationalError as exc:
                if "no such savepoint" not in str(exc).lower() or perform_started:
                    if "no such savepoint" in str(exc).lower():
                        raise RuntimeError(
                            "nested action escaped its savepoint by committing the caller transaction"
                        ) from exc
                    raise
            else:
                cur.execute(f"RELEASE SAVEPOINT {savepoint}")

    def commit_write():
        if owns_transaction:
            project.db.commit()
        else:
            cur.execute(f"RELEASE SAVEPOINT {savepoint}")

    try:
        cur.execute("BEGIN IMMEDIATE" if owns_transaction else f"SAVEPOINT {savepoint}")
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
            rollback_write()
            if cleanup_resolved_fn is not None:
                cleanup_resolved_fn(resolved)
            if replay is not None:
                return replay
            return _reserved_receipt_result_from_existing(
                project,
                existing,
                params_hash=params_hash,
                project_id=project_id,
                action=action,
                guard_running=False,
            )
        action_id = _new_id("act")
        receipt_id = _new_id("receipt")
        perform_started = True
        result = perform_fn(cur, action_id=action_id, receipt_id=receipt_id)
        committed = result.status == "completed" or (
            persist_failure and result.status == "failed"
        )
        if committed:
            commit_write()
        else:
            rollback_write()
            if cleanup_resolved_fn is not None:
                cleanup_resolved_fn(resolved)
    except BaseException as exc:
        try:
            rollback_write()
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
        return ctx.failed(error)

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
            return ActionCtx(project_id, action.kind).failed(resolved)

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
    cost_gate_error_fn = None
    if spec.cost_gate_error_fn is not None:
        cost_gate_error_spec_fn = spec.cost_gate_error_fn

        def cost_gate_error_fn(exc: CostGate) -> ActionError:
            return cost_gate_error_spec_fn(action, exc)

    core_spec = _ActionCoreSpec(
        kind=spec.kind,
        params_model=spec.params_model,
        params_hash_fn=spec.params_hash_fn,
        confirmed_fn=spec.confirmed_fn,
        result_from_existing_fn=_reserved_maprunner_result_from_existing(spec),
        reserve_fn=_reserved_maprunner_reserve_fn(spec),
        delete_reservation_fn=_reserved_maprunner_delete_fn(spec),
        runner_spec_fn=spec.runner_spec_fn,
        resolve_fn=spec.resolve_fn,
        precheck_fn=spec.precheck_fn,
        write_fn=spec.write_fn,
        resume_run_id_fn=spec.resume_run_id_fn,
        program_fn=spec.program_fn,
        cost_gate_error_fn=cost_gate_error_fn,
        map_error_code=spec.map_error_code,
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
    receipt_id: str,
    error: ActionError,
) -> ActionResult | None:
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
        return None
    run_id = int(prepared_run.run_id)
    from frisket.engine.executor.project_run_terminalization import (
        NeverDispatchedRunTerminalAuthority,
        terminalize_project_run,
    )
    from frisket.engine.store.receipts import ReceiptStore

    receipts = ReceiptStore(project)
    stored_receipt = receipts.find_by_id(receipt_id)
    if stored_receipt is None:
        raise StaleAttemptWriter("prepared run lost its reservation receipt")
    try:
        parsed_receipt = stored_receipt.parsed()
    except Exception as exc:
        raise RuntimeError(
            f"prepared run receipt {receipt_id} is not readable"
        ) from exc
    if stored_receipt.run_id == run_id and parsed_receipt.run_id == run_id:
        terminalization = terminalize_project_run(
            project,
            run_id=run_id,
            receipt_id=receipt_id,
            status="failed",
            run_status="failed",
            authority=NeverDispatchedRunTerminalAuthority(),
            errors=[error],
        )
        if terminalization.disposition not in {
            "terminalized",
            "already_terminal",
        }:
            raise StaleAttemptWriter(
                "prepared run terminalization refused: "
                f"{terminalization.reason or terminalization.disposition}"
            )
        final_receipt = receipts.find_by_id(receipt_id)
        if final_receipt is None:
            raise RuntimeError(
                f"prepared run receipt {receipt_id} disappeared after close"
            )
        project.refresh_pending_review_summary()
        return _result_from_receipt(final_receipt.parsed())
    if stored_receipt.run_id is not None or parsed_receipt.run_id is not None:
        raise StaleAttemptWriter("prepared run reservation belongs to another run")

    # A failed preparation transaction may leave a run row while its receipt
    # and output claim remain unbound. That distinct shape has no declared
    # generation or run-bound claim to publish; it uses this narrow cleanup.
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
            "SELECT status,current_attempt_id FROM runs WHERE id=?",
            (run_id,),
        ).fetchone()
        if run is None or str(run["status"]) != "running":
            project.db.rollback()
            return None
        reservation_row = project.db.execute(
            "SELECT run_id FROM receipts WHERE id=?", (receipt_id,)
        ).fetchone()
        if reservation_row is None or reservation_row["run_id"] is not None:
            raise StaleAttemptWriter("unbound prepared cleanup lost receipt ownership")
        if ResultGenerationStore(project).bindings_for_run(run_id):
            raise StaleAttemptWriter(
                "unbound prepared cleanup cannot close declared result generations"
            )
        if project.db.execute(
            "SELECT 1 FROM output_column_claims WHERE status='active' "
            "AND run_id=? LIMIT 1",
            (run_id,),
        ).fetchone():
            raise StaleAttemptWriter(
                "unbound prepared cleanup cannot close run-bound output claims"
            )
        attempts = project.db.execute(
            "SELECT id, state FROM execution_attempts WHERE run_id=? ORDER BY seq",
            (run_id,),
        ).fetchall()
        if (
            run["current_attempt_id"] is not None
            or len(attempts) > 1
            or any(str(row["state"]) not in {"created", "admitted"} for row in attempts)
        ):
            # None permits the caller to remove its reservation. A refusal
            # must preserve that reservation as well as the run and attempts.
            raise StaleAttemptWriter("unbound preparation has dispatch history")
        if project.db.execute(
            "SELECT 1 FROM effect_checkpoints effect "
            "JOIN execution_attempts attempt ON attempt.id=effect.authorized_attempt_id "
            "WHERE attempt.run_id=? UNION ALL "
            "SELECT 1 FROM model_calls WHERE run_id=? LIMIT 1",
            (run_id, run_id),
        ).fetchone():
            raise StaleAttemptWriter("unbound preparation has provider effects")
        terminal_attempt_id = str(attempts[0]["id"]) if attempts else None
        _finalize_run_in_transaction(
            project,
            RunResultStore(project),
            op_id=int(prepared_run.op_id),
            run_id=run_id,
            status="failed",
            out_cols=dict(prepared_run.out_cols),
            # A failed prepared-output bind leaves the action reservation's
            # claim attached to the receipt, not to this run. It does not
            # grant this run terminal write authority; finish the exact
            # unclaimed prepared run, then let the caller release its receipt
            # reservation.
            generation_claim_token=None,
            keep_zero_success_output_columns=bool(
                prepared_run.recipe.keep_zero_success_output_columns
            ),
            terminal_attempt_id=terminal_attempt_id,
            terminal_attempt_state=(
                "halted" if terminal_attempt_id is not None else None
            ),
            never_point_column_ids=frozenset(
                int(column_id) for column_id in prepared_run.out_cols.values()
            ),
        )
        project.db.commit()
    except BaseException:
        project.db.rollback()
        raise
    project.refresh_pending_review_summary()
    return None


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
    ctx = ActionCtx(project_id, action.kind)
    prepared_execution = resolved_execution
    params_hash = (
        prepared_execution.params_hash
        if prepared_execution is not None
        else params_hash_fn(action)
        if params_hash_fn is not None
        else _params_hash(action)
    )
    reused_reservation_ids: set[str] = set()

    def delete_reservation_fn(current_project: Any, receipt_id: str) -> None:
        if receipt_id in reused_reservation_ids:
            return
        if prepared_execution is not None:
            if prepared_execution.queued_reservation_owner:
                # An action.run worker owns this pre-existing reservation and
                # terminalizes its returned refusal. Deleting it here would
                # leave the queued job with no durable receipt to close.
                return
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
            return ctx.failed(resolution)
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
        if (
            prepared_execution is not None
            and prepared_execution.reservation is not None
        ):
            reservation = dict(prepared_execution.reservation)
        elif prepared_execution is not None:
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
        if reservation.get("reservation_reused") == "true":
            reused_reservation_ids.add(reservation["receipt_id"])

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
        return ctx.failed(claim_error)

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
        return ctx.failed(post_claim_precheck)

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
    refusal_site = RefusalSite(
        action_kind=action.kind,
        has_row_scope=action.row_scope is not None,
        program=recipe,
        output_claim_field=output_claim_error_field,
    )

    def fail_unclaimed(error: ActionError) -> ActionResult:
        terminal_result = _terminalize_unclaimed_prepared_run(
            project,
            prepared_run,
            receipt_id=reservation["receipt_id"],
            error=error,
        )
        if terminal_result is not None:
            return terminal_result
        OutputColumnClaimStore(project).release(
            claim_token=claim_token, status="failed"
        )
        delete_reservation_fn(project, reservation["receipt_id"])
        return ctx.failed(error)

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
        return ctx.failed(error)
    except (
        ProviderKeyRefusal,
        ExecutionResolutionRefused,
        OutputColumnExists,
        InvalidTargetSheet,
        InvalidTargetRows,
        RowError,
        EmptyInputColumns,
        BatchRowLimitExceeded,
        RouteBindingUnavailable,
        ExecutionRouteVerificationFailed,
        DependentChoiceRefusal,
    ) as exc:
        # Request-time refusals: no row work was queued. A route refusal's run
        # row was already closed out by the MapRunner before dispatch.
        if isinstance(exc, ExecutionResolutionRefused):
            OutputColumnClaimStore(project).discard_for_receipt(
                receipt_id=reservation["receipt_id"]
            )
        else:
            OutputColumnClaimStore(project).release(
                claim_token=claim_token, status="failed"
            )
        delete_reservation_fn(project, reservation["receipt_id"])
        if (
            isinstance(exc, InvalidTargetRows)
            and prepared_execution is not None
            and getattr(
                getattr(program, "_terminal", None), "infer_output_types", False
            )
        ):
            return ctx.fail(
                "stale_input",
                "Staged row scope changed",
                field=None,
                details={"missing": exc.missing},
            )
        return ctx.failed(refusal_error(exc, refusal_site))
    except (NetworkDisabled, GenerationDeclarationConflict) as exc:
        # Releasing the claim with a staged generation would make the run
        # permanently unterminalizable, so a network refusal under a live
        # managed writer fails loudly instead.
        if isinstance(exc, NetworkDisabled) and _has_live_managed_writer(
            project, prepared_run
        ):
            raise
        return fail_unclaimed(refusal_error(exc, refusal_site))
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
        logger.exception(
            "reserved_maprunner_sandbox_teardown_terminalized",
            extra={
                "event": "reserved_maprunner_sandbox_teardown_terminalized",
                "action_kind": action.kind,
                "run_id": terminal_progress.run_id,
                "receipt_id": reservation["receipt_id"],
            },
        )
        progress = terminal_progress
    except StaleAttemptWriter as exc:
        return ctx.fail(exc.code, str(exc))
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
    except Exception as exc:
        if _has_live_managed_writer(project, prepared_run):
            raise
        return fail_unclaimed(ctx.error(map_error_code, str(exc)))

    try:
        write_kwargs: dict[str, Any] = {}
        if deferred_terminal_close:
            terminal_status = "cancelled" if progress.cancelled else "completed"
            receipt_status = terminal_status
            if terminal_status == "completed":
                receipt_status = receipt_status_from_failures(
                    progress.failed, progress.total
                )
            write_kwargs.update(
                {
                    "writer_attempt_id": progress.writer_attempt_id,
                    "claim_token": claim_token,
                    "terminal_status": terminal_status,
                    "receipt_status": receipt_status,
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
        return ctx.fail(exc.code, str(exc))
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
                return ctx.fail(exc.code, str(exc))
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
