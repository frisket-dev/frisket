from __future__ import annotations

import json
import logging
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

from frisket.contracts.action import (
    ActionError,
    ActionIdentity,
    ActionResult,
    ActionSpec,
    Receipt,
    ReceiptEvidence,
    ReceiptIO,
)
from frisket.project_opener import ProjectOpener
from frisket.engine.store.output_claims import (
    ClaimLeaseRenewalFailed,
    OutputColumnClaimStore,
)
from frisket.engine.store.receipts import (
    FINISHED_RECEIPT_STATUSES,
    ReceiptStatus,
    ReceiptStore,
)
from frisket.execution.attempt import StaleAttemptWriter
from frisket.engine.executor.action_specs import declared_queued_action_job_kinds


ACTION_JOB_SCHEMA_VERSION = "frisket.action_job.v1"
ACTION_JOB_ENQUEUED_EVIDENCE_KIND = "queued_action_job_enqueued"
LOG = logging.getLogger("frisket.worker")


class ActionJobTerminalizationError(RuntimeError):
    """A queued action-job receipt could not be driven to a terminal status.

    Raised when the receipt is missing or the status update did not land, so a
    worker never reports a terminal result without a durable terminal receipt.
    """


class ActionJobRetryableFailure(RuntimeError):
    """Raised so the queue, not the receipt terminalizer, owns retry backoff."""

    def __init__(self, error: ActionError):
        self.error = error
        super().__init__(f"{error.code}: {error.message}")


@dataclass(frozen=True)
class ActionJobEnvelope:
    """Deterministic, JSON-only payload for a generic ``action.run`` job."""

    action_kind: str
    action_id: str
    receipt_id: str
    params_hash: str
    idempotency_key: str
    project_id: str
    # The validated action spec body, so the worker can validate/resolve at worker
    # time (resolve_phase="worker") or replay/idempotency-check at launch-time.
    action: Mapping[str, Any] = field(default_factory=dict)
    actor: str | None = None
    capabilities: tuple[str, ...] = ()
    resolved_snapshot: Mapping[str, Any] | None = None
    # "launch": resolved facts frozen at enqueue; "worker": resolve at worker time.
    resolve_phase: Literal["launch", "worker"] = "launch"
    schema_version: Literal["frisket.action_job.v1"] = ACTION_JOB_SCHEMA_VERSION

    _REQUIRED_KEYS = (
        "action_kind",
        "action_id",
        "receipt_id",
        "params_hash",
        "idempotency_key",
        "project_id",
        "action",
    )

    def to_json(self) -> dict[str, Any]:
        payload = {
            "schema_version": self.schema_version,
            "action_kind": self.action_kind,
            "action_id": self.action_id,
            "receipt_id": self.receipt_id,
            "params_hash": self.params_hash,
            "idempotency_key": self.idempotency_key,
            "project_id": self.project_id,
            "action": dict(self.action),
            "actor": self.actor,
            "capabilities": list(self.capabilities),
            "resolved_snapshot": (
                dict(self.resolved_snapshot)
                if self.resolved_snapshot is not None
                else None
            ),
            "resolve_phase": self.resolve_phase,
        }
        # Strict JSON round-trip: fail fast on non-serializable / non-redacted /
        # non-standard-float values (sqlite rows, Path, bytes, NaN/Infinity).
        return json.loads(json.dumps(payload, sort_keys=True, allow_nan=False))

    @classmethod
    def from_json(cls, payload: Mapping[str, Any]) -> ActionJobEnvelope:
        # Fail closed: a malformed envelope must not silently produce a runnable
        # job.
        if payload.get("schema_version") != ACTION_JOB_SCHEMA_VERSION:
            raise ValueError(
                "unexpected action job schema_version: "
                f"{payload.get('schema_version')!r}"
            )
        resolve_phase = payload.get("resolve_phase")
        if resolve_phase not in ("launch", "worker"):
            raise ValueError(f"invalid action job resolve_phase: {resolve_phase!r}")
        missing = [key for key in cls._REQUIRED_KEYS if key not in payload]
        if missing:
            raise KeyError(f"action job envelope missing fields: {missing}")
        return cls(
            action_kind=str(payload["action_kind"]),
            action_id=str(payload["action_id"]),
            receipt_id=str(payload["receipt_id"]),
            params_hash=str(payload["params_hash"]),
            idempotency_key=str(payload["idempotency_key"]),
            project_id=str(payload["project_id"]),
            action=dict(payload["action"]),
            actor=payload.get("actor"),
            capabilities=tuple(payload.get("capabilities") or ()),
            resolved_snapshot=payload.get("resolved_snapshot"),
            resolve_phase=resolve_phase,
        )


