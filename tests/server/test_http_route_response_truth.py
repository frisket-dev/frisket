"""HTTP-05-F5: FastAPI decorators own the current generated-stack wire truth."""

from __future__ import annotations

from pathlib import Path

from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from frisket.contracts.action import ActionCatalog, Receipt
from frisket.contracts.http.copilot import CopilotReply
from frisket.contracts.http.models import (
    ActionJob,
    ActionJobsPage,
    ActionRunCancel,
    ActionRunRows,
    ActionRunStatus,
    HttpError,
    Project,
    ProjectDelete,
    SheetDeleteResponse,
    ProjectList,
    Sheet,
    SheetData,
    SheetList,
    WorkbenchPluginRuntimeIndex,
)
from frisket.server import route_errors
from frisket.server.app import create_app


EXPECTED_ROUTE_TRUTH = {
    "v1_action_catalog": (
        "GET",
        "/api/actions/v1/catalog",
        ActionCatalog,
        {401, 500},
    ),
    "project_v1_action_catalog": (
        "GET",
        "/api/projects/{pid}/actions/v1/catalog",
        ActionCatalog,
        {401, 403, 404, 409, 500},
    ),
    "list_projects": ("GET", "/api/projects", ProjectList, {401, 500}),
    "create_project": ("POST", "/api/projects", Project, {401, 422, 500}),
    "get_project": (
        "GET",
        "/api/projects/{pid}",
        Project,
        {401, 403, 404, 409, 500},
    ),
    "update_project": (
        "PATCH",
        "/api/projects/{pid}",
        Project,
        {401, 403, 404, 409, 422, 500},
    ),
    "delete_project": (
        "DELETE",
        "/api/projects/{pid}",
        ProjectDelete,
        {401, 403, 404, 409, 422, 500},
    ),
    "list_sheets": (
        "GET",
        "/api/projects/{pid}/sheets",
        SheetList,
        {401, 403, 404, 409, 500},
    ),
    "update_sheet": (
        "PATCH",
        "/api/projects/{pid}/sheets/{sheet_id}",
        Sheet,
        {400, 401, 403, 404, 409, 422, 500},
    ),
    "delete_sheet": (
        "DELETE",
        "/api/projects/{pid}/sheets/{sheet_id}",
        SheetDeleteResponse,
        {401, 403, 404, 409, 422, 500},
    ),
    "sheet_data": (
        "GET",
        "/api/projects/{pid}/sheets/{sheet_id}/data",
        SheetData,
        {400, 401, 403, 404, 409, 422, 500},
    ),
    "action_run_status": (
        "GET",
        "/api/projects/{pid}/actions/runs/{run_id}/status",
        ActionRunStatus,
        {401, 403, 404, 409, 422, 500},
    ),
    "run_rows": (
        "GET",
        "/api/projects/{pid}/actions/runs/{run_id}/rows",
        ActionRunRows,
        {400, 401, 403, 404, 409, 422, 500},
    ),
    "cancel_run": (
        "POST",
        "/api/projects/{pid}/actions/runs/{run_id}/cancel",
        ActionRunCancel,
        {401, 403, 404, 409, 422, 500},
    ),
    "v1_receipt_lookup": (
        "GET",
        "/api/projects/{pid}/actions/v1/receipts/{receipt_id}",
        Receipt,
        {401, 403, 404, 409, 500},
    ),
    "action_jobs": (
        "GET",
        "/api/projects/{pid}/actions/jobs",
        ActionJobsPage,
        {401, 403, 404, 409, 422, 500},
    ),
    "action_job_detail": (
        "GET",
        "/api/projects/{pid}/actions/jobs/{job_id}",
        ActionJob,
        {401, 403, 404, 409, 422, 500},
    ),
    "workbench_plugins": (
        "GET",
        "/api/projects/{pid}/workbench/plugins",
        WorkbenchPluginRuntimeIndex,
        {401, 403, 404, 409, 500},
    ),
    "copilot_ep": (
        "POST",
        "/api/projects/{pid}/copilot",
        CopilotReply,
        {401, 403, 404, 409, 422, 500, 502},
    ),
}


def test_http_error_responses_projects_only_the_shared_error_model() -> None:
    helper = getattr(route_errors, "http_error_responses", None)
    assert callable(helper)
    assert helper() == {}
    assert helper(400, 401, 422, 500) == {
        400: {"model": HttpError},
        401: {"model": HttpError},
        422: {"model": HttpError},
        500: {"model": HttpError},
    }


def test_current_18_stack_routes_declare_exact_response_truth(tmp_path: Path) -> None:
    app = create_app(tmp_path / "workspace")
    routes_by_name: dict[str, list[APIRoute]] = {}
    for route in app.routes:
        if isinstance(route, APIRoute):
            routes_by_name.setdefault(route.name, []).append(route)

    assert len(EXPECTED_ROUTE_TRUTH) == 19
    for name, (method, path, model, statuses) in EXPECTED_ROUTE_TRUTH.items():
        assert len(routes_by_name.get(name, [])) == 1, name
        [route] = routes_by_name[name]
        assert route.methods == {method}, name
        assert route.path == path, name
        assert route.response_model is model, name
        assert set(route.responses) == statuses, name
        assert all(
            declaration == {"model": HttpError}
            for declaration in route.responses.values()
        ), name


def test_action_catalogs_and_validation_owned_422_stay_distinct(
    tmp_path: Path,
) -> None:
    app = create_app(tmp_path / "workspace")
    routes = {
        route.name: route
        for route in app.routes
        if isinstance(route, APIRoute)
        and route.name
        in {
            "v1_action_catalog",
            "project_v1_action_catalog",
            "create_project",
            "action_job_detail",
            "copilot_ep",
        }
    }

    assert set(routes["v1_action_catalog"].responses) == {401, 500}
    assert set(routes["project_v1_action_catalog"].responses) == {
        401,
        403,
        404,
        409,
        500,
    }
    for name in ("create_project", "action_job_detail", "copilot_ep"):
        assert routes[name].responses[422] == {"model": HttpError}

    with TestClient(app) as client:
        malformed_body = client.post("/api/projects", json={})
        malformed_path = client.get(
            "/api/projects/project-missing/actions/jobs/not-an-integer"
        )
        rejected_extra = client.post(
            "/api/projects",
            params={"unexpected": "value"},
            json={"name": "Must not be created"},
        )
        ignored_catalog_extra = client.get(
            "/api/actions/v1/catalog", params={"unexpected": "value"}
        )

    assert malformed_body.status_code == 422
    assert malformed_path.status_code == 422
    assert rejected_extra.status_code == 422
    assert ignored_catalog_extra.status_code == 200
