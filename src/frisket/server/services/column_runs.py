"""Column run history service for local server routes."""

from __future__ import annotations

import json
from typing import Any

from frisket.engine.executor.map_rows_action import (
    bound_typed_program_request_from_runner_spec,
    normalized_typed_request_identity,
)
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

    def reviewed_run_revision(
        self,
        project_id: str,
        column_id: int,
        run_id: int,
    ) -> dict[str, Any]:
        project = self._workspace.get(project_id)
        run = project.db.execute(
            "SELECT runs.* FROM runs "
            "WHERE runs.id=? AND EXISTS ("
            "SELECT 1 FROM results WHERE results.run_id=runs.id "
            "AND results.column_id=?"
            ")",
            (run_id, column_id),
        ).fetchone()
        if run is None:
            raise ColumnRunHistoryRouteError(404, "no such run for this column")
        reviewed_row_ids = [
            int(row["row_id"])
            for row in project.db.execute(
                "SELECT DISTINCT row_id FROM results "
                "WHERE run_id=? AND column_id=? AND review_decision IS NOT NULL "
                "ORDER BY row_id",
                (run_id, column_id),
            )
        ]
        if not reviewed_row_ids:
            raise ColumnRunHistoryRouteError(
                409,
                "Review at least one result before revising this run.",
            )
        try:
            stored = json.loads(run["params"] or "{}")
            if not isinstance(stored, dict):
                raise ValueError("stored run spec is not an object")
            bound = bound_typed_program_request_from_runner_spec(
                stored,
                project=project,
            )
            if bound is None:
                raise ValueError("unsupported action family")
            draft = normalized_typed_request_identity(bound)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ColumnRunHistoryRouteError(
                409,
                "This run cannot be revised with the current action form.",
            ) from exc
        draft.pop("replace_existing", None)
        draft.pop("implementation_identity", None)
        draft["scope"] = {
            "kind": "sheet_rows",
            "sheet_id": int(run["sheet_id"]),
            "row_ids": reviewed_row_ids,
        }
        return {
            "schema_version": "frisket.reviewed_run_revision.v1",
            "source_run_id": run_id,
            "reviewed_rows": len(reviewed_row_ids),
            "draft": draft,
        }