@dataclass(frozen=True)
class ActionJobResultSummary:
    """Bounded, JSON-only result summary surfaced on the job projection."""

    status: str
    counts: Mapping[str, int] = field(default_factory=dict)
    error_code: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "counts": dict(self.counts),
            "error_code": self.error_code,
        }


def action_job_result_payload(result: ActionResult) -> dict[str, Any]:
    """Bounded result summary stored on the queue job row.

    The full receipt remains the durable action result. Queue rows expose only the
    fields users need for job list/detail polling and the embedding refresh counters
    already surfaced by the old bespoke refresh worker.
    """

    out: dict[str, Any] = {
        "status": result.status,
        "action_kind": result.action.kind,
        "receipt_id": result.receipt_id,
    }
    if result.errors:
        err = result.errors[0]
        out["error_code"] = err.code
        out["error"] = err.message
        out["blocked"] = True
    for output in result.outputs:
        ref = output.ref or {}
        if ref.get("kind") == "embedding_index_refresh":
            for key in (
                "index_id",
                "space_id",
                "mode",
                "total_items",
                "refreshed",
                "skipped_current",
                "ready_items",
                "truncated_items",
                "max_input_tokens",
            ):
                if key in ref:
                    out[key] = ref[key]
            break
    return json.loads(json.dumps(out, sort_keys=True, allow_nan=False))


def _is_retryable_action_error(error: ActionError) -> bool:
    details = error.details
    return isinstance(details, Mapping) and details.get("retryable") is True


_TERMINALIZE_SOURCE_STATUSES: dict[ReceiptStatus, frozenset[str]] = {
    status: frozenset({"queued", "running"}) for status in FINISHED_RECEIPT_STATUSES
}


def _terminalize_action_job_receipt(
    project: Any,
    *,
    project_id: str,
    receipt_id: str,
    action_kind: str,
    status: ReceiptStatus,
    action_id: str | None = None,
    error: ActionError | None = None,
    job_id: int | None = None,
) -> ActionResult:
    """Drive a reserved ``queued_action_job`` receipt to a terminal status.

    Unlike the project-run path the run_id stays None. Releases any output claim
    held under the receipt. Idempotent over already-terminal receipts.
    """

    receipts = ReceiptStore(project)
    stored = receipts.find_by_id(receipt_id) if receipt_id else None
    if stored is None:
        # Fail closed: a terminalizer must never report a terminal result for a
        # receipt that does not exist — that would mask a reserved receipt that is
        # still (or never became) durable.
        raise ActionJobTerminalizationError(
            f"cannot terminalize {action_kind}: receipt {receipt_id!r} not found"
        )
    receipt = stored.parsed()
    receipt_id = receipt.receipt_id
    action_kind = receipt.action_kind
    if receipt.status in FINISHED_RECEIPT_STATUSES:
        # Terminal receipts are monotonic: cancellation/failure may not be
        # weakened by a late completion from an already-running handler.
        return _result_from_existing_action_job_receipt(
            receipt,
            job_id=job_id,
        )
    try:
        project.db.execute("BEGIN IMMEDIATE")
        errors = [error] if error is not None else []
        updated = receipt.model_copy(
            update={
                "status": status,
                "errors": errors,
            }
        )
        landed = receipts.update_if_status_in(
            updated,
            _TERMINALIZE_SOURCE_STATUSES[status],
            commit=False,
        )
        if not landed:
            project.db.rollback()
            raise ActionJobTerminalizationError(
                f"failed to terminalize {action_kind} receipt {receipt_id!r} to "
                f"{status!r} (current status {receipt.status!r})"
            )
        OutputColumnClaimStore(project).release_for_receipt(
            receipt_id=receipt_id,
            status="released" if status in {"completed", "partial"} else status,
            commit=False,
        )
        project.db.commit()
    except ActionJobTerminalizationError:
        raise
    except Exception:
        project.db.rollback()
        raise
    return _result_from_existing_action_job_receipt(updated, job_id=job_id)


def action_job_success_result(
    project: Any,
    *,
    project_id: str,
    receipt_id: str,
    action_kind: str,
    status: Literal["completed", "partial"] = "completed",
    job_id: int | None = None,
) -> ActionResult:
    return _terminalize_action_job_receipt(
        project,
        project_id=project_id,
        receipt_id=receipt_id,
        action_kind=action_kind,
        status=status,
        job_id=job_id,
    )


