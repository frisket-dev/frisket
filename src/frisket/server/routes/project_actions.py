"""Project action utility route registration."""

from __future__ import annotations

from fastapi import FastAPI, HTTPException

from frisket.server import schemas
from frisket.server.services.project_actions import ProjectActionUtilityService


def register_project_action_describe_routes(
    app: FastAPI,
    *,
    service: ProjectActionUtilityService,
) -> None:
    @app.get("/api/projects/{pid}/actions/describe")
    def action_describe_project(pid: str) -> dict:
        """Programmatic project snapshot (``describe_project``).

        This remains part of the stable JSON-first contract for programmatic
        callers (CLI/MCP/
        agents): ``describe_project`` is advertised by ``GET
        /api/actions/schema`` via frisket.actions.ACTION_NAMES and pinned by
        tests/test_programmatic_contract.py and
        tests/test_http_programmatic_actions_schema_describe_boundary.py;
        the endpoint catalog grants session_or_pat/viewer. Removal
        condition: remove only via a versioned frisket.actions
        schema cut that drops ``describe_project`` from ACTION_NAMES and
        retires the programmatic-contract tests and the
        ``action_describe_project`` endpoint-catalog entry together.
        """
        return service.describe_project(pid)


def register_project_action_data_routes(
    app: FastAPI,
    *,
    service: ProjectActionUtilityService,
) -> None:
    @app.get("/api/projects/{pid}/actions/sheets")
    def action_list_sheets(pid: str) -> dict:
        """Programmatic sheet/column listing (``list_sheets``).

        This remains part of the stable JSON-first programmatic contract:
        ``list_sheets`` is
        advertised by ``GET /api/actions/schema`` via
        frisket.actions.ACTION_NAMES; session_or_pat/viewer in the endpoint
        catalog. Removal condition: same versioned
        frisket.actions schema cut as ``action_describe_project`` above —
        drop ``list_sheets`` from ACTION_NAMES and retire the
        ``action_list_sheets`` endpoint-catalog entry and its route tests in
        the same change.
        """
        return service.list_sheets(pid)

    @app.post("/api/projects/{pid}/actions/read-range")
    def action_read_range(pid: str, body: schemas.ReadRangeBody) -> dict:
        """Programmatic paged cell reads (``read_range``).

        This remains part of the stable JSON-first programmatic contract:
        ``read_range`` is
        advertised by ``GET /api/actions/schema`` via
        frisket.actions.ACTION_NAMES and exercised end-to-end by
        tests/test_programmatic_contract.py; session_or_pat/editor in the
        endpoint catalog. Removal condition: same
        versioned
        frisket.actions schema cut as ``action_describe_project`` above —
        drop ``read_range`` from ACTION_NAMES and retire the
        ``action_read_range`` endpoint-catalog entry and
        tests/test_server_project_action_utility_routes.py pins together.
        """
        try:
            return service.read_range(
                pid,
                sheet_id=body.sheet_id,
                offset=body.offset,
                limit=body.limit,
                columns=body.columns,
            )
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
