"""Action run cancel service for local server routes."""

from __future__ import annotations

from typing import Any

from frisket.server.run_status import cancel_project_run
from frisket.server.workspace import Workspace
from frisket.server.route_errors import RouteError


class ActionRunCancelRouteError(RouteError):
    pass


class ActionRunCancelService:
    def __init__(self, workspace: Workspace):
        self._workspace = workspace

    def cancel_run(self, project_id: str, run_id: int) -> dict[str, Any]:
        project = self._workspace.get(project_id)
        row = project.db.execute(
            "SELECT status FROM runs WHERE id=?", (run_id,)
        ).fetchone()
        if row is None:
            raise ActionRunCancelRouteError(404, "no such run")
        if row["status"] not in {"running", "cancelled"}:
            raise ActionRunCancelRouteError(
                409, f"run is already {row['status']} — nothing to cancel"
            )
        cancelled = cancel_project_run(
            project=project,
            queue=self._workspace.queue,
            active_runs=self._workspace.active_runs,
            run_jobs=self._workspace.run_jobs,
            project_id=project_id,
            run_id=run_id,
            storage_org_id=self._workspace.queue_storage_org_id,
        )
        if cancelled.disposition == "cancel_pending":
            raise ActionRunCancelRouteError(
                409,
                {
                    "status": "cancel_pending",
                    "run_id": run_id,
                    "cancel_requested": cancelled.cancel_requested,
                    "queue_job_id": (
                        cancelled.job.id if cancelled.job is not None else None
                    ),
                    "queue_cancelled": cancelled.queue_cancelled,
                    "reason": (
                        cancelled.terminalization.reason
                        if cancelled.terminalization is not None
                        else "live_writer_must_terminalize"
                    ),
                },
            )
        if cancelled.disposition == "reconciliation_required":
            terminalization = cancelled.terminalization
            raise ActionRunCancelRouteError(
                409,
                {
                    "status": "reconciliation_required",
                    "run_id": run_id,
                    "cancel_requested": cancelled.cancel_requested,
                    "reserved_checkpoint_ids": (
                        list(terminalization.reserved_checkpoint_ids)
                        if terminalization is not None
                        else []
                    ),
                    "resolver": (
                        "resolve each checkpoint with `frisket reconcile "
                        "discard` or `frisket reconcile accept-charged`, then "
                        "retry this cancel request"
                    ),
                    "reason": (
                        terminalization.reason
                        if terminalization is not None
                        else "terminal_authority_not_proven"
                    ),
                },
            )
        if cancelled.disposition == "conflict":
            raise ActionRunCancelRouteError(
                409,
                {
                    "status": "cancel_conflict",
                    "run_id": run_id,
                    "cancel_requested": cancelled.cancel_requested,
                    "reason": (
                        cancelled.terminalization.reason
                        if cancelled.terminalization is not None
                        else "terminal_authority_not_proven"
                    ),
                },
            )
        cancelled_row = cancelled.row
        return {
            "run_id": run_id,
            "status": "cancelled",
            "completed": (
                cancelled_row["completed_rows"] if cancelled_row is not None else None
            ),
            "total": cancelled_row["total_rows"] if cancelled_row is not None else None,
            "queue_job_id": cancelled.job.id if cancelled.job is not None else None,
            "queue_cancelled": cancelled.queue_cancelled,
        }
