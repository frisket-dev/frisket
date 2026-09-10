"""Action run row inspector service for local server routes."""

from __future__ import annotations

from typing import Any

from frisket.server.run_payloads import action_run_rows_payload
from frisket.server.workspace import Workspace
from frisket.server.route_errors import RouteError


class ActionRunRowsRouteError(RouteError):
    pass


class ActionRunRowsService:
    def __init__(self, workspace: Workspace):
        self._workspace = workspace

    def run_rows(
        self,
        project_id: str,
        run_id: int,
        *,
        offset: int = 0,
        limit: int = 50,
        status: str | None = None,
    ) -> dict[str, Any]:
        try:
            return action_run_rows_payload(
                self._workspace.get(project_id),
                run_id,
                offset=offset,
                limit=limit,
                status=status,
            )
        except KeyError as exc:
            raise ActionRunRowsRouteError(404, str(exc)) from exc
        except ValueError as exc:
            raise ActionRunRowsRouteError(400, str(exc)) from exc
