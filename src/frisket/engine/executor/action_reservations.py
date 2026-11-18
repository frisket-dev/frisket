"""Action receipt reservation, output claims, queue publication, and finalization."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Literal, Mapping

from pydantic import BaseModel

from frisket.contracts.action import (
    ActionError,
    ActionResult,
    ActionSpec,
    Receipt,
    ReceiptEvidence,
    ReceiptIO,
)
from frisket.ai.llm.types import LLMError
from frisket.ops.base import Recipe
from frisket.engine.runner.result_generations import declare_bound_run_outputs
from frisket.engine.store.output_claims import (
    OutputColumnClaimStore,
)
from frisket.engine.store.receipts import (
    FINISHED_RECEIPT_STATUSES,
    ReceiptStore,
    StoredReceipt,
)
from frisket.execution.attempt import (
    STALE_DISPATCHING_AGE,
    StaleAttemptWriter,
    abandon_stale_dispatching_attempts,
)

from frisket.engine.executor.action_inventory import (
    _ActionExecutionEnvelope,
    _QueuedActionSpec,
    _ReservedMaprunnerActionSpec,
    _apply_action_envelope_to_runner_spec,
)
from frisket.engine.executor.action_receipts import _result_from_receipt
from frisket.engine.executor.action_support import (
    ActionCtx,
    _new_id,
    _params_hash_without_confirmed,
    _row_value,
)


logger = logging.getLogger("frisket.executor")


RUNNING_RECEIPT_STALE_AFTER_SECONDS = 60 * 60
QUEUED_ACTION_RUN_MARKER_PARAM = "_frisket_queued_action_run"
QUEUED_ACTION_RUN_MARKER_SCHEMA = "frisket.internal.queued_action_run.v1"
QUEUED_ACTION_RUN_PREPARED_RECORD_KIND = "queued_action_run_prepared"


def _output_claim_token(receipt_id: str) -> str:
    return f"output-claim:{receipt_id}"


def _runner_output_fields(
    runner_spec: Mapping[str, Any],
) -> list[dict[str, Any]]:
    from frisket.engine.runner.validation import recipe_for_spec

    recipe = recipe_for_spec(dict(runner_spec))
    fields = [dict(field) for field in recipe.output_fields(dict(runner_spec))]
    if any(
        not isinstance(field.get("name"), str) or not str(field["name"]).strip()
        for field in fields
    ):
        raise ValueError("runner output descriptors require non-empty names")
    return fields


def _generated_column_busy_error(
    action_kind: str, conflict: Any, *, field: str = "params.output_name"
) -> ActionError:
    return ActionError(
        code="output_column_busy",
        message="The target generated column is claimed by a running action.",
        action_kind=action_kind,
        field=field,
        details={
            "sheet_id": conflict["sheet_id"],
            "column_id": conflict["column_id"],
            "output_name": conflict["output_name"],
            "run_id": conflict["run_id"],
            "receipt_id": conflict["receipt_id"],
            "job_id": conflict["job_id"],
            "claim_id": conflict["id"],
            "action_kind": conflict["action_kind"],
            "mode": conflict["mode"],
            "lease_expires_at": conflict["lease_expires_at"],
            "requires_recovery": True,
        },
    )


def _acquire_output_claims_for_runner(
    project: Any,
    action: _ActionExecutionEnvelope,
    runner_spec: Mapping[str, Any],
    *,
    receipt_id: str,
    output_fields: list[dict[str, Any]],
    error_field: str = "params.output_name",
    commit: bool = True,
) -> ActionError | None:
    sheet_id = runner_spec.get("sheet_id")
    if type(sheet_id) is not int:
        return None
    output_names = [str(field["name"]) for field in output_fields]
    if not output_names:
        return None
    execution_root = runner_spec.get("execution_root_run_id")
    if execution_root is not None:
        from frisket.engine.store.execution_groups import execution_runs

        execution_root, _ = execution_runs(project, int(execution_root))
    _claims, conflict = OutputColumnClaimStore(project).acquire(
        sheet_id=sheet_id,
        output_names=output_names,
        action_kind=action.kind,
        receipt_id=receipt_id,
        claim_token=_output_claim_token(receipt_id),
        lease_seconds=int(STALE_DISPATCHING_AGE.total_seconds()),
        details={
            "idempotency_key": action.idempotency_key,
            **(
                {"execution_root_run_id": execution_root}
                if execution_root is not None
                else {}
            ),
            # Freeze the producer descriptors with the claim. Queue recovery
            # must not rebuild a compatibility key from whatever recipe code
            # happens to be installed later.
            "output_fields": [dict(field) for field in output_fields],
        },
        commit=commit,
    )
    if conflict is not None:
        return _generated_column_busy_error(action.kind, conflict, field=error_field)
    return None


@dataclass(frozen=True)
class _QueuedActionLifecycleAdapter:
    spec: _QueuedActionSpec

    def reserve(
        self,
        project: Any,
        action: _ActionExecutionEnvelope,
        params: BaseModel,
        *,
        project_id: str,
        recover_unobservable: bool = False,
        router: Any | None = None,
        program: Recipe | None = None,
        commit: bool = True,
    ) -> dict[str, Any] | ActionResult:
        return _reserve_queued_action(
            project,
            action,
            params,
            spec=self.spec,
            project_id=project_id,
            recover_unobservable=recover_unobservable,
            router=router,
            program=program,
            commit=commit,
        )

    def requires_atomic_publication(
        self, params: BaseModel, *, program: Recipe | None = None
    ) -> bool:
        from frisket.engine.runner.validation import recipe_for_spec

        recipe = program or recipe_for_spec(self.spec.runner_spec_fn(params))
        return bool(recipe.consumes_resolution)

    def mark_enqueued(
        self,
        project: Any,
        *,
        receipt_id: str,
        run_id: int,
        job_id: int,
    ) -> None:
        _mark_queued_action_enqueued(
            project,
            spec=self.spec,
            receipt_id=receipt_id,
            run_id=run_id,
            job_id=job_id,
        )

    def mark_runner_spec(
        self,
        runner_spec: Mapping[str, Any],
        *,
        receipt_id: str,
        action_id: str,
        params_hash: str,
    ) -> dict[str, Any]:
        return _queued_action_runner_spec_with_marker(
            runner_spec,
            action_kind=self.spec.kind,
            receipt_id=receipt_id,
            action_id=action_id,
            params_hash=params_hash,
        )

    def mark_prepared(
        self,
        project: Any,
        *,
        receipt_id: str,
        run_id: int,
        attempt_id: str | None,
        output_fields: list[dict[str, Any]],
        program: Recipe | None = None,
        reservation_snapshot: Mapping[str, Any] | None = None,
        commit: bool = True,
    ) -> None:
        _mark_queued_action_run_prepared(
            project,
            spec=self.spec,
            receipt_id=receipt_id,
            run_id=run_id,
            attempt_id=attempt_id,
            output_fields=output_fields,
            program=program,
            reservation_snapshot=reservation_snapshot,
            commit=commit,
        )

    def cleanup_reservation(
        self,
        project: Any,
        *,
        receipt_id: str,
    ) -> None:
        _cleanup_queued_action_reservation(project, receipt_id=receipt_id)


def _queued_action_lifecycle_adapter(
    spec: _QueuedActionSpec,
) -> _QueuedActionLifecycleAdapter:
    return _QueuedActionLifecycleAdapter(spec=spec)


def _terminalize_claimless_direct_failure(
    project: Any,
    *,
    project_id: str,
    action_kind: str,
    stored_receipt: StoredReceipt | None,
    error: ActionError,
    project_write_failed_message: str,
) -> ActionResult:
    ctx = ActionCtx(project_id, action_kind)
    if stored_receipt is None:
        return ctx.failed(error)
    prior = stored_receipt.parsed()
    if prior.status in FINISHED_RECEIPT_STATUSES:
        return _result_from_receipt(prior)
    failed = prior.model_copy(update={"status": "failed", "errors": [error]})
    try:
        project.db.execute("BEGIN IMMEDIATE")
        landed = ReceiptStore(project).update_body_status(
            failed,
            require_status="running",
            commit=False,
        )
        if not landed:
            project.db.rollback()
            return ctx.fail("project_write_failed", project_write_failed_message)
        project.db.commit()
    except Exception:
        project.db.rollback()
        logger.debug(
            "claimless_direct_failure_receipt_failed",
            exc_info=True,
            extra={
                "event": "claimless_direct_failure_receipt_failed",
                "action_kind": action_kind,
            },
        )
        return ctx.fail("project_write_failed", project_write_failed_message)
    except BaseException:
        project.db.rollback()
        raise
    return _result_from_receipt(failed)


def _receipt_for_idempotency(project: Any, key: str | None):
    return ReceiptStore(project).find_by_idempotency_key(key)


def _parse_receipt_created_at(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _running_receipt_is_stale(created_at: Any) -> bool:
    parsed = _parse_receipt_created_at(created_at)
    if parsed is None:
        return False
    age = datetime.now(timezone.utc) - parsed
    return age.total_seconds() >= RUNNING_RECEIPT_STALE_AFTER_SECONDS


def _recover_stale_receipt_attempt(project, receipt, params_hash):
    """Retry admission repairs a dead owner; read-only peeks never mutate it."""
    if (
        receipt is None
        or _row_value(receipt, "status") != "running"
        or _row_value(receipt, "params_hash") != params_hash
        or _row_value(receipt, "run_id") is None
    ):
        return receipt
    run_id = int(_row_value(receipt, "run_id"))
    owns_transaction = not project.db.in_transaction
    abandon_stale_dispatching_attempts(project, run_id, commit=owns_transaction)
    run = project.db.execute(
        "SELECT status,cancel_requested_at FROM runs WHERE id=?", (run_id,)
    ).fetchone()
    if (
        run is not None
        and run["status"] == "running"
        and run["cancel_requested_at"] is not None
    ):
        from .project_run_terminalization import (
            AbandonedRunRecoveryAuthority,
            terminalize_project_run,
        )

        # A stopped Change cannot resume. Its abandoned exact run may still
        # close the receipt and claims; returned facts remain intact. The
        # owner refuses live writers and ambiguous reserved effects itself.
        terminalize_project_run(
            project,
            run_id=run_id,
            receipt_id=str(_row_value(receipt, "id")),
            status="cancelled",
            authority=AbandonedRunRecoveryAuthority(require_cancel_intent=True),
            commit=owns_transaction,
        )
    return ReceiptStore(project).find_by_id(str(_row_value(receipt, "id")))


def _running_receipt_has_live_owner(project: Any, receipt: Any) -> bool:
    """Keep stale-looking reservations while their durable owner can still act."""
    receipt_id = str(_row_value(receipt, "id"))
    run_id = _row_value(receipt, "run_id")
    run = None
    if run_id is not None:
        run = project.db.execute(
            "SELECT status,current_attempt_id FROM runs WHERE id=?", (int(run_id),)
        ).fetchone()
        # A terminal run with a still-running receipt belongs to terminal
        # repair, not age-based reservation cleanup.
        if run is None or str(run["status"]) != "running":
            return True

    from frisket.engine.store.leases import lease_held

    if any(
        lease_held(
            row["claim_token"], row["lease_expires_at"], datetime.now(timezone.utc)
        )
        for row in project.db.execute(
            "SELECT claim_token,lease_expires_at FROM output_column_claims WHERE status='active' AND "
            "(receipt_id=? OR run_id=?)",
            (receipt_id, run_id),
        )
    ):
        return True

    if run_id is not None:
        attempts = project.db.execute(
            "SELECT id,state FROM execution_attempts WHERE run_id=?",
            (int(run_id),),
        ).fetchall()
        current_attempt_id = (
            None
            if run["current_attempt_id"] is None
            else str(run["current_attempt_id"])
        )
        states = {str(row["id"]): str(row["state"]) for row in attempts}
        if current_attempt_id is not None:
            # Only an abandoned attempt is eligible for the existing stale
            # retry path. Missing or otherwise terminal-but-unsettled current
            # attempts require normal run recovery.
            return states.get(current_attempt_id) != "abandoned"
        if any(
            state in {"created", "admitted", "dispatching"} for state in states.values()
        ):
            return True
        return False

    attempts = project.db.execute(
        "SELECT state FROM execution_attempts WHERE receipt_id=?", (receipt_id,)
    ).fetchall()
    return any(str(row["state"]) not in {"abandoned", "superseded"} for row in attempts)


def _running_receipt_stale_result(
    project: Any,
    existing: Any,
    *,
    params_hash: str,
    project_id: str,
    action: _ActionExecutionEnvelope,
) -> ActionResult | None:
    ctx = ActionCtx(project_id, action.kind)
    if _row_value(existing, "status") != "running":
        return None
    if not _running_receipt_is_stale(_row_value(existing, "created_at")):
        return None

    receipt_id = str(_row_value(existing, "id"))
    try:
        project.db.execute("BEGIN IMMEDIATE")
        receipts = ReceiptStore(project)
        current = receipts.find_by_id(receipt_id)
        if (
            current is None
            or current["params_hash"] != params_hash
            or current["status"] != "running"
            or not _running_receipt_is_stale(current["created_at"])
        ):
            project.db.rollback()
            return None
        current = _recover_stale_receipt_attempt(project, current, params_hash)
        if current["status"] != "running":
            project.db.commit()
            return _result_from_receipt(current.parsed())
        stopped = project.db.execute(
            "SELECT 1 FROM runs WHERE id=? AND cancel_requested_at IS NOT NULL",
            (current["run_id"],),
        ).fetchone()
        if stopped is not None:
            # Live or ambiguous stopped ownership belongs to terminal repair,
            # never deletion of its receipt and accounting lineage.
            project.db.commit()
            return None
        if _running_receipt_has_live_owner(project, current):
            project.db.commit()
            return None
        # No live attempt or lease remains. Release the expired reservation's
        # claims with its receipt so a new admission can actually acquire them.
        project.db.execute(
            "UPDATE output_column_claims SET status='released',released_at=datetime('now') "
            "WHERE status='active' AND (receipt_id=? OR run_id=?)",
            (receipt_id, current["run_id"]),
        )
        receipts.delete_if_status(receipt_id, "running", commit=False)
        project.db.commit()
    except Exception as exc:
        try:
            project.db.rollback()
        except Exception:
            pass
        return ctx.fail(
            "project_write_failed",
            f"failed to clear stale running receipt: {exc}",
            field="idempotency_key",
        )
    except BaseException:
        project.db.rollback()
        raise

    return ctx.fail(
        "idempotency_stale_running",
        "stale running idempotency reservation was cleared; "
        "retry the action to continue",
        field="idempotency_key",
        details={
            "receipt_id": receipt_id,
            "created_at": str(_row_value(existing, "created_at")),
            "stale_after_seconds": RUNNING_RECEIPT_STALE_AFTER_SECONDS,
            "retryable": True,
        },
    )


def _reserved_receipt_result_from_existing(
    project: Any,
    existing: Any,
    *,
    params_hash: str,
    project_id: str,
    action: _ActionExecutionEnvelope,
    replay_error_fn: Callable[[Receipt], ActionError | None] | None = None,
    guard_running: bool = True,
    **_ignored: Any,
) -> ActionResult:
    """Replay the receipt an idempotency key already names.

    ``guard_running=False`` is for in-transaction hosts, whose receipts are
    never left running for another caller to observe.
    """
    ctx = ActionCtx(project_id, action.kind)
    if existing["params_hash"] != params_hash:
        return ctx.fail(
            "idempotency_conflict",
            "idempotency_key was already used with different normalized params",
            field="idempotency_key",
        )
    stale = (
        _running_receipt_stale_result(
            project,
            existing,
            params_hash=params_hash,
            project_id=project_id,
            action=action,
        )
        if guard_running
        else None
    )
    if stale is not None:
        return stale
    if guard_running and existing["status"] == "running":
        return ctx.fail(
            "idempotency_in_progress",
            f"idempotency_key is already reserved by a running {action.kind} action",
            field="idempotency_key",
            details={"receipt_id": existing["id"]},
        )
    receipt = Receipt.model_validate(json.loads(existing["body"]))
    replay_error = replay_error_fn(receipt) if replay_error_fn is not None else None
    if replay_error is not None:
        return ctx.failed(replay_error)
    return _result_from_receipt(receipt)


def _reserve_running_action_receipt(
    project: Any,
    action: _ActionExecutionEnvelope,
    *,
    params_hash: str,
    project_id: str,
    reservation_kind: str,
    result_from_existing_fn: Callable[..., ActionResult | None] | None = None,
    replay_error_fn: Callable[[Receipt], ActionError | None] | None = None,
    params_model: type[BaseModel] | None = None,
    edition_run_context: Mapping[str, Any] | None = None,
    commit: bool = True,
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
                    "kind": reservation_kind,
                    "params_hash": params_hash,
                },
            )
        ],
    )
    try:
        if commit:
            project.db.execute("BEGIN IMMEDIATE")
        elif not project.db.in_transaction:
            raise RuntimeError(
                "_reserve_running_action_receipt(commit=False) requires a "
                "caller-owned project transaction"
            )
        existing = _receipt_for_idempotency(project, action.idempotency_key)
        if existing is not None:
            if commit:
                project.db.rollback()
            kwargs: dict[str, Any] = {
                "params_hash": params_hash,
                "project_id": project_id,
                "action": action,
            }
            if params_model is not None:
                kwargs["params"] = params_model.model_validate(action.params)
            if result_from_existing_fn is not None:
                replay = result_from_existing_fn(project, existing, **kwargs)
                if replay is None:
                    return {
                        "action_id": str(_row_value(existing, "action_id")),
                        "receipt_id": str(_row_value(existing, "id")),
                        "reservation_reused": "true",
                    }
                return replay
            return _reserved_receipt_result_from_existing(
                project, existing, **kwargs, replay_error_fn=replay_error_fn
            )
        ReceiptStore(project).insert_running(
            receipt,
            edition_run_context=edition_run_context,
            commit=False,
        )
        if commit:
            project.db.commit()
    except BaseException as exc:
        if not commit:
            raise
        project.db.rollback()
        if not isinstance(exc, Exception):
            raise
        logger.debug(
            "action_failed",
            exc_info=True,
            extra={"event": "action_failed", "action_kind": action.kind},
        )
        return ActionCtx(project_id, action.kind).fail(
            "project_write_failed", "project write failed"
        )
    return {"action_id": action_id, "receipt_id": receipt_id}


def _queued_receipt_job_id(receipt: Receipt) -> int | None:
    for item in receipt.evidence:
        ref = item.ref
        if ref.get("queue_kind") != "project.run":
            continue
        job_id = ref.get("job_id")
        if isinstance(job_id, int) and not isinstance(job_id, bool):
            return job_id
    return None


def _queued_action_result_from_existing(
    spec: _QueuedActionSpec,
) -> Callable[..., ActionResult]:
    def result_from_existing(
        project: Any,
        existing: Any,
        *,
        params_hash: str,
        project_id: str,
        action: ActionSpec,
        params: BaseModel,
    ) -> ActionResult:
        ctx = ActionCtx(project_id, action.kind)
        if existing["params_hash"] != params_hash:
            return ctx.fail(
                "idempotency_conflict",
                "idempotency_key was already used with different normalized params",
                field="idempotency_key",
            )
        receipt = Receipt.model_validate(json.loads(existing["body"]))
        if receipt.status == "queued":
            job_id = _queued_receipt_job_id(receipt)
            if receipt.run_id is None or job_id is None:
                return ctx.fail(
                    "idempotency_in_progress",
                    "idempotency_key is already reserved by a queued "
                    f"{action.kind} action",
                    field="idempotency_key",
                    details={
                        "receipt_id": receipt.receipt_id,
                        "retryable": True,
                    },
                )
            return _result_from_receipt(receipt).model_copy(update={"job_id": job_id})
        if receipt.status in {"failed", "cancelled"}:
            result = _result_from_receipt(receipt)
            job_id = _queued_receipt_job_id(receipt)
            if job_id is not None:
                result = result.model_copy(update={"job_id": job_id})
            return result
        if spec.completed_result_from_existing_fn is not None:
            return spec.completed_result_from_existing_fn(
                project,
                existing,
                params_hash=params_hash,
                project_id=project_id,
                action=action,
                params=params,
            )
        return ctx.fail(
            "project_write_failed", "queued action spec has no completed replay handler"
        )

    return result_from_existing


def _queued_action_reservation_payload(
    project: Any,
    action: _ActionExecutionEnvelope,
    params: BaseModel,
    *,
    spec: _QueuedActionSpec,
    project_id: str,
    run_precheck: bool = True,
    router: Any | None = None,
    program: Recipe | None = None,
) -> dict[str, Any] | ActionResult:
    ctx = ActionCtx(project_id, action.kind)
    runner_spec = _apply_action_envelope_to_runner_spec(
        action, spec.runner_spec_fn(params)
    )
    consented_hash = getattr(params, "consented_promise_set_hash", None)
    if consented_hash:
        runner_spec["consented_promise_set_hash"] = consented_hash
    resolved = (
        spec.resolve_fn(project, params, runner_spec, router)
        if spec.resolve_needs_router
        else spec.resolve_fn(project, params, runner_spec)
    )
    if isinstance(resolved, ActionError):
        return ctx.failed(resolved)
    try:
        local_endpoint_bindings = _local_endpoint_bindings_for_runner_spec(
            runner_spec,
            router=router,
        )
    except (LLMError, RuntimeError, ValueError) as exc:
        return ctx.fail("local_endpoint_unavailable", str(exc))
    precomputed_output_fields: list[dict[str, Any]] | None = None
    if run_precheck:
        from frisket.engine.runner.validation import recipe_for_spec

        recipe = program or recipe_for_spec(runner_spec)
        if recipe.consumes_resolution:
            precomputed_output_fields = recipe.output_fields(runner_spec)
            output_precheck = spec.precheck_fn(
                project,
                params,
                runner_spec,
                output_fields=precomputed_output_fields,
            )
        else:
            if program is not None:
                precomputed_output_fields = program.output_fields(runner_spec)
            output_precheck = spec.precheck_fn(project, params, runner_spec)
        if output_precheck is not None:
            if precomputed_output_fields is not None:
                sheet_id = runner_spec.get("sheet_id")
                if type(sheet_id) is int:
                    claim_store = OutputColumnClaimStore(project)
                    for field in precomputed_output_fields:
                        active_claim = claim_store.active_for_output_name(
                            sheet_id=sheet_id,
                            output_name=str(field["name"]),
                        )
                        if active_claim is not None:
                            output_precheck = _generated_column_busy_error(
                                action.kind,
                                active_claim,
                                field=spec.output_claim_error_field,
                            )
                            break
            return ctx.failed(output_precheck)
    # Absence of a confirmed field/fn is NOT consent: the default is False,
    # matching the sibling needs_confirmation
    # checks. Free ops are unaffected (a $0/None-free estimate never gates);
    # a cost-policied op must thread confirmation explicitly.
    confirmed = (
        spec.confirmed_fn(params)
        if spec.confirmed_fn is not None
        else bool(getattr(params, "confirmed", False))
    )
    payload = {
        "runner_spec": runner_spec,
        "confirmed": confirmed,
        "local_endpoint_bindings": local_endpoint_bindings,
        **{codec.key: resolved[codec.key] for codec in spec.payload_codecs},
    }
    if precomputed_output_fields is not None:
        # Transient caller value, removed by _reserve_queued_action before
        # reservation data is returned or projected. It is the one dynamic
        # descriptor mint shared by the collision precheck, claim acquisition,
        # and MapRunner preparation.
        payload["_precomputed_output_fields"] = precomputed_output_fields
    return payload


def _local_endpoint_bindings_for_runner_spec(
    runner_spec: Mapping[str, Any],
    *,
    router: Any | None,
) -> list[dict[str, str]]:
    """Freeze exact local endpoint origins from the spec that will execute.

    Only canonical model-bearing keys participate. Prompts and other arbitrary
    string values must never be interpreted as provider authority merely
    because their text happens to start with ``ollama/``.
    """

    local_model_ids: set[str] = set()

    def collect(value: Any) -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                if (
                    key in {"model", "engine", "model_id"}
                    and isinstance(child, str)
                    and child.startswith("ollama/")
                ):
                    local_model_ids.add(child)
                elif isinstance(child, (Mapping, list, tuple)):
                    collect(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                collect(child)

    collect(runner_spec)
    if local_model_ids and router is None:
        raise RuntimeError(
            "a canonical local model reference requires an execution router"
        )

    bindings: dict[str, str] = {}
    for model_id in sorted(local_model_ids):
        endpoint_id, origin = router.local_endpoint_identity(model_id)
        previous_origin = bindings.setdefault(endpoint_id, origin)
        if previous_origin != origin:
            raise RuntimeError(
                f"local endpoint {endpoint_id!r} resolved to two origins"
            )
    return [
        {"endpoint_id": endpoint_id, "origin": origin}
        for endpoint_id, origin in sorted(bindings.items())
    ]


def _queued_action_runner_spec_with_marker(
    runner_spec: Mapping[str, Any],
    *,
    action_kind: str,
    receipt_id: str,
    action_id: str,
    params_hash: str,
) -> dict[str, Any]:
    marked = dict(runner_spec)
    marked[QUEUED_ACTION_RUN_MARKER_PARAM] = _queued_action_run_marker(
        action_kind=action_kind,
        receipt_id=receipt_id,
        action_id=action_id,
        params_hash=params_hash,
    )
    return marked


def _queued_action_run_marker(
    *, action_kind: str, receipt_id: str, action_id: str, params_hash: str
) -> dict[str, str]:
    """The runner-spec marker binding a queued project run to its receipt."""
    return {
        "schema_version": QUEUED_ACTION_RUN_MARKER_SCHEMA,
        "action_kind": action_kind,
        "receipt_id": receipt_id,
        "action_id": action_id,
        "params_hash": params_hash,
    }


def _queued_receipt_prepared_link(receipt: Receipt) -> Mapping[str, Any] | None:
    if type(receipt.run_id) is not int:
        return None
    matches = [
        item.ref
        for item in receipt.evidence
        if item.ref.get("kind") == QUEUED_ACTION_RUN_PREPARED_RECORD_KIND
        and item.ref.get("queue_kind") == "project.run"
        and item.ref.get("run_id") == receipt.run_id
    ]
    if len(matches) != 1:
        return None
    return matches[0]


def _queued_receipt_prepared_attempt_id(receipt: Receipt) -> str | None:
    link = _queued_receipt_prepared_link(receipt)
    if link is None:
        return None
    attempt_id = link.get("attempt_id")
    if not isinstance(attempt_id, str) or not attempt_id:
        return None
    return attempt_id


def _recover_existing_queued_action_reservation(
    project: Any,
    existing: Any,
    *,
    params_hash: str,
    project_id: str,
    action: _ActionExecutionEnvelope,
    params: BaseModel,
    spec: _QueuedActionSpec,
    router: Any | None = None,
    program: Recipe | None = None,
) -> dict[str, Any] | ActionResult | None:
    if existing["params_hash"] != params_hash or existing["status"] != "queued":
        return None
    receipt = Receipt.model_validate(json.loads(existing["body"]))
    if type(receipt.run_id) is not int:
        return None
    # An already-enqueued receipt is observable and must replay its existing
    # run/job identity. Recovery is only for the prepared-before-enqueue gap.
    if _queued_receipt_job_id(receipt) is not None:
        return None
    prepared_link = _queued_receipt_prepared_link(receipt)
    if prepared_link is None:
        # A pre-atomicity half receipt has no retained population under GD-07;
        # recovery never scans ambient run markers.
        return None
    payload = _queued_action_reservation_payload(
        project,
        action,
        params,
        spec=spec,
        project_id=project_id,
        run_precheck=False,
        router=router,
        program=program,
    )
    if isinstance(payload, ActionResult):
        return payload
    if program is not None:
        original_snapshot = prepared_link.get("reservation_snapshot")
        current_snapshot = {
            codec.key: payload.get(codec.key) for codec in spec.payload_codecs
        }
        if not isinstance(original_snapshot, Mapping) or any(
            codec.key not in original_snapshot for codec in spec.payload_codecs
        ):
            original_snapshot = None
        if original_snapshot is None or current_snapshot != dict(original_snapshot):
            from frisket.engine.executor.queued_actions import (
                queued_v1_terminal_receipt_result,
            )

            error = ActionError(
                code="stale_input",
                message="source columns changed after this action was prepared",
                action_kind=action.kind,
                field="scope",
            )
            return queued_v1_terminal_receipt_result(
                project,
                project_id=project_id,
                run_id=receipt.run_id,
                status="failed",
                error=error,
                receipt_id=receipt.receipt_id,
                action_kind=action.kind,
                action_id=receipt.action_id or "act_queued_terminal",
                never_dispatched=True,
            )
        payload.update(original_snapshot)
    from frisket.engine.runner.validation import recipe_for_spec

    recovery_recipe = program or recipe_for_spec(payload["runner_spec"])
    routed = bool(recovery_recipe.consumes_resolution)
    prepared_attempt_id = _queued_receipt_prepared_attempt_id(receipt)
    replacement_needed = False
    if routed:
        if prepared_attempt_id is None:
            return None
        if project.db.in_transaction:
            abandon_stale_dispatching_attempts(
                project,
                receipt.run_id,
                commit=False,
            )
        attempt = project.db.execute(
            "SELECT state FROM execution_attempts WHERE id=? AND run_id=?",
            (prepared_attempt_id, receipt.run_id),
        ).fetchone()
        if attempt is None:
            return None
        state = str(attempt["state"])
        if state == "abandoned":
            replacement_needed = True
        elif state not in {"admitted", "dispatching"}:
            return None
    output_fields = OutputColumnClaimStore(project).active_output_fields(
        claim_token=_output_claim_token(receipt.receipt_id),
        run_id=receipt.run_id,
        require_frozen_descriptors=True,
    )
    return {
        "action_id": receipt.action_id or _new_id("act"),
        "receipt_id": receipt.receipt_id,
        "params_hash": params_hash,
        "prepared_run_id": receipt.run_id,
        "prepared_attempt_id": prepared_attempt_id,
        "replace_abandoned_attempt": replacement_needed,
        "output_fields": output_fields,
        **payload,
    }


def _reserve_queued_action(
    project: Any,
    action: _ActionExecutionEnvelope,
    params: BaseModel,
    *,
    spec: _QueuedActionSpec,
    project_id: str,
    recover_unobservable: bool = False,
    router: Any | None = None,
    program: Recipe | None = None,
    commit: bool = True,
) -> dict[str, Any] | ActionResult:
    params_hash_fn = spec.params_hash_fn or _params_hash_without_confirmed
    params_hash = params_hash_fn(action)
    result_from_existing = _queued_action_result_from_existing(spec)
    existing = _receipt_for_idempotency(project, action.idempotency_key)
    if existing is not None and recover_unobservable:
        recovered = _recover_existing_queued_action_reservation(
            project,
            existing,
            params_hash=params_hash,
            project_id=project_id,
            action=action,
            params=params,
            spec=spec,
            router=router,
            program=program,
        )
        if recovered is not None:
            return recovered
    if existing is not None:
        return result_from_existing(
            project,
            existing,
            params_hash=params_hash,
            project_id=project_id,
            action=action,
            params=params,
        )
    payload = _queued_action_reservation_payload(
        project,
        action,
        params,
        spec=spec,
        project_id=project_id,
        router=router,
        program=program,
    )
    if isinstance(payload, ActionResult):
        return payload
    output_fields = payload.pop("_precomputed_output_fields", None)
    if output_fields is None:
        output_fields = _runner_output_fields(payload["runner_spec"])

    action_id = _new_id("act")
    receipt_id = _new_id("receipt")
    receipt = Receipt(
        receipt_id=receipt_id,
        project_id=project_id,
        action_id=action_id,
        action_kind=action.kind,
        idempotency_key=action.idempotency_key,
        params_hash=params_hash,
        status="queued",
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
    owns_transaction = commit and not project.db.in_transaction
    try:
        if commit:
            project.db.execute("BEGIN IMMEDIATE")
        elif not project.db.in_transaction:
            raise RuntimeError(
                "_reserve_queued_action(commit=False) requires a "
                "caller-owned project transaction"
            )
        existing = _receipt_for_idempotency(project, action.idempotency_key)
        if existing is not None:
            if commit:
                project.db.rollback()
            if recover_unobservable:
                recovered = _recover_existing_queued_action_reservation(
                    project,
                    existing,
                    params_hash=params_hash,
                    project_id=project_id,
                    action=action,
                    params=params,
                    spec=spec,
                    router=router,
                    program=program,
                )
                if recovered is not None:
                    return recovered
            return result_from_existing(
                project,
                existing,
                params_hash=params_hash,
                project_id=project_id,
                action=action,
                params=params,
            )
        ReceiptStore(project).insert_queued(receipt, commit=False)
        claim_error = _acquire_output_claims_for_runner(
            project,
            action,
            payload["runner_spec"],
            receipt_id=receipt_id,
            output_fields=output_fields,
            error_field=spec.output_claim_error_field,
            commit=False,
        )
        if claim_error is not None:
            if commit:
                project.db.rollback()
            return ActionCtx(project_id, action.kind).failed(claim_error)
        from frisket.engine.runner.validation import recipe_for_spec

        if (program or recipe_for_spec(payload["runner_spec"])).consumes_resolution:
            post_claim_precheck = spec.precheck_fn(
                project,
                params,
                payload["runner_spec"],
                output_fields=output_fields,
            )
        else:
            post_claim_precheck = spec.precheck_fn(
                project, params, payload["runner_spec"]
            )
        if post_claim_precheck is not None:
            if commit:
                project.db.rollback()
            return ActionCtx(project_id, action.kind).failed(post_claim_precheck)
        if commit:
            project.db.commit()
    except BaseException as exc:
        if owns_transaction and project.db.in_transaction:
            project.db.rollback()
        if not commit or not isinstance(exc, Exception):
            raise
        logger.debug(
            "action_failed",
            exc_info=True,
            extra={"event": "action_failed", "action_kind": action.kind},
        )
        return ActionCtx(project_id, action.kind).fail(
            "project_write_failed", "project write failed"
        )
    return {
        "action_id": action_id,
        "receipt_id": receipt_id,
        "params_hash": params_hash,
        "output_fields": output_fields,
        **payload,
    }


def _mark_queued_action_run_prepared(
    project: Any,
    *,
    spec: _QueuedActionSpec,
    receipt_id: str,
    run_id: int,
    attempt_id: str | None,
    output_fields: list[dict[str, Any]],
    program: Recipe | None = None,
    reservation_snapshot: Mapping[str, Any] | None = None,
    commit: bool = True,
) -> None:
    prepared_ref: dict[str, Any] = {
        "kind": QUEUED_ACTION_RUN_PREPARED_RECORD_KIND,
        "queue_kind": "project.run",
        "run_id": run_id,
        "action_queue_kind": spec.queue_job_kind,
    }
    if attempt_id is not None:
        prepared_ref["attempt_id"] = attempt_id
    if reservation_snapshot is not None:
        prepared_ref["reservation_snapshot"] = dict(reservation_snapshot)
    _bind_receipt_to_prepared_run(
        project,
        receipt_id=receipt_id,
        run_id=run_id,
        receipt_status="queued",
        prepared_ref=prepared_ref,
        output_fields=output_fields,
        program=program,
        defer_publication=getattr(program, "defer_generation_seal", False) is True,
        commit=commit,
    )


def _hide_deferred_created_columns(
    project: Any, run_id: int, program: Recipe | None
) -> None:
    if getattr(program, "defer_generation_seal", False) is not True:
        return
    from frisket.engine.runner.preparation import _created_generated_column_ids

    row = project.db.execute("SELECT op_id FROM runs WHERE id=?", (run_id,)).fetchone()
    created = _created_generated_column_ids(project, int(row["op_id"]))
    # Existing replacement targets retain their prior visible generation.
    project.db.executemany(
        "UPDATE columns SET hidden=1 WHERE id=?",
        ((column_id,) for column_id in created),
    )


def _mark_running_action_run_prepared(
    project: Any,
    *,
    receipt_id: str,
    run_id: int,
    attempt_id: str | None,
    output_fields: list[dict[str, Any]],
    program: Recipe | None,
    defer_publication: bool,
    commit: bool,
) -> None:
    """Bind a direct action's existing receipt and claim group to its run."""

    _bind_receipt_to_prepared_run(
        project,
        receipt_id=receipt_id,
        run_id=run_id,
        receipt_status="running",
        prepared_ref=None
        if attempt_id is None
        else {
            "kind": QUEUED_ACTION_RUN_PREPARED_RECORD_KIND,
            "queue_kind": "direct",
            "run_id": run_id,
            "attempt_id": attempt_id,
        },
        output_fields=output_fields,
        program=program,
        defer_publication=defer_publication,
        commit=commit,
    )