def action_job_failure_result(
    project: Any,
    *,
    project_id: str,
    receipt_id: str,
    action_kind: str,
    error: ActionError,
    job_id: int | None = None,
) -> ActionResult:
    return _terminalize_action_job_receipt(
        project,
        project_id=project_id,
        receipt_id=receipt_id,
        action_kind=action_kind,
        status="failed",
        error=error,
        job_id=job_id,
    )


def action_job_missing_handler_result(
    project: Any,
    *,
    project_id: str,
    receipt_id: str,
    action_kind: str,
    job_id: int | None = None,
) -> ActionResult:
    return action_job_failure_result(
        project,
        project_id=project_id,
        receipt_id=receipt_id,
        action_kind=action_kind,
        error=ActionError(
            code="action_handler_missing",
            message=f"no queued action.run handler registered for {action_kind}",
            action_kind=action_kind,
        ),
        job_id=job_id,
    )


def action_job_cancelled_result(
    project: Any,
    *,
    project_id: str,
    receipt_id: str,
    action_kind: str,
    job_id: int | None = None,
) -> ActionResult:
    return _terminalize_action_job_receipt(
        project,
        project_id=project_id,
        receipt_id=receipt_id,
        action_kind=action_kind,
        status="cancelled",
        job_id=job_id,
    )


def action_job_exhausted_lease_result(
    project: Any,
    *,
    project_id: str,
    receipt_id: str,
    action_kind: str,
    job_id: int | None = None,
) -> ActionResult:
    return action_job_failure_result(
        project,
        project_id=project_id,
        receipt_id=receipt_id,
        action_kind=action_kind,
        error=ActionError(
            code="action_job_lease_exhausted",
            message=f"{action_kind} action.run job exhausted its lease/retries",
            action_kind=action_kind,
        ),
        job_id=job_id,
    )


def action_job_retry_exhausted_result(
    project: Any,
    *,
    project_id: str,
    receipt_id: str,
    action_kind: str,
    job_id: int | None = None,
) -> ActionResult:
    return action_job_failure_result(
        project,
        project_id=project_id,
        receipt_id=receipt_id,
        action_kind=action_kind,
        error=ActionError(
            code="action_job_retry_exhausted",
            message=f"{action_kind} action.run job exhausted its queue attempts",
            action_kind=action_kind,
        ),
        job_id=job_id,
    )


def _mark_action_job_receipt_running(
    project: Any,
    *,
    project_id: str,
    receipt_id: str,
    job_id: int | None,
) -> ActionResult | None:
    """Move a queued action-job receipt to running before provider work.

    Returns a terminal ActionResult when another path already terminalized the
    receipt; otherwise returns None and the caller may proceed with execution.
    """

    receipts = ReceiptStore(project)
    stored = receipts.find_by_id(receipt_id)
    if stored is None:
        return None
    receipt = stored.parsed()
    if receipt.status in FINISHED_RECEIPT_STATUSES:
        return _result_from_existing_action_job_receipt(
            receipt,
            job_id=job_id,
        )
    if receipt.status != "queued":
        return None
    running = receipt.model_copy(update={"status": "running"})
    try:
        project.db.execute("BEGIN IMMEDIATE")
        updated = receipts.update_body_status(
            running,
            require_status="queued",
            commit=False,
        )
        if not updated:
            project.db.rollback()
            refreshed = receipts.find_by_id(receipt_id)
            if refreshed is not None:
                refreshed_receipt = refreshed.parsed()
                if refreshed_receipt.status in FINISHED_RECEIPT_STATUSES:
                    return _result_from_existing_action_job_receipt(
                        refreshed_receipt,
                        job_id=job_id,
                    )
            return None
        project.db.commit()
    except Exception:
        project.db.rollback()
        raise
    return None


def _mark_action_job_receipt_queued_for_retry(
    project: Any,
    *,
    receipt_id: str,
) -> None:
    receipts = ReceiptStore(project)
    stored = receipts.find_by_id(receipt_id)
    if stored is None:
        return
    receipt = stored.parsed()
    if receipt.status != "running":
        return
    queued = receipt.model_copy(update={"status": "queued"})
    try:
        project.db.execute("BEGIN IMMEDIATE")
        receipts.update_body_status(
            queued,
            require_status="running",
            commit=False,
        )
        project.db.commit()
    except Exception:
        project.db.rollback()
        raise


# --- receipt reservation / enqueue marking -----------------------------------


