"""Action receipt reservation, output claims, queue publication, and finalization."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

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
    _QueuedPayloadCodec,
    _ReservedMaprunnerActionSpec,
    _apply_action_envelope_to_runner_spec,
)
from frisket.engine.executor.action_receipts import _result_from_receipt
from frisket.engine.executor.action_support import (
    _failed_result,
    _new_id,
    _params_hash,
    _params_hash_without_confirmed,
    _row_value,
)


logger = logging.getLogger("frisket.executor")


RUNNING_RECEIPT_STALE_AFTER_SECONDS = 60 * 60
QUEUED_ACTION_RUN_MARKER_PARAM = "_frisket_queued_action_run"
QUEUED_ACTION_RUN_MARKER_SCHEMA = "frisket.internal.queued_action_run.v1"
QUEUED_ACTION_RUN_PREPARED_EVIDENCE_KIND = "queued_action_run_prepared"


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


def _output_column_busy_error(
    action_kind: str, conflict: Any, *, field: str = "params.output_name"
) -> ActionError:
    return ActionError(
        code="output_column_busy",
        message="The target output column is claimed by a running action.",
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
    _claims, conflict = OutputColumnClaimStore(project).acquire(
        sheet_id=sheet_id,
        output_names=output_names,
        action_kind=action.kind,
        receipt_id=receipt_id,
        claim_token=_output_claim_token(receipt_id),
        lease_seconds=int(STALE_DISPATCHING_AGE.total_seconds()),
        details={
            "idempotency_key": action.idempotency_key,
            # Freeze the producer descriptors with the claim. Queue recovery
            # must not rebuild a compatibility key from whatever recipe code
            # happens to be installed later.
            "output_fields": [dict(field) for field in output_fields],
        },
        commit=commit,
    )
    if conflict is not None:
        return _output_column_busy_error(action.kind, conflict, field=error_field)
    return None


@dataclass(frozen=True)
class _DirectActionFinalizeMetadata:
    action_kind: str
    result_from_existing_fn: Callable[..., ActionResult]
    result_from_existing_kwargs: dict[str, Any] | None = None
    require_running_status: bool = True

    @property
    def reservation_lost_message(self) -> str:
        return f"{self.action_kind} idempotency reservation was lost"

    @property
    def update_failed_message(self) -> str:
        return f"{self.action_kind} receipt update failed"


def _direct_action_finalize_metadata(
    action_kind: str,
    *,
    result_from_existing_fn: Callable[..., ActionResult],
    result_from_existing_kwargs: dict[str, Any] | None = None,
    require_running_status: bool = True,
) -> _DirectActionFinalizeMetadata:
    return _DirectActionFinalizeMetadata(
        action_kind=action_kind,
        result_from_existing_fn=result_from_existing_fn,
        result_from_existing_kwargs=result_from_existing_kwargs,
        require_running_status=require_running_status,
    )


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


def _queued_action_spec_from_reserved(
    spec: _ReservedMaprunnerActionSpec,
    *,
    payload_codecs: tuple[_QueuedPayloadCodec, ...],
    reservation_kind: str | None = None,
    queue_job_kind: str | None = None,
    pre_run_guard: Callable[[Any, Mapping[str, Any], BaseModel], ActionError | None]
    | None = None,
    params_hash_fn: Callable[[ActionSpec], str] | None = None,
    finalize_kwargs: Mapping[str, Any] | None = None,
) -> _QueuedActionSpec:
    return _QueuedActionSpec(
        kind=spec.kind,
        params_model=spec.params_model,
        completed_spec=spec,
        runner_spec_fn=spec.runner_spec_fn,
        resolve_fn=spec.resolve_fn,
        precheck_fn=spec.precheck_fn,
        reservation_kind=reservation_kind
        or spec.reservation_kind.replace(
            "_idempotency_reservation",
            "_queue_reservation",
        ),
        queue_job_kind=queue_job_kind
        or spec.reservation_kind.replace(
            "_idempotency_reservation",
            "_queue_job",
        ),
        payload_codecs=payload_codecs,
        finalize_action=_queued_action_finalizer_from_reserved(
            spec,
            finalize_kwargs=finalize_kwargs,
        ),
        pre_run_guard=pre_run_guard,
        params_hash_fn=params_hash_fn or spec.params_hash_fn or _params_hash,
        confirmed_fn=spec.confirmed_fn,
        needs_confirmation_error_fn=spec.needs_confirmation_error_fn,
        cost_gate_error_fn=spec.cost_gate_error_fn,
        map_error_code=spec.map_error_code,
    )


def _queued_action_finalizer_from_reserved(
    spec: _ReservedMaprunnerActionSpec,
    *,
    finalize_kwargs: Mapping[str, Any] | None = None,
) -> Callable[..., ActionResult]:
    extra_kwargs = dict(finalize_kwargs or {})

    def finalize(
        project: Any,
        action: ActionSpec,
        params: BaseModel,
        **kwargs: Any,
    ) -> ActionResult:
        receipt_id = kwargs.get("receipt_id")
        run_id = kwargs.get("run_id")
        writer_attempt_id = kwargs.get("writer_attempt_id")
        claim_token = kwargs.get("claim_token")
        try:
            result = spec.write_fn(
                project,
                action,
                params,
                **kwargs,
                **extra_kwargs,
            )
        except StaleAttemptWriter:
            raise
        except Exception:
            # Deferred finalizers own the paid-effect/checkpoint/receipt close
            # transaction. If that transaction rolls back, keep the immutable
            # writer tuple live for reconciliation instead of closing it in a
            # second cleanup transaction.
            if isinstance(writer_attempt_id, str) and writer_attempt_id:
                raise
            if isinstance(receipt_id, str):
                OutputColumnClaimStore(project).release(
                    claim_token=_output_claim_token(receipt_id), status="failed"
                )
            raise
        if (
            isinstance(run_id, int)
            and isinstance(writer_attempt_id, str)
            and writer_attempt_id
        ):
            attempt_row = project.db.execute(
                "SELECT state FROM execution_attempts WHERE id=?",
                (writer_attempt_id,),
            ).fetchone()
            if attempt_row is not None and attempt_row["state"] == "dispatching":
                OutputColumnClaimStore(project).finish_current_writer(
                    run_id=run_id,
                    writer_attempt_id=writer_attempt_id,
                    claim_token=claim_token,
                    attempt_state="effected",
                    claim_status=(
                        "failed" if result.status == "failed" else "released"
                    ),
                )
        if isinstance(receipt_id, str):
            OutputColumnClaimStore(project).release(
                claim_token=_output_claim_token(receipt_id),
                status="released" if result.status != "failed" else "failed",
            )
        return result

    return finalize


def _terminalize_claimless_direct_failure(
    project: Any,
    *,
    project_id: str,
    action_kind: str,
    stored_receipt: StoredReceipt | None,
    error: ActionError,
    project_write_failed_message: str,
) -> ActionResult:
    if stored_receipt is None:
        return _failed_result(
            project_id=project_id,
            action_kind=action_kind,
            error=error,
        )
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
            return _failed_result(
                project_id=project_id,
                action_kind=action_kind,
                error=ActionError(
                    code="project_write_failed",
                    message=project_write_failed_message,
                    action_kind=action_kind,
                ),
            )
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
        return _failed_result(
            project_id=project_id,
            action_kind=action_kind,
            error=ActionError(
                code="project_write_failed",
                message=project_write_failed_message,
                action_kind=action_kind,
            ),
        )
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


def _running_receipt_stale_result(
    project: Any,
    existing: Any,
    *,
    params_hash: str,
    project_id: str,
    action: _ActionExecutionEnvelope,
) -> ActionResult | None:
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
        receipts.delete_if_status(receipt_id, "running", commit=False)
        project.db.commit()
    except Exception as exc:
        try:
            project.db.rollback()
        except Exception:
            pass
        return _failed_result(
            project_id=project_id,
            action_kind=action.kind,
            error=ActionError(
                code="project_write_failed",
                message=f"failed to clear stale running receipt: {exc}",
                action_kind=action.kind,
                field="idempotency_key",
            ),
        )

    return _failed_result(
        project_id=project_id,
        action_kind=action.kind,
        error=ActionError(
            code="idempotency_stale_running",
            message=(
                "stale running idempotency reservation was cleared; "
                "retry the action to continue"
            ),
            action_kind=action.kind,
            field="idempotency_key",
            details={
                "receipt_id": receipt_id,
                "created_at": str(_row_value(existing, "created_at")),
                "stale_after_seconds": RUNNING_RECEIPT_STALE_AFTER_SECONDS,
                "retryable": True,
            },
        ),
    )


def _reserved_receipt_result_from_existing(
    project: Any,
    existing: Any,
    *,
    params_hash: str,
    project_id: str,
    action: _ActionExecutionEnvelope,
    replay_error_fn: Callable[[Receipt], ActionError | None] | None = None,
    **_ignored: Any,
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
                    "idempotency_key is already reserved by a running "
                    f"{action.kind} action"
                ),
                action_kind=action.kind,
                field="idempotency_key",
                details={"receipt_id": existing["id"]},
            ),
        )
    receipt = Receipt.model_validate(json.loads(existing["body"]))
    replay_error = replay_error_fn(receipt) if replay_error_fn is not None else None
    if replay_error is not None:
        return _failed_result(
            project_id=project_id,
            action_kind=action.kind,
            error=replay_error,
        )
    return _result_from_receipt(receipt)


def _reserve_running_action_receipt(
    project: Any,
    action: _ActionExecutionEnvelope,
    *,
    params_hash: str,
    project_id: str,
    reservation_kind: str,
    result_from_existing_fn: Callable[..., ActionResult] | None = None,
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
            if replay_error_fn is not None:
                return _reserved_receipt_result_from_existing(
                    project, existing, **kwargs, replay_error_fn=replay_error_fn
                )
            assert result_from_existing_fn is not None
            return result_from_existing_fn(project, existing, **kwargs)
        ReceiptStore(project).insert_running(
            receipt,
            edition_run_context=edition_run_context,
            commit=False,
        )
        if commit:
            project.db.commit()
    except Exception:
        if commit:
            project.db.rollback()
        else:
            raise
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
        receipt = Receipt.model_validate(json.loads(existing["body"]))
        if receipt.status == "queued":
            job_id = _queued_receipt_job_id(receipt)
            if receipt.run_id is None or job_id is None:
                return _failed_result(
                    project_id=project_id,
                    action_kind=action.kind,
                    error=ActionError(
                        code="idempotency_in_progress",
                        message=(
                            "idempotency_key is already reserved by a queued "
                            f"{action.kind} action"
                        ),
                        action_kind=action.kind,
                        field="idempotency_key",
                        details={
                            "receipt_id": receipt.receipt_id,
                            "retryable": True,
                        },
                    ),
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
        if spec.completed_spec is None:
            return _failed_result(
                project_id=project_id,
                action_kind=action.kind,
                error=ActionError(
                    code="project_write_failed",
                    message="queued action spec has no completed replay handler",
                    action_kind=action.kind,
                ),
            )
        return _reserved_maprunner_result_from_existing(spec.completed_spec)(
            project,
            existing,
            params_hash=params_hash,
            project_id=project_id,
            action=action,
            params=spec.params_model.model_validate(action.params),
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
        return _failed_result(
            project_id=project_id,
            action_kind=action.kind,
            error=resolved,
        )
    try:
        local_endpoint_bindings = _local_endpoint_bindings_for_runner_spec(
            runner_spec,
            router=router,
        )
    except (LLMError, RuntimeError, ValueError) as exc:
        return _failed_result(
            project_id=project_id,
            action_kind=action.kind,
            error=ActionError(
                code="local_endpoint_unavailable",
                message=str(exc),
                action_kind=action.kind,
            ),
        )
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
                            output_precheck = _output_column_busy_error(
                                action.kind,
                                active_claim,
                                field=spec.output_claim_error_field,
                            )
                            break
            return _failed_result(
                project_id=project_id,
                action_kind=action.kind,
                error=output_precheck,
            )
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
        endpoint, _adapter, _bare_model = router.resolve_local_model(model_id)
        previous_origin = bindings.setdefault(endpoint.endpoint_id, endpoint.origin)
        if previous_origin != endpoint.origin:
            raise RuntimeError(
                f"local endpoint {endpoint.endpoint_id!r} resolved to two origins"
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
    marked[QUEUED_ACTION_RUN_MARKER_PARAM] = {
        "schema_version": QUEUED_ACTION_RUN_MARKER_SCHEMA,
        "action_kind": action_kind,
        "receipt_id": receipt_id,
        "action_id": action_id,
        "params_hash": params_hash,
    }
    return marked


def _queued_receipt_prepared_link(receipt: Receipt) -> Mapping[str, Any] | None:
    if type(receipt.run_id) is not int:
        return None
    matches = [
        item.ref
        for item in receipt.evidence
        if item.ref.get("kind") == QUEUED_ACTION_RUN_PREPARED_EVIDENCE_KIND
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
            return _failed_result(
                project_id=project_id,
                action_kind=action.kind,
                error=claim_error,
            )
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
            return _failed_result(
                project_id=project_id,
                action_kind=action.kind,
                error=post_claim_precheck,
            )
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
        return _failed_result(
            project_id=project_id,
            action_kind=action.kind,
            error=ActionError(
                code="project_write_failed",
                message="project write failed",
                action_kind=action.kind,
            ),
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
    row = ReceiptStore(project).find_by_id(receipt_id)
    if row is None:
        raise RuntimeError(
            f"queued receipt {receipt_id!r} disappeared during preparation"
        )
    receipt = Receipt.model_validate(json.loads(row["body"]))
    evidence = [
        item
        for item in receipt.evidence
        if item.ref.get("kind") != QUEUED_ACTION_RUN_PREPARED_EVIDENCE_KIND
    ]
    prepared_ref: dict[str, Any] = {
        "kind": QUEUED_ACTION_RUN_PREPARED_EVIDENCE_KIND,
        "queue_kind": "project.run",
        "run_id": run_id,
        "action_queue_kind": spec.queue_job_kind,
    }
    if attempt_id is not None:
        prepared_ref["attempt_id"] = attempt_id
    if reservation_snapshot is not None:
        prepared_ref["reservation_snapshot"] = dict(reservation_snapshot)
    evidence.append(ReceiptEvidence(ref=prepared_ref))
    receipt = receipt.model_copy(update={"run_id": run_id, "evidence": evidence})
    try:
        if commit:
            project.db.execute("BEGIN IMMEDIATE")
        elif not project.db.in_transaction:
            raise RuntimeError(
                "_mark_queued_action_run_prepared(commit=False) requires a "
                "caller-owned project transaction"
            )
        updated = ReceiptStore(project).update_body_status(
            receipt,
            require_status="queued",
            commit=False,
        )
        if not updated:
            raise RuntimeError(f"queued receipt {receipt_id!r} is no longer preparable")
        OutputColumnClaimStore(project).bind_to_run(
            claim_token=_output_claim_token(receipt_id),
            run_id=run_id,
            expected_output_names=[str(field["name"]) for field in output_fields],
            commit=False,
        )
        from frisket.engine.runner.result_generations import (
            declare_bound_run_outputs,
        )

        declare_bound_run_outputs(
            project,
            run_id=run_id,
            output_fields=output_fields,
            program=program,
            claim_token=_output_claim_token(receipt_id),
            defer_publication=getattr(program, "defer_generation_seal", False) is True,
        )
        _hide_deferred_created_columns(project, run_id, program)
        if commit:
            project.db.commit()
    except Exception:
        if commit:
            project.db.rollback()
        raise


def _hide_deferred_created_columns(
    project: Any, run_id: int, program: Recipe | None
) -> None:
    if getattr(program, "defer_generation_seal", False) is not True:
        return
    from frisket.engine.runner.preparation import _created_output_column_ids

    row = project.db.execute("SELECT op_id FROM runs WHERE id=?", (run_id,)).fetchone()
    created = _created_output_column_ids(project, int(row["op_id"]))
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

    row = ReceiptStore(project).find_by_id(receipt_id)
    if row is None:
        raise RuntimeError(
            f"running receipt {receipt_id!r} disappeared during preparation"
        )
    receipt = Receipt.model_validate(json.loads(row["body"]))
    evidence = [
        item
        for item in receipt.evidence
        if item.ref.get("kind") != QUEUED_ACTION_RUN_PREPARED_EVIDENCE_KIND
    ]
    if attempt_id is not None:
        evidence.append(
            ReceiptEvidence(
                ref={
                    "kind": QUEUED_ACTION_RUN_PREPARED_EVIDENCE_KIND,
                    "queue_kind": "direct",
                    "run_id": run_id,
                    "attempt_id": attempt_id,
                }
            )
        )
    prepared_receipt = receipt.model_copy(
        update={"run_id": run_id, "evidence": evidence}
    )
    try:
        if commit:
            project.db.execute("BEGIN IMMEDIATE")
        elif not project.db.in_transaction:
            raise RuntimeError(
                "_mark_running_action_run_prepared(commit=False) requires a "
                "caller-owned project transaction"
            )
        if not ReceiptStore(project).update_body_status(
            prepared_receipt,
            require_status="running",
            commit=False,
        ):
            raise RuntimeError(
                f"running receipt {receipt_id!r} is no longer preparable"
            )
        OutputColumnClaimStore(project).bind_to_run(
            claim_token=_output_claim_token(receipt_id),
            run_id=run_id,
            expected_output_names=[str(field["name"]) for field in output_fields],
            commit=False,
        )
        from frisket.engine.runner.result_generations import (
            declare_bound_run_outputs,
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
        project.db.execute(
            "UPDATE runs SET edition_run_context="
            "(SELECT edition_run_context FROM receipts WHERE id=?) "
            "WHERE id=? AND edition_run_context IS NULL",
            (receipt_id, run_id),
        )
        if commit:
            project.db.commit()
    except Exception:
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


def _delete_reserved_action_receipt(
    project: Any,
    receipt_id: str,
    *,
    action_kind: str,
    log_missed_delete: bool = True,
) -> None:
    try:
        project.db.execute("BEGIN IMMEDIATE")
        receipts = ReceiptStore(project)
        deleted = receipts.delete_if_status(receipt_id, "running", commit=False)
        if log_missed_delete and not deleted:
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
    reservation_lost_message: str,
    update_failed_message: str,
    result_from_existing_fn: Callable[..., ActionResult] | None = None,
    replay_error_fn: Callable[[Receipt], ActionError | None] | None = None,
    result_from_existing_kwargs: dict[str, Any] | None = None,
    require_running_status: bool = True,
    before_commit: Callable[[], None] | None = None,
    commit: bool = True,
) -> ActionResult | None:
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
            return _failed_result(
                project_id=project_id,
                action_kind=action.kind,
                error=ActionError(
                    code="project_write_failed",
                    message=reservation_lost_message,
                    action_kind=action.kind,
                ),
            )
        if existing["id"] != receipt.receipt_id:
            if commit:
                project.db.rollback()
            kwargs = dict(result_from_existing_kwargs or {})
            if replay_error_fn is not None:
                return _reserved_receipt_result_from_existing(
                    project,
                    existing,
                    params_hash=params_hash,
                    project_id=project_id,
                    action=action,
                    replay_error_fn=replay_error_fn,
                    **kwargs,
                )
            assert result_from_existing_fn is not None
            return result_from_existing_fn(
                project,
                existing,
                params_hash=params_hash,
                project_id=project_id,
                action=action,
                **kwargs,
            )
        if existing["params_hash"] != params_hash:
            if commit:
                project.db.rollback()
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
        updated = ReceiptStore(project).update_body_status(
            receipt,
            require_status="running" if require_running_status else None,
            commit=False,
        )
        if not updated:
            if commit:
                project.db.rollback()
            return _failed_result(
                project_id=project_id,
                action_kind=action.kind,
                error=ActionError(
                    code="project_write_failed",
                    message=update_failed_message,
                    action_kind=action.kind,
                ),
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
        return _failed_result(
            project_id=project_id,
            action_kind=action.kind,
            error=ActionError(
                code="project_write_failed",
                message="project write failed",
                action_kind=action.kind,
            ),
        )
    return None


def _finalize_direct_reserved_action_receipt(
    project: Any,
    action: _ActionExecutionEnvelope,
    *,
    params_hash: str,
    project_id: str,
    receipt: Receipt,
    finalize: _DirectActionFinalizeMetadata,
    before_commit: Callable[[], None] | None = None,
    commit: bool = True,
) -> ActionResult | None:
    return _finalize_reserved_action_receipt(
        project,
        action,
        params_hash=params_hash,
        project_id=project_id,
        receipt=receipt,
        reservation_lost_message=finalize.reservation_lost_message,
        update_failed_message=finalize.update_failed_message,
        result_from_existing_fn=finalize.result_from_existing_fn,
        result_from_existing_kwargs=finalize.result_from_existing_kwargs,
        require_running_status=finalize.require_running_status,
        before_commit=before_commit,
        commit=commit,
    )


def _reserved_maprunner_result_from_existing(
    spec: _ReservedMaprunnerActionSpec,
) -> Callable[..., ActionResult]:
    def result_from_existing(
        project: Any,
        existing: Any,
        *,
        params_hash: str,
        project_id: str,
        action: ActionSpec,
        params: Any | None = None,
    ) -> ActionResult:
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
            log_missed_delete=spec.log_missed_delete,
        )

    return delete
