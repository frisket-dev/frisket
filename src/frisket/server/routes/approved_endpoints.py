"""Manage approved endpoints: the workspace's list of approved destinations.

Local registers these routes for its single user. Team registers them for the
organization, readable by members and replaceable by organization owners, the
same role that manages organization provider keys.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI, Request

from frisket.server.route_errors import http_error_responses, register_typed_error
from frisket.service_access import (
    ApprovedEndpoints,
    InvalidApprovedEndpointsError,
    read_approved_endpoints,
    write_approved_endpoints,
)


def _anyone(_request: Request) -> None:
    return None


@dataclass(frozen=True)
class ApprovedEndpointAccess:
    """Where an edition serves the routes and who may read or replace the list."""

    path: str = "/api/approved-endpoints"
    name_prefix: str = ""
    require_reader: Callable[[Request], object] = _anyone
    require_manager: Callable[[Request], object] = _anyone


def register_approved_endpoint_routes(
    app: FastAPI,
    *,
    workspace_root: Path,
    access: ApprovedEndpointAccess,
) -> None:
    path, name_prefix = access.path, access.name_prefix
    require_reader, require_manager = access.require_reader, access.require_manager
    register_typed_error(app, InvalidApprovedEndpointsError, 409)

    @app.get(
        path,
        response_model=ApprovedEndpoints,
        name=f"list_{name_prefix}approved_endpoints",
        responses=http_error_responses(401, 403, 409, 500),
    )
    def list_approved_endpoints(request: Request) -> ApprovedEndpoints:
        require_reader(request)
        return ApprovedEndpoints(endpoints=read_approved_endpoints(workspace_root))

    @app.put(
        path,
        response_model=ApprovedEndpoints,
        name=f"replace_{name_prefix}approved_endpoints",
        responses=http_error_responses(401, 403, 422, 500),
    )
    def replace_approved_endpoints(
        request: Request, body: ApprovedEndpoints
    ) -> ApprovedEndpoints:
        require_manager(request)
        return ApprovedEndpoints(
            endpoints=write_approved_endpoints(workspace_root, body)
        )
