"""Action run trace service for local server routes."""

from __future__ import annotations

from typing import Any

from frisket.authoring import actions as action_contract
from frisket.server.workspace import Workspace
from frisket.server.route_errors import RouteError


class ActionRunTraceRouteError(RouteError):
    pass


class ActionRunTraceService:
    def __init__(self, workspace: Workspace):
        self._workspace = workspace

    def run_trace(self, project_id: str, run_id: int) -> dict[str, Any]:
        project = self._workspace.get(project_id)
        try:
            return action_contract.run_trace(project_id, project, run_id)
        except KeyError as exc:
            raise ActionRunTraceRouteError(404, str(exc)) from exc
        except FileNotFoundError:
            return {
                "schema_version": action_contract.ACTION_SCHEMA_VERSION,
                "action": "run_trace",
                "project_id": project_id,
                "run_id": run_id,
                "recorded": False,
                "trace": None,
            }

    def run_trace_row(
        self,
        project_id: str,
        run_id: int,
        row_id: int,
        *,
        column_id: int | None,
    ) -> dict[str, Any]:
        project = self._workspace.get(project_id)
        try:
            return action_contract.run_trace_row(
                project_id,
                project,
                run_id,
                row_id,
                column_id=column_id,
            )
        except KeyError as exc:
            raise ActionRunTraceRouteError(404, str(exc)) from exc
