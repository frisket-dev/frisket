"""One fenced terminal transaction for durable project-run projections.

This module owns the transition shared by queued workers, queue-state
reconciliation, admin cancellation, and claimless action families.  It does
not own provider sidecars or runless ``action.run`` receipts.
"""

from __future__ import annotations

import json
from collections.abc import Collection
from dataclasses import dataclass
from typing import Literal

from frisket.contracts.action import ActionError, Receipt
from frisket.engine.store.output_claims import OutputColumnClaimStore
from frisket.engine.store.receipts import (
    FINISHED_RECEIPT_STATUSES,
    ReceiptStatus,
    ReceiptStore,
)
from frisket.execution.attempt import StaleAttemptWriter, set_attempt_state

RunTerminalStatus = Literal["completed", "failed", "cancelled"]
TerminalizationDisposition = Literal[
    "terminalized",
    "already_terminal",
    "receipt_missing",
    "conflict",
    "reconciliation_required",
]
ReceiptDisposition = Literal[
    "updated",
    "inserted",
    "already_terminal",
    "missing",
    "write_refused",
]


@dataclass(frozen=True)
class CurrentWriterTerminalAuthority:
    writer_attempt_id: str
    claim_token: str | None
    claimless_direct_effect: bool = False


@dataclass(frozen=True)
class UnclaimedRunTerminalAuthority:
    claimless_direct_effect: bool = False
    require_cancel_intent: bool = False


@dataclass(frozen=True)
class PreparedRunTerminalAuthority:
    prepared_attempt_id: str
    claimless_direct_effect: bool = False
    require_cancel_intent: bool = False


@dataclass(frozen=True)
class NeverDispatchedRunTerminalAuthority:
    """Close a refusal that happened before any attempt claimed dispatch.

    The authority is proved from the durable attempt topology under the
    terminal transaction's write lock.  Zero or one created/admitted attempt
    is pre-dispatch; a dispatching attempt or multiple open attempts requires
    an exact owner or explicit recovery instead.
    """

    claimless_direct_effect: bool = False


@dataclass(frozen=True)
class AbandonedAttemptRecoveryAuthority:
    claimless_direct_effect: bool = False
    require_cancel_intent: bool = True


@dataclass(frozen=True)
class AbandonedRunRecoveryAuthority:
    """Recover a swept run whose producer receipt predates attempt binding."""

    claimless_direct_effect: bool = False
    require_cancel_intent: bool = True


@dataclass(frozen=True)
class ReceiptOnlyOrphanAuthority:
    claimless_direct_effect: bool = False


@dataclass(frozen=True)
class TerminalRunReceiptRepairAuthority:
    """Fence receipt repair after terminal-only queued schema drift.

    A queued payload can become undecodable after its run has already committed
    a terminal status. This authority repairs the run-bound receipt and claim
    without changing that durable outcome. It refuses a run reopened for
    resume, or any live/pending writer, under the terminal transaction's lock.
    """

    claimless_direct_effect: bool = False


ProjectRunTerminalAuthority = (
    CurrentWriterTerminalAuthority
    | UnclaimedRunTerminalAuthority
    | PreparedRunTerminalAuthority
    | NeverDispatchedRunTerminalAuthority
    | AbandonedAttemptRecoveryAuthority
    | AbandonedRunRecoveryAuthority
    | ReceiptOnlyOrphanAuthority
    | TerminalRunReceiptRepairAuthority
)


@dataclass(frozen=True)
class ProjectRunTerminalizationResult:
    disposition: TerminalizationDisposition
    receipt_disposition: ReceiptDisposition
    run_status: str | None
    receipt_status: str | None
    reserved_checkpoint_ids: tuple[str, ...] = ()
    reason: str | None = None


def _result(
    disposition: TerminalizationDisposition,
    *,
    receipt_disposition: ReceiptDisposition,
    run_status: str | None,
    receipt_status: str | None,
    reserved_checkpoint_ids: Collection[str] = (),
    reason: str | None = None,
) -> ProjectRunTerminalizationResult:
    return ProjectRunTerminalizationResult(
        disposition=disposition,
        receipt_disposition=receipt_disposition,
        run_status=run_status,
        receipt_status=receipt_status,
        reserved_checkpoint_ids=tuple(reserved_checkpoint_ids),
        reason=reason,
    )