def _bind_receipt_to_prepared_run(
    project: Any,
    *,
    receipt_id: str,
    run_id: int,
    receipt_status: Literal["queued", "running"],
    prepared_ref: dict[str, Any] | None,
    output_fields: list[dict[str, Any]],
    program: Recipe | None,
    defer_publication: bool,
    commit: bool,
) -> None:
    """Point a reserved receipt and its output claim group at a prepared run."""

    row = ReceiptStore(project).find_by_id(receipt_id)
    if row is None:
        raise RuntimeError(
            f"{receipt_status} receipt {receipt_id!r} disappeared during preparation"
        )
    receipt = Receipt.model_validate(json.loads(row["body"]))
    evidence = [
        item
        for item in receipt.evidence
        if item.ref.get("kind") != QUEUED_ACTION_RUN_PREPARED_RECORD_KIND
    ]
    if prepared_ref is not None:
        evidence.append(ReceiptEvidence(ref=prepared_ref))
    receipt = receipt.model_copy(update={"run_id": run_id, "evidence": evidence})
    try:
        if commit:
            project.db.execute("BEGIN IMMEDIATE")
        elif not project.db.in_transaction:
            raise RuntimeError(
                f"_mark_{receipt_status}_action_run_prepared(commit=False) requires a "
                "caller-owned project transaction"
            )
        if not ReceiptStore(project).update_body_status(
            receipt, require_status=receipt_status, commit=False
        ):
            raise RuntimeError(
                f"{receipt_status} receipt {receipt_id!r} is no longer preparable"
            )
        OutputColumnClaimStore(project).bind_to_run(
            claim_token=_output_claim_token(receipt_id),
            run_id=run_id,
            expected_output_names=[str(field["name"]) for field in output_fields],
            commit=False,
        )
        declare_bound_run_outputs(
            project,
            run_id=run_id,
            output_fields=output_fields,
            program=program,
            claim_token=_output_claim_token(receipt_id),
            defer_publication=defer_publication,
        )
        _hide_deferred_created_columns(project, run_id, program)
        if receipt_status == "running":
            project.db.execute(
                "UPDATE runs SET edition_run_context="
                "(SELECT edition_run_context FROM receipts WHERE id=?) "
                "WHERE id=? AND edition_run_context IS NULL",
                (receipt_id, run_id),
            )
        if commit:
            project.db.commit()
    except BaseException:
        if commit:
            project.db.rollback()
        raise


