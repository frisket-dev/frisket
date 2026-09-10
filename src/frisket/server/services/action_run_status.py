"""Action run status service for local server routes."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from frisket.server.run_status import action_run_status_payload
from frisket.server.workspace import Workspace
from frisket.server.route_errors import RouteError


class ActionRunStatusRouteError(RouteError):
    pass


class ActionRunStatusService:
    def __init__(
        self,
        workspace: Workspace,
        *,
        stale_run_grace_seconds: float,
        worker_liveness_window_seconds: float = 90.0,
        queue_timeout_seconds: float | None = None,
    ):
        self._workspace = workspace
        self._stale_run_grace_seconds = stale_run_grace_seconds
        self._worker_liveness_window_seconds = worker_liveness_window_seconds
        self._queue_timeout_seconds = queue_timeout_seconds

    def run_status(self, project_id: str, run_id: int) -> dict[str, Any]:
        try:
            return action_run_status_payload(
                project_id=project_id,
                project=self._workspace.get(project_id),
                queue=self._workspace.queue,
                active_runs=self._workspace.active_runs,
                run_jobs=self._workspace.run_jobs,
                run_id=run_id,
                now=datetime.now(UTC),
                stale_run_grace_seconds=self._stale_run_grace_seconds,
                worker_liveness_window_seconds=self._worker_liveness_window_seconds,
                queue_timeout_seconds=self._queue_timeout_seconds,
                storage_org_id=self._workspace.queue_storage_org_id,
            )
        except KeyError as exc:
            raise ActionRunStatusRouteError(404, str(exc)) from exc