def _action_job_receipt_job_id(receipt: Receipt) -> int | None:
    for item in receipt.evidence:
        ref = item.ref
        if ref.get("kind") != ACTION_JOB_ENQUEUED_EVIDENCE_KIND:
            continue
        job_id = ref.get("job_id")
        if isinstance(job_id, int) and not isinstance(job_id, bool):
            return job_id
    return None


def _result_from_existing_action_job_receipt(
    receipt: Receipt,
    *,
    job_id: int | None,
) -> ActionResult:
    from frisket.engine.executor.action_receipts import _result_from_receipt

    result = _result_from_receipt(receipt)
    if job_id is not None:
        result = result.model_copy(update={"job_id": job_id})
    return result


def _existing_action_job_reservation_or_result(
    existing: Any,
    *,
    action: ActionSpec,
    project_id: str,
    params_hash: str,
) -> dict[str, str] | ActionResult:
    """Project an exact replay or recover an unpublished prepared intent."""
    from frisket.engine.executor.action_support import _failed_result

    if existing.params_hash != params_hash:
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
    receipt = existing.parsed()
    job_id = _action_job_receipt_job_id(receipt)
    if (
        action.kind in declared_queued_action_job_kinds()
        and receipt.status not in FINISHED_RECEIPT_STATUSES
        and job_id is None
    ):
        return {
            "action_id": receipt.action_id,
            "receipt_id": receipt.receipt_id,
            "params_hash": params_hash,
        }
    return _result_from_existing_action_job_receipt(receipt, job_id=job_id)


def reserve_queued_action_job_receipt(
    project: Any,
    action: ActionSpec,
    *,
    project_id: str,
    params_hash: str | None = None,
    edition_run_context: Mapping[str, Any] | None = None,
    prepared_input: ReceiptIO | None = None,
) -> dict[str, str] | ActionResult:
    """Reserve the runless receipt used by a generic ``action.run`` job.

    ``edition_run_context`` is an opaque, write-once edition snapshot. It is
    stored beside the receipt rather than inside its public body, exactly as a
    queued project run stores the same snapshot on ``runs``.
    """

    from frisket.engine.executor.action_support import (
        _failed_result,
        _new_id,
        _params_hash,
    )

    params_hash = params_hash or _params_hash(action)
    encoded_edition_context = (
        None
        if edition_run_context is None
        else json.dumps(
            dict(edition_run_context),
            sort_keys=True,
            allow_nan=False,
        )
    )
    existing = (
        ReceiptStore(project).find_by_idempotency_key(action.idempotency_key)
        if action.idempotency_key
        else None
    )
    if existing is not None:
        outcome = _existing_action_job_reservation_or_result(
            existing,
            action=action,
            project_id=project_id,
            params_hash=params_hash,
        )
        if prepared_input is None or isinstance(outcome, ActionResult):
            return outcome

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
                ref={"kind": "queued_action_job", "params_hash": params_hash},
            ),
            *([prepared_input] if prepared_input is not None else []),
        ],
    )
    try:
        project.db.execute("BEGIN IMMEDIATE")
        existing = (
            ReceiptStore(project).find_by_idempotency_key(action.idempotency_key)
            if action.idempotency_key
            else None
        )
        if existing is not None:
            outcome = _existing_action_job_reservation_or_result(
                existing,
                action=action,
                project_id=project_id,
                params_hash=params_hash,
            )
            if prepared_input is not None and isinstance(outcome, dict):
                previous = existing.parsed()
                pins = [
                    item for item in previous.inputs if item.name == prepared_input.name
                ]
                if pins and pins != [prepared_input]:
                    project.db.rollback()
                    return _failed_result(
                        project_id=project_id,
                        action_kind=action.kind,
                        error=ActionError(
                            code="idempotency_conflict",
                            message="Prepared operation differs from its reserved intent.",
                            action_kind=action.kind,
                            field="idempotency_key",
                        ),
                    )
                if not pins:
                    pinned = ReceiptStore(project).update_body_status(
                        previous.model_copy(
                            update={"inputs": [*previous.inputs, prepared_input]}
                        ),
                        require_status=previous.status,
                        commit=False,
                    )
                    if not pinned:
                        raise RuntimeError("queued operation reservation changed")
                project.db.commit()
            else:
                project.db.rollback()
            return outcome
        ReceiptStore(project).insert_queued(receipt, commit=False)
        if encoded_edition_context is not None:
            project.db.execute(
                "UPDATE receipts SET edition_run_context=? WHERE id=?",
                (encoded_edition_context, receipt_id),
            )
        project.db.commit()
    except BaseException as exc:
        project.db.rollback()
        if not isinstance(exc, Exception):
            raise
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
    }


