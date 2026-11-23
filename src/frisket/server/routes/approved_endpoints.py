"""Manage approved endpoints: the workspace's list of approved destinations."""

from __future__ import annotations

from fastapi import FastAPI

from frisket.server.route_errors import http_error_responses, register_typed_error
from frisket.server.workspace import Workspace
from frisket.service_access import (
    ApprovedEndpoints,
    InvalidApprovedEndpointsError,
    read_approved_endpoints,
    write_approved_endpoints,
)


def register_approved_endpoint_routes(app: FastAPI, *, workspace: Workspace) -> None:
    register_typed_error(app, InvalidApprovedEndpointsError, 409)

    @app.get(
        "/api/approved-endpoints",
        response_model=ApprovedEndpoints,
        name="list_approved_endpoints",
        responses=http_error_responses(409, 500),
    )
    def list_approved_endpoints() -> ApprovedEndpoints:
        return ApprovedEndpoints(endpoints=read_approved_endpoints(workspace.root))

    @app.put(
        "/api/approved-endpoints",
        response_model=ApprovedEndpoints,
        name="replace_approved_endpoints",
        responses=http_error_responses(422, 500),
    )
    def replace_approved_endpoints(body: ApprovedEndpoints) -> ApprovedEndpoints:
        return ApprovedEndpoints(
            endpoints=write_approved_endpoints(workspace.root, body)
        )