def _mark_queued_action_enqueued(
    project: Any,
    *,
    spec: _QueuedActionSpec,
    receipt_id: str,
    run_id: int,
    job_id: int,
) -> None:
    row = ReceiptStore(project).find_by_id(receipt_id)
    if row is None:
        return
    receipt = Receipt.model_validate(json.loads(row["body"]))
    evidence = [
        item for item in receipt.evidence if item.ref.get("kind") != spec.queue_job_kind
    ]
    evidence.append(
        ReceiptEvidence(
            ref={
                "kind": spec.queue_job_kind,
                "queue_kind": "project.run",
                "run_id": run_id,
                "job_id": job_id,
            }
        )
    )
    receipt = receipt.model_copy(update={"run_id": run_id, "evidence": evidence})
    try:
        project.db.execute("BEGIN IMMEDIATE")
        updated = ReceiptStore(project).update_body_status(
            receipt,
            require_status="queued",
            commit=False,
        )
        if updated:
            OutputColumnClaimStore(project).bind_to_run(
                claim_token=_output_claim_token(receipt_id),
                run_id=run_id,
                job_id=job_id,
                commit=False,
            )
        else:
            _release_queued_output_claim_if_terminal(project, receipt_id=receipt_id)
        project.db.commit()
        if not updated:
            logger.warning(
                "queued_receipt_enqueue_mark_skipped",
                extra={
                    "event": "queued_receipt_enqueue_mark_skipped",
                    "receipt_id": receipt_id,
                    "run_id": run_id,
                    "job_id": job_id,
                    "status": receipt.status,
                },
            )
    except Exception:
        project.db.rollback()
        logger.debug(
            "queued_receipt_mark_failed",
            exc_info=True,
            extra={"event": "queued_receipt_mark_failed", "receipt_id": receipt_id},
        )
    except BaseException:
        project.db.rollback()
        raise