def reserve_typed_action_job(
    project: Any,
    project_id: str,
    bound: Any,
    *,
    edition_run_context: Mapping[str, Any] | None = None,
) -> ActionJobEnvelope | ActionResult:
    """Reserve canonical intent without invoking a worker-resolved callable."""
    from frisket.actions.core import _ProjectAction, CreateSheet
    from frisket.actions.transcript_types import TranscriptReader
    from frisket.actions.temporal_types import TemporalMediaReader
    from frisket.engine.executor.action_inventory import _TypedProjectEnvelope
    from frisket.engine.executor.map_rows_action import typed_request_hash

    terminal = bound.action.definition.run
    if not (
        isinstance(terminal, _ProjectAction)
        or isinstance(terminal, CreateSheet)
        and any(
            cap in terminal.capabilities
            for cap in (TranscriptReader, TemporalMediaReader)
        )
    ):
        raise TypeError(
            "typed action jobs require a supported callable or transcript table"
        )
    request = bound.request
    identity = _TypedProjectEnvelope(
        kind=request.action_id,
        idempotency_key=request.idempotency_key,
        params=bound.params.model_dump(mode="json"),
    )
    reservation = reserve_queued_action_job_receipt(
        project,
        identity,
        project_id=project_id,
        params_hash=typed_request_hash(bound),
        edition_run_context=edition_run_context,
    )
    if isinstance(reservation, ActionResult):
        return reservation
    return ActionJobEnvelope(
        action_kind=request.action_id,
        action_id=reservation["action_id"],
        receipt_id=reservation["receipt_id"],
        params_hash=reservation["params_hash"],
        idempotency_key=request.idempotency_key,
        project_id=project_id,
        action=request.model_dump(mode="json", exclude_none=True),
        resolve_phase="worker",
    )


def bind_typed_action_job(
    envelope: ActionJobEnvelope, *, project: Any = None
) -> Any | ActionResult:
    """Revalidate queued intent; queue fields never authorize capability arguments."""
    from frisket.actions.core import _ProjectAction, CreateSheet
    from frisket.actions.transcript_types import TranscriptReader
    from frisket.actions.temporal_types import TemporalMediaReader
    from frisket.actions.system import typed_action_for_request
    from frisket.engine.executor.map_rows_action import typed_request_hash

    try:
        bound = None
        if project is not None:
            from frisket.actions.types import ActionRequest
            from frisket.authoring.workbench.installed_actions import (
                bind_installed_action,
            )

            bound = bind_installed_action(
                project, ActionRequest.model_validate(envelope.action)
            )
        if bound is None:
            bound = typed_action_for_request(dict(envelope.action))
        terminal = bound.action.definition.run
        if (
            bound.request.action_id != envelope.action_kind
            or bound.request.idempotency_key != envelope.idempotency_key
            or not (
                isinstance(terminal, _ProjectAction)
                or isinstance(terminal, CreateSheet)
                and any(
                    cap in terminal.capabilities
                    for cap in (TranscriptReader, TemporalMediaReader)
                )
            )
            or envelope.resolve_phase != "worker"
        ):
            raise ValueError("action job identity does not match its canonical request")
        if typed_request_hash(bound) != envelope.params_hash:
            raise ValueError(
                "action job request hash does not match its canonical request"
            )
        return bound
    except (KeyError, TypeError, ValueError) as exc:
        from frisket.engine.executor.action_support import _failed_result

        return _failed_result(
            project_id=envelope.project_id,
            action_kind=envelope.action_kind,
            error=ActionError(
                code="invalid_action_request",
                message=str(exc),
                action_kind=envelope.action_kind,
                field="action",
            ),
        )


def probe_queued_action_job_receipt(
    project: Any,
    action: ActionSpec,
    *,
    project_id: str,
    params_hash: str,
) -> ActionResult | None:
    """Read an exact published replay/conflict before fresh admission.

    An unpublished prepared intent deliberately returns ``None``: it has no
    queue job and is not authority to bypass current dependency/confirmation
    checks. The later reservation call recovers that same receipt after those
    checks. This function never creates or updates a receipt.
    """

    existing = (
        ReceiptStore(project).find_by_idempotency_key(action.idempotency_key)
        if action.idempotency_key
        else None
    )
    if existing is None:
        return None
    outcome = _existing_action_job_reservation_or_result(
        existing,
        action=action,
        project_id=project_id,
        params_hash=params_hash,
    )
    return None if isinstance(outcome, dict) else outcome


