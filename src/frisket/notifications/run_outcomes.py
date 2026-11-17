"""Run notifications projected from a run's committed terminal state.

Every caller passes a run whose outcome may have just committed; the run row
and its latest receipt are the only inputs, so repeated calls for the same
outcome resolve to the same dedupe key and record one notification. A run is
never notified from inside an open transaction: that outcome has not
committed yet, and emitting would commit the caller's transaction early.
"""

from __future__ import annotations

import contextlib
import logging
from typing import TYPE_CHECKING

from frisket.engine.store.receipts import ReceiptStore
from frisket.notifications.candidates import NotificationCandidate
from frisket.notifications.emit import emit_notification_candidate
from frisket.notifications.producers import run_transition_notification_candidate
from frisket.ops.base import persisted_recipe_invocation_halt

if TYPE_CHECKING:
    from frisket.contracts.action import ActionResult
    from frisket.engine.store import Project

LOG = logging.getLogger(__name__)

_TERMINAL_RUN_STATUSES = frozenset({"completed", "failed", "cancelled"})


def emit_run_outcome_notification(project: Project, run_id: int) -> None:
    """Record the notification for ``run_id``'s committed terminal outcome.

    Never raises: the run's outcome stays authoritative when emission fails.
    """
    try:
        if project.db.in_transaction:
            LOG.warning(
                "run_notification_skipped_open_transaction",
                extra={"run_id": int(run_id)},
            )
            return
        candidate = run_outcome_notification_candidate(project, int(run_id))
        if candidate is not None:
            emit_notification_candidate(project, candidate)
    except Exception:
        LOG.warning(
            "run_notification_emit_failed",
            extra={"run_id": run_id},
            exc_info=True,
        )
        # Entry required a closed transaction, so anything open now holds
        # only this emission's partial notification writes.
        with contextlib.suppress(Exception):
            if project.db.in_transaction:
                project.db.rollback()


def emit_action_run_outcome_notification(
    project: Project, result: ActionResult
) -> None:
    """Notify the run an action result names, directly or through its receipt."""
    run_id = result.run_id
    try:
        if run_id is None and result.receipt_id and not project.db.in_transaction:
            stored = ReceiptStore(project).find_by_id(result.receipt_id)
            run_id = None if stored is None else stored.run_id
    except Exception:
        LOG.warning("run_notification_receipt_lookup_failed", exc_info=True)
        return
    if run_id is not None:
        emit_run_outcome_notification(project, int(run_id))


def run_outcome_notification_candidate(
    project: Project, run_id: int
) -> NotificationCandidate | None:
    run = project.db.execute(
        "SELECT status, params, action_kind, completed_rows, failed_rows "
        "FROM runs WHERE id=?",
        (run_id,),
    ).fetchone()
    if run is None or str(run["status"]) not in _TERMINAL_RUN_STATUSES:
        return None
    receipts = ReceiptStore(project)
    receipt_id = receipts.latest_id_for_run(run_id)
    receipt = None if receipt_id is None else receipts.parsed_by_id(receipt_id)
    if receipt is None:
        return None
    status = str(run["status"])
    # An allowlisted invocation halt is a resumable stop the person must act
    # on; any other cancellation reads as an ordinary cancel.
    halt = (
        persisted_recipe_invocation_halt(run["params"])
        if status == "cancelled"
        else None
    )
    error = None
    if status == "failed":
        messages = [item.message for item in receipt.errors if item.message.strip()]
        error = "; ".join(messages) or None
    return run_transition_notification_candidate(
        project_id=receipt.project_id,
        run_id=run_id,
        status="needs_user_action" if halt is not None else status,
        action_kind=str(run["action_kind"]),
        reason=None if halt is None else halt[0],
        error=error,
        completed_rows=int(run["completed_rows"] or 0),
        failed_rows=int(run["failed_rows"] or 0),
        payload={} if halt is None else {"halted_reason": halt[1]},
    )