def _prepared_attempt_id(receipt: Receipt, run_id: int) -> str | None:
    matches = [
        item.ref
        for item in receipt.evidence
        if item.ref.get("kind") == "queued_action_run_prepared"
        and item.ref.get("queue_kind") == "project.run"
        and item.ref.get("run_id") == run_id
    ]
    if len(matches) != 1:
        return None
    attempt_id = matches[0].get("attempt_id")
    return attempt_id if isinstance(attempt_id, str) and attempt_id else None


def _terminal_receipt_from(
    stored: Receipt,
    *,
    run_id: int,
    status: ReceiptStatus,
    errors: Collection[ActionError],
) -> Receipt:
    return stored.model_copy(
        update={"run_id": run_id, "status": status, "errors": list(errors)}
    )


def terminalize_project_run(
    project: object,
    *,
    run_id: int,
    receipt_id: str,
    status: ReceiptStatus,
    authority: ProjectRunTerminalAuthority,
    run_status: RunTerminalStatus | None = None,
    terminal_receipt: Receipt | None = None,
    errors: Collection[ActionError] = (),
    receipt_source_statuses: Collection[str] | None = ("queued", "running"),
    commit: bool = True,
) -> ProjectRunTerminalizationResult:
    """Terminalize one project run under current, pre-dispatch, or recovery authority.

    Except for never-dispatched authority, ``commit=False`` joins an already-open
    family transaction and never begins, commits, or rolls it back.
    ``receipt_source_statuses=None`` is the explicit no-reservation/insert mode
    used by direct embedding refreshes.
    """

    if not isinstance(receipt_id, str) or not receipt_id:
        raise ValueError("project-run terminalization requires a receipt id")
    if status not in FINISHED_RECEIPT_STATUSES:
        raise ValueError(f"unsupported terminal receipt status: {status}")
    terminal_run_status: RunTerminalStatus = run_status or (
        "completed" if status in {"completed", "partial"} else status
    )
    if terminal_run_status not in {"completed", "failed", "cancelled"}:
        raise ValueError(f"unsupported terminal run status: {terminal_run_status}")
    if isinstance(authority, NeverDispatchedRunTerminalAuthority) and not commit:
        raise ValueError(
            "never-dispatched terminal authority requires its owned "
            "BEGIN IMMEDIATE transaction"
        )
    db = project.db  # type: ignore[attr-defined]
    if commit:
        db.execute("BEGIN IMMEDIATE")
    elif not db.in_transaction:
        raise RuntimeError(
            "terminalize_project_run(commit=False) requires a caller transaction"
        )

    def complete(
        result: ProjectRunTerminalizationResult,
    ) -> ProjectRunTerminalizationResult:
        if commit:
            db.commit()
        return result

    try:
        run = db.execute(
            "SELECT runs.status, runs.finished_at, runs.current_attempt_id, "
            "runs.cancel_requested_at, ops.spec AS runner_spec "
            "FROM runs JOIN ops ON ops.id=runs.op_id WHERE runs.id=?",
            (run_id,),
        ).fetchone()
        if run is None:
            return complete(
                _result(
                    "conflict",
                    receipt_disposition="missing",
                    run_status=None,
                    receipt_status=None,
                    reason="run_missing",
                )
            )

        receipts = ReceiptStore(project)
        stored_row = receipts.find_by_id(receipt_id)
        stored_receipt: Receipt | None = None
        if stored_row is not None:
            try:
                stored_receipt = stored_row.parsed()
            except Exception:
                return complete(
                    _result(
                        "conflict",
                        receipt_disposition="write_refused",
                        run_status=str(run["status"]),
                        receipt_status=str(stored_row.status),
                        reason="receipt_body_invalid",
                    )
                )
            attaching_rich_receipt = bool(
                terminal_receipt is not None
                and isinstance(authority, CurrentWriterTerminalAuthority)
                and stored_row.run_id is None
                and stored_receipt.run_id is None
                and receipt_source_statuses is not None
            )
            if (
                (stored_row.run_id != run_id and not attaching_rich_receipt)
                or (stored_receipt.run_id != run_id and not attaching_rich_receipt)
                or stored_receipt.receipt_id != receipt_id
                or stored_receipt.action_kind != stored_row.action_kind
                or stored_receipt.status != stored_row.status
            ):
                return complete(
                    _result(
                        "conflict",
                        receipt_disposition="write_refused",
                        run_status=str(run["status"]),
                        receipt_status=str(stored_row.status),
                        reason="receipt_identity_mismatch",
                    )
                )

        if terminal_receipt is not None:
            if (
                terminal_receipt.receipt_id != receipt_id
                or terminal_receipt.run_id != run_id
                or terminal_receipt.status != status
            ):
                raise ValueError("terminal receipt does not match run/id/status")
            if stored_receipt is not None and (
                terminal_receipt.action_kind != stored_receipt.action_kind
                or terminal_receipt.action_id != stored_receipt.action_id
                or terminal_receipt.params_hash != stored_receipt.params_hash
            ):
                return complete(
                    _result(
                        "conflict",
                        receipt_disposition="write_refused",
                        run_status=str(run["status"]),
                        receipt_status=str(stored_row.status),
                        reason="terminal_receipt_identity_mismatch",
                    )
                )

        if terminal_receipt is not None and receipt_source_statuses is not None:
            allowed_sources = {str(item) for item in receipt_source_statuses}
            if not allowed_sources:
                raise ValueError("receipt source statuses cannot be empty")
            if stored_row is None:
                return complete(
                    _result(
                        "receipt_missing",
                        receipt_disposition="missing",
                        run_status=str(run["status"]),
                        receipt_status=None,
                        reason="rich_terminal_receipt_reservation_missing",
                    )
                )
            if stored_row.status == status and stored_row.run_id != run_id:
                return complete(
                    _result(
                        "conflict",
                        receipt_disposition="write_refused",
                        run_status=str(run["status"]),
                        receipt_status=stored_row.status,
                        reason="terminal_receipt_cannot_attach_terminal_reservation",
                    )
                )
            if stored_row.status != status and stored_row.status not in allowed_sources:
                return complete(
                    _result(
                        "conflict",
                        receipt_disposition="write_refused",
                        run_status=str(run["status"]),
                        receipt_status=stored_row.status,
                        reason="rich_terminal_receipt_source_refused",
                    )
                )

        if receipt_source_statuses is None:
            if terminal_receipt is None:
                raise ValueError("receipt insert mode requires a terminal receipt")
            if stored_row is not None:
                return complete(
                    _result(
                        "conflict",
                        receipt_disposition="write_refused",
                        run_status=str(run["status"]),
                        receipt_status=str(stored_row.status),
                        reason="receipt_insert_collision",
                    )
                )
        elif stored_receipt is None:
            return complete(
                _result(
                    "receipt_missing",
                    receipt_disposition="missing",
                    run_status=str(run["status"]),
                    receipt_status=None,
                    reason="project_run_receipt_missing",
                )
            )

        attempts = db.execute(
            "SELECT id, state, seq FROM execution_attempts WHERE run_id=? ORDER BY seq",
            (run_id,),
        ).fetchall()
        attempt_states = {str(row["id"]): str(row["state"]) for row in attempts}
        attempt_seqs = {str(row["id"]): int(row["seq"]) for row in attempts}
        live_attempt_ids = tuple(
            attempt_id
            for attempt_id, attempt_state in attempt_states.items()
            if attempt_state == "dispatching"
        )
        pending_attempt_ids = tuple(
            attempt_id
            for attempt_id, attempt_state in attempt_states.items()
            if attempt_state in {"created", "admitted"}
        )

        if isinstance(authority, NeverDispatchedRunTerminalAuthority):
            active_claims = db.execute(
                "SELECT run_id, receipt_id, action_kind, claim_token "
                "FROM output_column_claims WHERE status='active' "
                "AND (run_id=? OR receipt_id=?)",
                (run_id, receipt_id),
            ).fetchall()
        else:
            active_claims = db.execute(
                "SELECT run_id, receipt_id, action_kind, claim_token "
                "FROM output_column_claims WHERE run_id=? AND status='active'",
                (run_id,),
            ).fetchall()
        claimless = authority.claimless_direct_effect
        current_status = str(run["status"])
        # A deferred action finalizer can still own a provisional MapRunner
        # terminal status.  In particular, join.semantic leaves its exact
        # attempt dispatching and its claim active after the map run records
        # ``completed``; only then can the worker promote a persisted provider
        # failure to the action's honest ``failed`` outcome.  No recovery or
        # unclaimed authority may reclassify a terminal run.
        current_writer_reclassification = bool(
            isinstance(authority, CurrentWriterTerminalAuthority)
            and current_status in {"completed", "failed", "cancelled"}
            and current_status != terminal_run_status
            and stored_row is not None
            and stored_row.status in {"queued", "running"}
        )
        if (
            current_status not in {"running", terminal_run_status}
            and not current_writer_reclassification
        ):
            return complete(
                _result(
                    "conflict",
                    receipt_disposition="write_refused",
                    run_status=current_status,
                    receipt_status=(None if stored_row is None else stored_row.status),
                    reason="run_already_terminal_with_different_outcome",
                )
            )

        authority_attempt_id: str | None = None
        if isinstance(authority, CurrentWriterTerminalAuthority):
            authority_attempt_id = authority.writer_attempt_id
            try:
                OutputColumnClaimStore.require_current_writer(
                    db,
                    run_id=run_id,
                    writer_attempt_id=authority.writer_attempt_id,
                    claim_token=authority.claim_token,
                    claimless_direct_effect=authority.claimless_direct_effect,
                )
            except StaleAttemptWriter as exc:
                terminal_retry = (
                    current_status == terminal_run_status
                    and stored_row is not None
                    and stored_row.status == status
                    and not active_claims
                    and not live_attempt_ids
                    and not pending_attempt_ids
                )
                if terminal_retry:
                    return complete(
                        _result(
                            "already_terminal",
                            receipt_disposition="already_terminal",
                            run_status=current_status,
                            receipt_status=stored_row.status,
                        )
                    )
                return complete(
                    _result(
                        "conflict",
                        receipt_disposition="write_refused",
                        run_status=current_status,
                        receipt_status=(
                            None if stored_row is None else stored_row.status
                        ),
                        reason=str(exc),
                    )
                )
            if run["current_attempt_id"] != authority.writer_attempt_id:
                return complete(
                    _result(
                        "conflict",
                        receipt_disposition="write_refused",
                        run_status=current_status,
                        receipt_status=(
                            None if stored_row is None else stored_row.status
                        ),
                        reason=(
                            "current_writer_authority_not_proven: "
                            f"run holder {run['current_attempt_id']!r} does not "
                            f"match writer {authority.writer_attempt_id!r}"
                        ),
                    )
                )
        elif isinstance(authority, AbandonedAttemptRecoveryAuthority):
            if stored_receipt is None:
                return complete(
                    _result(
                        "receipt_missing",
                        receipt_disposition="missing",
                        run_status=current_status,
                        receipt_status=None,
                        reason="recovery_receipt_missing",
                    )
                )
            authority_attempt_id = _prepared_attempt_id(stored_receipt, run_id)
            if (
                authority_attempt_id is None
                or attempt_states.get(authority_attempt_id) != "abandoned"
                or run["current_attempt_id"] is not None
                or live_attempt_ids
                or pending_attempt_ids
                or (
                    authority.require_cancel_intent
                    and run["cancel_requested_at"] is None
                )
            ):
                return complete(
                    _result(
                        "conflict",
                        receipt_disposition="write_refused",
                        run_status=current_status,
                        receipt_status=stored_row.status,
                        reason="abandoned_recovery_authority_not_proven",
                    )
                )
        elif isinstance(authority, AbandonedRunRecoveryAuthority):
            abandoned_attempt_ids = tuple(
                attempt_id
                for attempt_id, attempt_state in attempt_states.items()
                if attempt_state == "abandoned"
            )
            authority_attempt_id = (
                max(abandoned_attempt_ids, key=attempt_seqs.__getitem__)
                if abandoned_attempt_ids
                else None
            )
            if (
                authority_attempt_id is None
                or run["current_attempt_id"] is not None
                or live_attempt_ids
                or pending_attempt_ids
                or (
                    authority.require_cancel_intent
                    and run["cancel_requested_at"] is None
                )
            ):
                return complete(
                    _result(
                        "conflict",
                        receipt_disposition="write_refused",
                        run_status=current_status,
                        receipt_status=(
                            None if stored_row is None else stored_row.status
                        ),
                        reason="abandoned_run_recovery_authority_not_proven",
                    )
                )
        elif isinstance(authority, PreparedRunTerminalAuthority):
            if run["current_attempt_id"] is not None or live_attempt_ids:
                return complete(
                    _result(
                        "conflict",
                        receipt_disposition="write_refused",
                        run_status=current_status,
                        receipt_status=(
                            None if stored_row is None else stored_row.status
                        ),
                        reason=(
                            "live_writer_requires_owner_terminalization: the run has "
                            "a durable dispatching attempt; terminalization requires "
                            "that exact writer"
                        ),
                    )
                )
            authority_attempt_id = authority.prepared_attempt_id
            receipt_attempt_id = (
                None
                if stored_receipt is None
                else _prepared_attempt_id(stored_receipt, run_id)
            )
            prepared_attempt_proven = bool(
                authority_attempt_id == receipt_attempt_id
                and attempt_states.get(authority_attempt_id) == "admitted"
                and pending_attempt_ids == (authority_attempt_id,)
                and attempt_seqs.get(authority_attempt_id)
                == max(attempt_seqs.values(), default=-1)
            )
            if not prepared_attempt_proven or (
                authority.require_cancel_intent and run["cancel_requested_at"] is None
            ):
                return complete(
                    _result(
                        "conflict",
                        receipt_disposition="write_refused",
                        run_status=current_status,
                        receipt_status=(
                            None if stored_row is None else stored_row.status
                        ),
                        reason="prepared_attempt_authority_not_proven",
                    )
                )
        elif isinstance(authority, NeverDispatchedRunTerminalAuthority):
            open_attempt_ids = tuple(
                attempt_id
                for attempt_id, attempt_state in attempt_states.items()
                if attempt_state in {"created", "admitted", "dispatching"}
            )
            terminal_tuple_retry = bool(
                current_status == terminal_run_status
                and stored_row is not None
                and stored_row.status == status
                and run["current_attempt_id"] is None
                and not active_claims
                and not live_attempt_ids
                and not pending_attempt_ids
            )
            has_dispatch_history = any(
                attempt_state in {"effected", "halted", "abandoned"}
                for attempt_state in attempt_states.values()
            )
            if (
                run["current_attempt_id"] is not None
                or live_attempt_ids
                or len(open_attempt_ids) > 1
                or (has_dispatch_history and not terminal_tuple_retry)
            ):
                return complete(
                    _result(
                        "conflict",
                        receipt_disposition="write_refused",
                        run_status=current_status,
                        receipt_status=(
                            None if stored_row is None else stored_row.status
                        ),
                        reason="never_dispatched_authority_not_proven",
                    )
                )
            effect_history = db.execute(
                "SELECT ec.id FROM effect_checkpoints ec "
                "JOIN execution_attempts a ON a.id=ec.authorized_attempt_id "
                "WHERE a.run_id=? AND ec.state IN ('returned','consumed') "
                "ORDER BY ec.id",
                (run_id,),
            ).fetchall()
            if effect_history and not terminal_tuple_retry:
                return complete(
                    _result(
                        "conflict",
                        receipt_disposition="write_refused",
                        run_status=current_status,
                        receipt_status=(
                            None if stored_row is None else stored_row.status
                        ),
                        reason="never_dispatched_effect_history",
                    )
                )
            authority_attempt_id = open_attempt_ids[0] if open_attempt_ids else None
        elif isinstance(authority, UnclaimedRunTerminalAuthority):
            if run["current_attempt_id"] is not None or live_attempt_ids:
                return complete(
                    _result(
                        "conflict",
                        receipt_disposition="write_refused",
                        run_status=current_status,
                        receipt_status=(
                            None if stored_row is None else stored_row.status
                        ),
                        reason=(
                            "live_writer_requires_owner_terminalization: the run has "
                            "a durable dispatching attempt; terminalization requires "
                            "that exact writer"
                        ),
                    )
                )
            has_abandoned_history = any(
                state == "abandoned" for state in attempt_states.values()
            )
            implicit_pending_attempt_id = (
                pending_attempt_ids[0]
                if len(pending_attempt_ids) == 1
                and attempt_states.get(pending_attempt_ids[0]) == "admitted"
                and not has_abandoned_history
                else None
            )
            if (
                has_abandoned_history
                or (pending_attempt_ids and implicit_pending_attempt_id is None)
                or (
                    authority.require_cancel_intent
                    and run["cancel_requested_at"] is None
                )
            ):
                return complete(
                    _result(
                        "conflict",
                        receipt_disposition="write_refused",
                        run_status=current_status,
                        receipt_status=(
                            None if stored_row is None else stored_row.status
                        ),
                        reason="unclaimed_run_authority_not_proven",
                    )
                )
            authority_attempt_id = implicit_pending_attempt_id
        elif isinstance(authority, ReceiptOnlyOrphanAuthority):
            # Receipt-only orphan repair deliberately leaves the run running,
            # but it may never race a live writer.
            if run["current_attempt_id"] is not None or live_attempt_ids:
                return complete(
                    _result(
                        "conflict",
                        receipt_disposition="write_refused",
                        run_status=current_status,
                        receipt_status=(
                            None if stored_row is None else stored_row.status
                        ),
                        reason="receipt_orphan_has_live_writer",
                    )
                )
        else:
            # Schema-drift repair is deliberately narrower than unclaimed-run
            # authority: the run must still be terminal under this write lock
            # and no writer may be able to publish against it.
            if (
                current_status == "running"
                or run["current_attempt_id"] is not None
                or live_attempt_ids
                or pending_attempt_ids
            ):
                return complete(
                    _result(
                        "conflict",
                        receipt_disposition="write_refused",
                        run_status=current_status,
                        receipt_status=(
                            None if stored_row is None else stored_row.status
                        ),
                        reason="terminal_run_receipt_repair_has_writer",
                    )
                )

        reserved_rows = db.execute(
            "SELECT ec.id FROM effect_checkpoints ec "
            "JOIN execution_attempts a ON a.id=ec.authorized_attempt_id "
            "WHERE a.run_id=? AND ec.state='reserved' ORDER BY ec.id",
            (run_id,),
        ).fetchall()
        reserved_ids = tuple(str(row["id"]) for row in reserved_rows)
        if reserved_ids:
            return complete(
                _result(
                    "reconciliation_required",
                    receipt_disposition="write_refused",
                    run_status=current_status,
                    receipt_status=(None if stored_row is None else stored_row.status),
                    reserved_checkpoint_ids=reserved_ids,
                    reason="reserved_effect_checkpoint",
                )
            )

        # Paid-effect ambiguity dominates a missing or malformed claim after
        # recovery authority has been proven. Only once no reservation is
        # ambiguous may ordinary claim topology accept or refuse the close.
        if isinstance(authority, NeverDispatchedRunTerminalAuthority) and any(
            row["run_id"] is None or int(row["run_id"]) != run_id
            for row in active_claims
        ):
            return complete(
                _result(
                    "conflict",
                    receipt_disposition="write_refused",
                    run_status=current_status,
                    receipt_status=(None if stored_row is None else stored_row.status),
                    reason="never_dispatched_claim_run_mismatch",
                )
            )
        if claimless and active_claims:
            return complete(
                _result(
                    "conflict",
                    receipt_disposition="write_refused",
                    run_status=current_status,
                    receipt_status=(None if stored_row is None else stored_row.status),
                    reason="claimless_run_has_active_claims",
                )
            )
        if not claimless and active_claims:
            if any(row["receipt_id"] != receipt_id for row in active_claims):
                return complete(
                    _result(
                        "conflict",
                        receipt_disposition="write_refused",
                        run_status=current_status,
                        receipt_status=(
                            None if stored_row is None else stored_row.status
                        ),
                        reason="active_claim_identity_mismatch",
                    )
                )
            if stored_receipt is not None and any(
                str(row["action_kind"]) != stored_receipt.action_kind
                for row in active_claims
            ):
                return complete(
                    _result(
                        "conflict",
                        receipt_disposition="write_refused",
                        run_status=current_status,
                        receipt_status=stored_row.status,
                        reason="active_claim_action_mismatch",
                    )
                )
        if (
            not claimless
            and not active_claims
            and current_status == "running"
            and stored_row is not None
            and stored_row.status in {"queued", "running"}
            and not isinstance(authority, NeverDispatchedRunTerminalAuthority)
        ):
            return complete(
                _result(
                    "conflict",
                    receipt_disposition="write_refused",
                    run_status=current_status,
                    receipt_status=stored_row.status,
                    reason="claimed_terminalization_has_no_active_claim",
                )
            )

        if (
            current_status == terminal_run_status
            and stored_row is not None
            and stored_row.status == status
            and not active_claims
            and not live_attempt_ids
            and not pending_attempt_ids
        ):
            return complete(
                _result(
                    "already_terminal",
                    receipt_disposition="already_terminal",
                    run_status=current_status,
                    receipt_status=stored_row.status,
                )
            )

        if terminal_run_status == "cancelled" and authority_attempt_id is not None:
            # The terminalizer is the producer for rows cancellation prevented
            # from publishing.  Result persistence has already classified
            # every completed row under the same attempt; fill only the
            # remaining write-once slots so settlement can exclude cancelled
            # rows individually without turning their absence into a refusal
            # for completed siblings.
            db.execute(
                "UPDATE attempt_row_authorizations "
                "SET terminal_outcome='cancelled' "
                "WHERE attempt_id=? AND terminal_outcome IS NULL",
                (authority_attempt_id,),
            )

        if pending_attempt_ids:
            placeholders = ",".join("?" for _ in pending_attempt_ids)
            pending_terminal_state = (
                "halted"
                if isinstance(authority, NeverDispatchedRunTerminalAuthority)
                else "superseded"
            )
            pending_update = db.execute(
                "UPDATE execution_attempts SET state=? "
                f"WHERE id IN ({placeholders}) AND state IN ('created','admitted')",
                (pending_terminal_state, *pending_attempt_ids),
            )
            if pending_update.rowcount != len(pending_attempt_ids):
                raise RuntimeError(
                    "project-run pending attempt closure refused: "
                    f"expected {len(pending_attempt_ids)}, closed "
                    f"{pending_update.rowcount}"
                )
        if not isinstance(authority, ReceiptOnlyOrphanAuthority) and (
            current_status == "running" or current_writer_reclassification
        ):
            current_writer_id = (
                authority.writer_attempt_id
                if isinstance(authority, CurrentWriterTerminalAuthority)
                else None
            )
            run_update = db.execute(
                "UPDATE runs SET status=?, finished_at=datetime('now') "
                "WHERE id=? AND status=? "
                "AND (? IS NULL OR current_attempt_id=?)",
                (
                    terminal_run_status,
                    run_id,
                    current_status,
                    current_writer_id,
                    current_writer_id,
                ),
            )
            if run_update.rowcount != 1:
                raise RuntimeError(
                    "project-run terminal status write refused: "
                    f"run {run_id} did not leave {current_status}"
                )

        from frisket.engine.store.result_generations import ResultGenerationStore

        generation_store = ResultGenerationStore(project)
        declared_bindings = generation_store.bindings_for_run(run_id)
        if any(binding.state != "sealed" for binding in declared_bindings):
            claim_tokens = {str(row["claim_token"]) for row in active_claims}
            if len(claim_tokens) != 1:
                raise RuntimeError(
                    "open result generation terminalization requires one exact claim"
                )
            # Pass the COMPLETE declared set: seal() requires exactly that
            # set and skips members already sealed with a matching
            # disposition, so this stays correct even if a partially-sealed
            # run ever becomes durably observable (a mismatched prior
            # disposition still refuses loudly inside seal()).
            generation_store.seal(
                run_id,
                [binding.column_id for binding in declared_bindings],
                claim_token=claim_tokens.pop(),
                terminal_disposition=terminal_run_status,
                # A pre-dispatch target rename invalidates the queued action,
                # but it must not strand the exact prepared run/receipt/claim.
                # NeverDispatched authority has already proven no effect could
                # publish; permit only this terminal close to ignore name drift.
                allow_stale_output_name=isinstance(
                    authority, NeverDispatchedRunTerminalAuthority
                ),
                # Only the host's coupled finalizer may expose these heads.
                # Read its durable declaration, never the worker/job payload.
                publish=json.loads(run["runner_spec"]).get("deferred_publication")
                is not True,
                commit=False,
            )

        receipt_disposition: ReceiptDisposition
        if receipt_source_statuses is None:
            assert terminal_receipt is not None
            receipts.insert_finished(terminal_receipt, commit=False)
            receipt_disposition = "inserted"
        elif stored_receipt is None:
            receipt_disposition = "missing"
        elif stored_row.status == status:
            receipt_disposition = "already_terminal"
        elif stored_row.status in FINISHED_RECEIPT_STATUSES:
            receipt_disposition = "already_terminal"
        else:
            landed_receipt = terminal_receipt or _terminal_receipt_from(
                stored_receipt,
                run_id=run_id,
                status=status,
                errors=errors,
            )
            landed = receipts.update_if_status_in(
                landed_receipt,
                receipt_source_statuses,
                commit=False,
            )
            receipt_disposition = "updated" if landed else "write_refused"

        if isinstance(authority, CurrentWriterTerminalAuthority):
            set_attempt_state(
                project,
                authority.writer_attempt_id,
                "effected",
                commit=False,
            )
            closed_attempt = db.execute(
                "SELECT state FROM execution_attempts WHERE id=?",
                (authority.writer_attempt_id,),
            ).fetchone()
            owner_pointer = db.execute(
                "SELECT current_attempt_id FROM runs WHERE id=?",
                (run_id,),
            ).fetchone()
            if (
                closed_attempt is None
                or str(closed_attempt["state"]) != "effected"
                or owner_pointer is None
                or owner_pointer["current_attempt_id"] is not None
            ):
                raise RuntimeError(
                    "project-run terminal attempt closure refused: "
                    f"attempt {authority.writer_attempt_id} did not close as "
                    "effected"
                )

        released = 0
        if not claimless and receipt_disposition != "missing":
            claim_store = OutputColumnClaimStore(project)
            terminal_claim_status = (
                "released" if status in {"completed", "partial"} else status
            )
            released = claim_store.release_for_run_receipt(
                run_id=run_id,
                receipt_id=receipt_id,
                status=terminal_claim_status,
                commit=False,
            )
            if released != len(active_claims):
                raise RuntimeError(
                    "project-run terminal claim release refused: "
                    f"expected {len(active_claims)}, released {released}"
                )

        final_run = db.execute(
            "SELECT status, finished_at FROM runs WHERE id=?", (run_id,)
        ).fetchone()
        final_receipt = receipts.find_by_id(receipt_id)
        if not isinstance(authority, ReceiptOnlyOrphanAuthority) and (
            final_run is None
            or str(final_run["status"]) != terminal_run_status
            or final_run["finished_at"] is None
        ):
            raise RuntimeError(
                "project-run terminal status postcondition failed: "
                f"run {run_id} is not durably {terminal_run_status}"
            )
        if final_receipt is None:
            raise RuntimeError(f"project-run terminal receipt {receipt_id} disappeared")
        try:
            parsed_final_receipt = final_receipt.parsed()
        except Exception as exc:
            raise RuntimeError(
                f"project-run terminal receipt {receipt_id} became invalid"
            ) from exc
        if (
            parsed_final_receipt.receipt_id != receipt_id
            or parsed_final_receipt.run_id != run_id
            or parsed_final_receipt.status != final_receipt.status
            or (
                receipt_disposition in {"updated", "inserted"}
                and final_receipt.status != status
            )
            or receipt_disposition == "write_refused"
        ):
            raise RuntimeError(
                "project-run terminal receipt postcondition failed: "
                f"receipt {receipt_id} did not land consistently"
            )
        disposition: TerminalizationDisposition = (
            "receipt_missing" if receipt_disposition == "missing" else "terminalized"
        )
        return complete(
            _result(
                disposition,
                receipt_disposition=receipt_disposition,
                run_status=(None if final_run is None else str(final_run["status"])),
                receipt_status=(
                    None if final_receipt is None else str(final_receipt.status)
                ),
            )
        )
    except BaseException:
        if commit:
            db.rollback()
        raise