def mark_action_job_enqueued(
    project: Any,
    *,
    receipt_id: str,
    job_id: int,
    job_kind: str,
) -> None:
    receipts = ReceiptStore(project)
    try:
        project.db.execute("BEGIN IMMEDIATE")
        row = receipts.find_by_id(receipt_id)
        if row is None:
            project.db.rollback()
            raise ActionJobTerminalizationError(
                f"cannot mark action job receipt {receipt_id!r} enqueued: "
                "receipt missing"
            )
        receipt = row.parsed()
        if receipt.status not in {"queued", "running"}:
            project.db.rollback()
            raise ActionJobTerminalizationError(
                f"failed to mark action job receipt {receipt_id!r} enqueued"
            )
        evidence = [
            item
            for item in receipt.evidence
            if item.ref.get("kind") != ACTION_JOB_ENQUEUED_EVIDENCE_KIND
        ]
        evidence.append(
            ReceiptEvidence(
                ref={
                    "kind": ACTION_JOB_ENQUEUED_EVIDENCE_KIND,
                    "queue_kind": job_kind,
                    "job_id": job_id,
                }
            )
        )
        updated_receipt = receipt.model_copy(update={"evidence": evidence})
        updated = receipts.update_body_status(
            updated_receipt,
            require_status=receipt.status,
            commit=False,
        )
        if not updated:
            project.db.rollback()
            raise ActionJobTerminalizationError(
                f"failed to mark action job receipt {receipt_id!r} enqueued"
            )
        project.db.commit()
    except ActionJobTerminalizationError:
        raise
    except Exception:
        project.db.rollback()
        raise


# --- generic action.run executor dispatch -----------------------------------
#
# A queued_action_job action registers an executor keyed by its action kind on
# the composition's HandlerRegistry (``register_action_executor``) — one
# registration surface, no module-global registry. The generic action.run
# worker passes the registry lookup into ``run_action_run_job``; the worker
# owns terminalization, so the executor just resolves+computes and returns its
# ActionResult (or raises).
ActionJobExecutor = Callable[[Any, "ActionJobEnvelope"], ActionResult]
ActionJobExecutorLookup = Callable[[str], ActionJobExecutor | None]


# --- launch path -----------------------------------------------------------


def merge_queue_payload(
    authoritative: Mapping[str, Any],
    *metadata_layers: Mapping[str, Any] | None,
    protected_keys: Collection[str] = (),
) -> dict[str, Any]:
    """Add queue metadata without permitting an ownership split.

    Flat queue fields feed indexes, terminal hooks, and hosted routing while
    the nested action/spec envelope feeds the worker.  A spread-last merge can
    therefore make those consumers act on different projects, runs, actions,
    or receipts.  Treat collisions as a programmer error even when the values
    happen to match: an identity field must have one owner, not two sources
    whose agreement is merely conventional.
    """

    protected = set(authoritative) | set(protected_keys)
    payload = dict(authoritative)
    for metadata in metadata_layers:
        layer = dict(metadata or {})
        collisions = sorted(protected.intersection(layer))
        if collisions:
            raise ValueError(
                "queue metadata cannot override authoritative field(s): "
                + ", ".join(collisions)
            )
        payload.update(layer)
    return payload


def launch_queued_action_job(
    *,
    envelope: ActionJobEnvelope,
    queue: Any,
    job_kind: str,
    org_id: str | None = None,
    max_attempts: int = 1,
    payload_extra: Mapping[str, Any] | None = None,
    project: Any | None = None,
) -> ActionResult:
    """Enqueue a generic action.run job and return its ``queued`` ActionResult.

    The receipt must already be reserved (run_id=None) and its id carried on the
    envelope; this enqueues the job referencing it.
    """

    authoritative_payload = {
        "action_kind": envelope.action_kind,
        "project_id": envelope.project_id,
        "v1_receipt_id": envelope.receipt_id,
        "v1_action_id": envelope.action_id,
        "v1_params_hash": envelope.params_hash,
        "action_job": envelope.to_json(),
    }
    # Existing launch consumers that already own a publication key (notably
    # embedding refresh's scheduler window) retain it. A declared first-party
    # runless action job without one uses its durable Project receipt as
    # publication identity. Dynamic plugin publication remains unchanged.
    if (
        envelope.action_kind in declared_queued_action_job_kinds()
        and "dedupe_key" not in (payload_extra or {})
    ):
        authoritative_payload["dedupe_key"] = envelope.receipt_id
    if org_id is not None:
        authoritative_payload["org_id"] = org_id
    payload = merge_queue_payload(authoritative_payload, payload_extra)
    job_id = queue.enqueue(job_kind, payload, max_attempts=max_attempts)
    if project is not None:
        mark_action_job_enqueued(
            project,
            receipt_id=envelope.receipt_id,
            job_id=job_id,
            job_kind=job_kind,
        )
    return ActionResult(
        action={
            "kind": envelope.action_kind,
            "action_id": envelope.action_id,
        },
        status="queued",
        project_id=envelope.project_id,
        run_id=None,
        job_id=job_id,
        receipt_id=envelope.receipt_id,
    )