def _release_queued_output_claim_if_terminal(
    project: Any,
    *,
    receipt_id: str,
) -> None:
    row = ReceiptStore(project).find_by_id(receipt_id)
    receipt_status = str(row["status"]) if row is not None else "failed"
    if receipt_status not in {"completed", "failed", "cancelled"}:
        return
    claim_status = "released" if receipt_status == "completed" else receipt_status
    OutputColumnClaimStore(project).release(
        claim_token=_output_claim_token(receipt_id),
        status=claim_status,
        commit=False,
    )


def _cleanup_queued_action_reservation(
    project: Any,
    *,
    receipt_id: str,
) -> None:
    try:
        project.db.execute("BEGIN IMMEDIATE")
        OutputColumnClaimStore(project).discard_for_receipt(
            receipt_id=receipt_id,
            commit=False,
        )
        ReceiptStore(project).delete_queued(receipt_id, commit=False)
        project.db.commit()
    except Exception:
        project.db.rollback()
        logger.debug(
            "queued_receipt_delete_failed",
            exc_info=True,
            extra={"event": "queued_receipt_delete_failed", "receipt_id": receipt_id},
        )
    except BaseException:
        project.db.rollback()
        raise


def _delete_reserved_action_receipt(
    project: Any,
    receipt_id: str,
    *,
    action_kind: str,
) -> None:
    try:
        project.db.execute("BEGIN IMMEDIATE")
        receipts = ReceiptStore(project)
        deleted = receipts.delete_if_status(receipt_id, "running", commit=False)
        if not deleted:
            row = receipts.find_by_id(receipt_id)
            logger.debug(
                "reservation_delete_missed",
                extra={
                    "event": "reservation_delete_missed",
                    "action_kind": action_kind,
                    "receipt_id": receipt_id,
                    "status": row["status"] if row is not None else None,
                },
            )
        project.db.commit()
    except Exception:
        project.db.rollback()
        logger.debug(
            "action_failed",
            exc_info=True,
            extra={"event": "action_failed", "action_kind": action_kind},
        )
    except BaseException:
        project.db.rollback()
        raise


