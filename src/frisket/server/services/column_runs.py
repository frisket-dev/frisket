"""Column run history service for local server routes."""

from __future__ import annotations

from typing import Any

from frisket.server.run_payloads import column_run_provenance_payload
from frisket.server.workspace import Workspace
from frisket.server.route_errors import RouteError


class ColumnRunHistoryRouteError(RouteError):
    pass


class ColumnRunHistoryService:
    def __init__(self, workspace: Workspace):
        self._workspace = workspace

    def column_runs(
        self,
        project_id: str,
        column_id: int,
        *,
        offset: int,
        limit: int,
    ) -> dict[str, Any]:
        try:
            return column_run_provenance_payload(
                self._workspace.get(project_id),
                column_id,
                offset=offset,
                limit=limit,
            )
        except KeyError as exc:
            raise ColumnRunHistoryRouteError(404, str(exc)) from exc