# --- worker path -----------------------------------------------------------


def action_run_envelope_from_payload(
    payload: Mapping[str, Any],
) -> ActionJobEnvelope | None:
    body = payload.get("action_job")
    if not isinstance(body, Mapping):
        return None
    return ActionJobEnvelope.from_json(body)


def run_action_run_job(
    project: Any,
    payload: Mapping[str, Any],
    *,
    executor_lookup: ActionJobExecutorLookup | None = None,
) -> ActionResult:
    """Execute one generic action.run job and terminalize its receipt.

    ``executor_lookup`` is the composition's registered-executor lookup
    (``HandlerRegistry.action_executor``). Admitted installed table actions use
    the same table executor without global registration. The worker owns
    terminalization: on a missing
    executor, an executor exception, or a non-terminal executor result, the
    reserved receipt is driven terminal so it can never stay running forever.
    """

    envelope = action_run_envelope_from_payload(payload)
    if envelope is None:
        raise ValueError("action.run payload missing a valid action_job envelope")
    project_id = envelope.project_id
    receipt_id = envelope.receipt_id
    action_kind = envelope.action_kind
    job_id = payload.get("job_id") if isinstance(payload, dict) else None
    if isinstance(job_id, int) and isinstance(envelope.resolved_snapshot, Mapping):
        envelope = replace(
            envelope,
            resolved_snapshot={**dict(envelope.resolved_snapshot), "job_id": job_id},
        )

    # Replay/idempotency: if the receipt is already terminal, return it as-is.
    already_terminal = _mark_action_job_receipt_running(
        project,
        project_id=project_id,
        receipt_id=receipt_id,
        job_id=job_id,
    )
    if already_terminal is not None:
        return already_terminal

    executor = executor_lookup(action_kind) if executor_lookup is not None else None
    try:
        if executor is None:
            from frisket.actions.core import CreateSheet
            from frisket.authoring.workbench.installed_actions import (
                resolve_installed_action,
            )

            installed = resolve_installed_action(project, action_kind)
            if installed is not None and isinstance(
                installed[0].definition.run, CreateSheet
            ):
                from frisket.engine.executor.table_action import (
                    run_typed_table_action_job,
                )

                executor = run_typed_table_action_job
        if executor is None:
            return action_job_missing_handler_result(
                project,
                project_id=project_id,
                receipt_id=receipt_id,
                action_kind=action_kind,
                job_id=job_id,
            )
        result = executor(project, envelope)
    except ClaimLeaseRenewalFailed:
        # The effect batch and renewal rolled back together. Preserve the
        # running receipt so the queue can retry without buying work again.
        raise
    except StaleAttemptWriter as exc:
        return ActionResult(
            action=ActionIdentity(
                kind=action_kind,
                action_id=envelope.action_id,
            ),
            status="failed",
            project_id=project_id,
            errors=[
                ActionError(
                    code=exc.code,
                    message=str(exc),
                    action_kind=action_kind,
                )
            ],
        )
    except Exception:  # noqa: BLE001 — the receipt must terminalize
        return action_job_failure_result(
            project,
            project_id=project_id,
            receipt_id=receipt_id,
            action_kind=action_kind,
            error=ActionError(
                code="action_job_failed",
                message=f"{action_kind} action.run job failed",
                action_kind=action_kind,
            ),
            job_id=job_id,
        )
    if result.errors and result.errors[0].code == StaleAttemptWriter.code:
        # A direct boundary may already have normalized the narrow refusal.
        # Do not terminalize or queue-retry the replacement's receipt.
        return result

    # The executor may have terminalized its own receipt (e.g. via its output
    # strategy). Trust a terminal result only when the reserved receipt is now
    # durably terminal; otherwise drive the reserved receipt terminal here.
    if result.status in FINISHED_RECEIPT_STATUSES:
        stored = ReceiptStore(project).find_by_id(receipt_id)
        if stored is not None and stored.parsed().status in FINISHED_RECEIPT_STATUSES:
            if isinstance(job_id, int) and result.job_id is None:
                result = result.model_copy(update={"job_id": job_id})
            return result
        if result.status == "cancelled":
            return action_job_cancelled_result(
                project,
                project_id=project_id,
                receipt_id=receipt_id,
                action_kind=action_kind,
                job_id=job_id,
            )
        if result.status == "failed":
            error = (
                result.errors[0]
                if result.errors
                else ActionError(
                    code="action_job_failed",
                    message=f"{action_kind} action.run job failed",
                    action_kind=action_kind,
                )
            )
            if _is_retryable_action_error(error):
                _mark_action_job_receipt_queued_for_retry(
                    project,
                    receipt_id=receipt_id,
                )
                raise ActionJobRetryableFailure(error)
            return action_job_failure_result(
                project,
                project_id=project_id,
                receipt_id=receipt_id,
                action_kind=action_kind,
                error=error,
                job_id=job_id,
            )
        return action_job_success_result(
            project,
            project_id=project_id,
            receipt_id=receipt_id,
            action_kind=action_kind,
            status=result.status,
            job_id=job_id,
        )
    return action_job_failure_result(
        project,
        project_id=project_id,
        receipt_id=receipt_id,
        action_kind=action_kind,
        error=ActionError(
            code="action_job_failed",
            message=f"{action_kind} executor returned nonterminal status {result.status!r}",
            action_kind=action_kind,
        ),
        job_id=job_id,
    )