def _cleanup_reserved_receipt_on_failed_result(
    project: Any,
    result: ActionResult,
    *,
    receipt_id: str,
    action_kind: str,
) -> ActionResult:
    if result.status == "failed" and result.receipt_id is None:
        _delete_reserved_action_receipt(
            project,
            receipt_id,
            action_kind=action_kind,
        )
    return result


def _finalize_reserved_action_receipt(
    project: Any,
    action: _ActionExecutionEnvelope,
    *,
    params_hash: str,
    project_id: str,
    receipt: Receipt,
    reservation_lost_message: str | None = None,
    result_from_existing_fn: Callable[..., ActionResult | None] | None = None,
    replay_error_fn: Callable[[Receipt], ActionError | None] | None = None,
    result_from_existing_kwargs: dict[str, Any] | None = None,
    require_running_status: bool = True,
    before_commit: Callable[[], None] | None = None,
    commit: bool = True,
) -> ActionResult | None:
    ctx = ActionCtx(project_id, action.kind)
    if reservation_lost_message is None:
        reservation_lost_message = f"{action.kind} idempotency reservation was lost"
    try:
        if commit:
            project.db.execute("BEGIN IMMEDIATE")
        elif not project.db.in_transaction:
            raise RuntimeError(
                "reserved receipt finalization with commit=False requires "
                "a caller-owned transaction"
            )
        existing = _receipt_for_idempotency(project, action.idempotency_key)
        if existing is None:
            if commit:
                project.db.rollback()
            return ctx.fail("project_write_failed", reservation_lost_message)
        if existing["id"] != receipt.receipt_id:
            if commit:
                project.db.rollback()
            kwargs = dict(result_from_existing_kwargs or {})
            if result_from_existing_fn is None:
                return _reserved_receipt_result_from_existing(
                    project,
                    existing,
                    params_hash=params_hash,
                    project_id=project_id,
                    action=action,
                    replay_error_fn=replay_error_fn,
                    **kwargs,
                )
            existing_result = result_from_existing_fn(
                project,
                existing,
                params_hash=params_hash,
                project_id=project_id,
                action=action,
                **kwargs,
            )
            if existing_result is None:
                return ctx.fail("project_write_failed", reservation_lost_message)
            return existing_result
        if existing["params_hash"] != params_hash:
            if commit:
                project.db.rollback()
            return ctx.fail(
                "idempotency_conflict",
                "idempotency_key was already used with different normalized params",
                field="idempotency_key",
            )
        if existing["status"] not in {"queued", "running"}:
            # Re-delivery may project the run again, but a completed receipt is
            # the durable answer. Explicit resume must reopen it first.
            stored = ReceiptStore(project).parsed_by_id(receipt.receipt_id)
            assert stored is not None
            if commit:
                project.db.rollback()
            return _result_from_receipt(stored)
        updated = ReceiptStore(project).update_if_status_in(
            receipt,
            {"running"} if require_running_status else {"queued", "running"},
            commit=False,
        )
        if not updated:
            if commit:
                project.db.rollback()
            return ctx.fail(
                "project_write_failed", f"{action.kind} receipt update failed"
            )
        if before_commit is not None:
            before_commit()
        if commit:
            project.db.commit()
    except StaleAttemptWriter:
        if commit:
            project.db.rollback()
        raise
    except Exception:
        if not commit:
            raise
        project.db.rollback()
        logger.warning(
            "reserved_receipt_finalization_failed",
            exc_info=True,
            extra={
                "event": "reserved_receipt_finalization_failed",
                "action_kind": action.kind,
                "receipt_id": receipt.receipt_id,
            },
        )
        return ctx.fail("project_write_failed", "project write failed")
    except BaseException:
        if commit:
            project.db.rollback()
        raise
    return None