def reconcile_exhausted_action_run_jobs(
    queue: Any,
    *,
    workspace_root: str | Path,
    limit: int = 1000,
    workspace_root_storage_org_id: int | None = None,
    project_opener: ProjectOpener | None = None,
) -> int:
    """Terminalize receipts for exhausted ``action.run`` jobs.

    The queue deliberately does not rerun a job whose single attempt died; that
    avoids duplicate irreversible provider calls. This reconciliation closes the
    paired receipt so replays return a terminal failed action instead of a stale
    queued claim.
    """

    from frisket.engine.jobs.queue import ACTION_RUN_KIND, claimed_project_root
    from frisket.engine.store import Project

    root = Path(workspace_root)
    reconciled = 0
    for job in queue.list_jobs(status="failed", limit=limit):
        if job.kind != ACTION_RUN_KIND:
            continue
        if not str(job.error or "").startswith("worker_lease_expired:"):
            continue
        envelope = action_run_envelope_from_payload(job.payload or {})
        if envelope is None:
            continue
        project_id = envelope.project_id
        storage_key = getattr(job, "project_storage_key", None)
        if project_opener is None:
            project_root = Path(job.payload.get("workspace_root") or root)
            project_path = project_root / f"{project_id}.frisket"
        else:
            # An injected opener only ever recovers a CLAIMED project: the
            # envelope's project_id is caller-controlled payload JSON, so a job
            # without a claimed key is left alone rather than opened by guess.
            if storage_key is None:
                continue
            project_id = storage_key.project_slug
            try:
                project_root = claimed_project_root(
                    storage_key,
                    workspace_root=root,
                    workspace_root_storage_org_id=workspace_root_storage_org_id,
                )
            except ValueError:
                continue
            project_path = project_root / f"{project_id}.frisket"
        if not project_path.exists() or not (project_path / "project.db").exists():
            continue
        project: Any | None = None
        try:
            project = (
                Project(project_path)
                if project_opener is None
                else project_opener(storage_key, project_path)
            )
            stored = ReceiptStore(project).find_by_id(envelope.receipt_id)
            if stored is None or stored.status in FINISHED_RECEIPT_STATUSES:
                continue
            action_job_exhausted_lease_result(
                project,
                project_id=project_id,
                receipt_id=envelope.receipt_id,
                action_kind=envelope.action_kind,
                job_id=job.id,
            )
            reconciled += 1
        except Exception as exc:  # noqa: BLE001 - recovery must not crash workers
            LOG.warning(
                "action_job_recovery_failed",
                extra={
                    "event": "action_job_recovery_failed",
                    "job_id": job.id,
                    "project_id": project_id,
                    "receipt_id": envelope.receipt_id,
                    "error": str(exc),
                },
            )
        finally:
            if project is not None:
                project.close()
    return reconciled