def _reserved_maprunner_result_from_existing(
    spec: _ReservedMaprunnerActionSpec,
) -> Callable[..., ActionResult | None]:
    def resumable_direct_run(
        project: Any, existing: Any, *, params_hash: str, action: ActionSpec
    ) -> bool:
        """Whether this resume-capable action left an exact owned run idle.

        Only an explicitly halted last attempt, no active claims, and a
        missing current-writer pointer allow continuing the still-running
        receipt. The normal reservation and MapRunner resume path then opens
        a fresh attempt under its own claim.
        """
        if (
            spec.resume_run_id_fn is None
            or _row_value(existing, "status") != "running"
            or _row_value(existing, "params_hash") != params_hash
        ):
            return False
        try:
            receipt = Receipt.model_validate(json.loads(_row_value(existing, "body")))
        except (TypeError, ValueError):
            return False
        if (
            receipt.receipt_id != str(_row_value(existing, "id"))
            or receipt.run_id is None
            or receipt.run_id != _row_value(existing, "run_id")
            or receipt.action_kind != action.kind
        ):
            return False
        run_id = int(receipt.run_id)
        run = project.db.execute(
            "SELECT runs.status,runs.current_attempt_id,runs.action_kind,ops.spec "
            "FROM runs JOIN ops ON ops.id=runs.op_id WHERE runs.id=?",
            (run_id,),
        ).fetchone()
        if (
            run is None
            or run["status"] != "running"
            or run["current_attempt_id"] is not None
            or run["action_kind"] != action.kind
        ):
            return False
        try:
            persisted_spec = json.loads(run["spec"] or "{}")
        except (TypeError, ValueError):
            return False
        if persisted_spec.get("idempotency_key") != action.idempotency_key:
            return False
        attempts = project.db.execute(
            "SELECT id,state,handler_authority_id FROM execution_attempts "
            "WHERE run_id=? ORDER BY seq",
            (run_id,),
        ).fetchall()
        if not attempts or str(attempts[-1]["state"]) != "halted":
            return False
        if any(
            str(attempt["state"]) in {"created", "admitted", "dispatching"}
            for attempt in attempts
        ):
            return False
        if project.db.execute(
            "SELECT 1 FROM output_column_claims WHERE status='active' "
            "AND (receipt_id=? OR run_id=?) LIMIT 1",
            (receipt.receipt_id, run_id),
        ).fetchone():
            return False
        last_attempt = attempts[-1]
        if last_attempt["handler_authority_id"] is not None:
            from frisket.engine.store.output_claims import OutputColumnClaimStore

            if not OutputColumnClaimStore(project).attempt_handler_execution_stopped(
                str(last_attempt["id"])
            ):
                return False
        return True

    def result_from_existing(
        project: Any,
        existing: Any,
        *,
        params_hash: str,
        project_id: str,
        action: ActionSpec,
        params: Any | None = None,
    ) -> ActionResult | None:
        if resumable_direct_run(
            project, existing, params_hash=params_hash, action=action
        ):
            return None
        typed_params = (
            params
            if params is not None
            else spec.params_model.model_validate(action.params)
        )
        replay_error_fn = None
        if spec.replay_error_fn is not None:
            replay_error_spec_fn = spec.replay_error_fn

            def replay_error_fn(receipt: Receipt) -> ActionError | None:
                return replay_error_spec_fn(
                    project,
                    typed_params,
                    receipt,
                    action,
                )

        return _reserved_receipt_result_from_existing(
            project,
            existing,
            params_hash=params_hash,
            project_id=project_id,
            action=action,
            replay_error_fn=replay_error_fn,
        )

    return result_from_existing


def _reserved_maprunner_reserve_fn(
    spec: _ReservedMaprunnerActionSpec,
) -> Callable[..., dict[str, str] | ActionResult]:
    result_from_existing_fn = _reserved_maprunner_result_from_existing(spec)

    def reserve(
        project: Any,
        action: ActionSpec,
        *,
        params_hash: str,
        project_id: str,
        commit: bool = True,
        edition_run_context: Mapping[str, Any] | None = None,
    ) -> dict[str, str] | ActionResult:
        return _reserve_running_action_receipt(
            project,
            action,
            params_hash=params_hash,
            project_id=project_id,
            reservation_kind=spec.reservation_kind,
            result_from_existing_fn=result_from_existing_fn,
            params_model=spec.params_model,
            edition_run_context=edition_run_context,
            commit=commit,
        )

    return reserve


def _reserved_maprunner_delete_fn(
    spec: _ReservedMaprunnerActionSpec,
) -> Callable[[Any, str], None]:
    def delete(project: Any, receipt_id: str) -> None:
        _delete_reserved_action_receipt(
            project,
            receipt_id,
            action_kind=spec.kind,
        )

    return delete
