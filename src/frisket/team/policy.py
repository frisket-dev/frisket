"""Fail-closed compiler for team-owned outer routes."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.routing import compile_path

from frisket.contracts.http.endpoint_catalog import (
    BASE_ENDPOINT_CATALOG,
    EndpointPolicy,
)


class TeamOuterPolicyError(RuntimeError):
    pass


def _routes(app: FastAPI) -> tuple[tuple[str, str, str], ...]:
    return tuple(
        (method.upper(), route.path, str(route.name))
        for route in app.routes
        if isinstance(route, APIRoute)
        and (route.path.startswith("/api") or route.path.startswith("/auth"))
        for method in route.methods or set()
    )


class _PolicyMiddleware(BaseHTTPMiddleware):
    def __init__(
        self,
        app: Any,
        *,
        policies: Mapping[tuple[str, str], str],
        require_user: Callable[..., dict[str, Any]],
    ):
        super().__init__(app)
        self.require_user = require_user
        self.matchers = [
            (method, compile_path(path)[0], mode)
            for (method, path), mode in policies.items()
        ]

    async def dispatch(self, request: Request, call_next):
        mode = next(
            (
                candidate
                for method, pattern, candidate in self.matchers
                if method == request.method.upper() and pattern.match(request.url.path)
            ),
            None,
        )
        if mode and mode != "public":
            try:
                request.state.team_user = self.require_user(
                    request,
                    browser=mode in {"browser_session", "admin"},
                    admin=mode == "admin",
                )
            except HTTPException as exc:
                return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        return await call_next(request)


class _ClientErrorBodyLimitMiddleware:
    LIMIT = 65_536

    def __init__(self, app: Any):
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http" or scope.get("path") != "/api/client-errors":
            await self.app(scope, receive, send)
            return
        buffered: list[dict[str, Any]] = []
        total = 0
        while True:
            message = await receive()
            if message.get("type") != "http.request":
                buffered.append(message)
                break
            total += len(message.get("body", b""))
            if total > self.LIMIT:
                body = b'{"detail":"client error report is too large"}'
                await send(
                    {
                        "type": "http.response.start",
                        "status": 413,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode()),
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": body})
                return
            buffered.append(message)
            if not message.get("more_body", False):
                break

        async def replay_receive():
            if buffered:
                return buffered.pop(0)
            return await receive()

        await self.app(scope, replay_receive, send)


def compile_team_policies(
    app: FastAPI,
    *,
    auth_declarations: Mapping[tuple[str, str, str], str],
    local_declarations: Sequence[EndpointPolicy] = (),
) -> dict[tuple[str, str], str]:
    """Compile the outer app's per-route auth policy.

    ``local_declarations`` is the TEAM-ONLY edition-scoped catalog
    contribution:
    routes this composition adds that the shared, cross-edition
    ``BASE_ENDPOINT_CATALOG`` (:mod:`frisket.contracts.http.endpoint_catalog`,
    also consumed by an external managed edition's own route-policy compiler)
    must NOT carry, because adding to that shared catalog would require
    EVERY edition importing it to have registered the identical route or
    fail closed at their own startup. Declared here, at team COMPOSITION
    time, instead -- a contribution this module alone owns and an external
    edition never sees, so it structurally cannot break that edition's
    startup. Checked first; the base catalog is still consulted for every
    other route by its unique route-name/method identity. The live outer route
    supplies the path; a base entry may be tenant-owned when the team outer
    composition fronts that same operation.
    """
    registered_routes = _routes(app)
    allowed_cross_owner_base_reuse = frozenset(
        {
            ("create_project", "POST"),
            ("list_projects", "GET"),
            ("delete_project", "DELETE"),
            ("update_project_network", "PATCH"),
            ("update_project_sensitivity", "PATCH"),
        }
    )
    registered_by_identity: dict[tuple[str, str, str], tuple[str, str, str]] = {}
    for method, path, name in registered_routes:
        identity = ("outer", name, method)
        if identity in registered_by_identity:
            raise TeamOuterPolicyError(f"duplicate registered identity {identity!r}")
        registered_by_identity[identity] = (method, path, name)

    policies: dict[tuple[str, str], str] = {}
    local_by_identity: dict[tuple[str, str, str], EndpointPolicy] = {
        (entry.route_owner, entry.route_name, entry.method): entry
        for entry in local_declarations
    }
    if len(local_by_identity) != len(local_declarations):
        raise TeamOuterPolicyError("ambiguous team-local endpoint declaration")
    for method, path, name in registered_routes:
        identity = ("outer", name, method)
        local_match = local_by_identity.get(identity)
        if local_match is not None:
            mode = local_match.auth
        elif path.startswith("/auth"):
            try:
                mode = auth_declarations[(method, path, name)]
            except KeyError as exc:
                raise TeamOuterPolicyError(
                    f"unclassified auth route {(method, path, name)}"
                ) from exc
        else:
            matches = [
                entry
                for entry in BASE_ENDPOINT_CATALOG
                if entry.route_name == name and entry.method == method
            ]
            if len(matches) != 1:
                raise TeamOuterPolicyError(
                    f"route {identity!r} at {(method, path)!r} must resolve to exactly one neutral base declaration; matches={[entry.id for entry in matches]}"
                )
            match = matches[0]
            if (
                match.route_owner != "outer"
                and (name, method) not in allowed_cross_owner_base_reuse
            ):
                raise TeamOuterPolicyError(
                    f"route {identity!r} is not an authorized cross-owner base reuse"
                )
            mode = match.auth
        key = (method, path)
        if key in policies:
            raise TeamOuterPolicyError(f"ambiguous wire template {key}")
        policies[key] = mode
    auth_routes_not_locally_declared = {
        route
        for route in registered_routes
        if route[1].startswith("/auth")
        and ("outer", route[2], route[0]) not in local_by_identity
    }
    if set(auth_declarations) != auth_routes_not_locally_declared:
        raise TeamOuterPolicyError("auth route declaration inventory is stale")
    stale_local = set(local_by_identity) - set(registered_by_identity)
    if stale_local:
        raise TeamOuterPolicyError(
            f"team-local endpoint declaration inventory is stale: {sorted(stale_local)}"
        )
    return policies


def install_outer_policy(
    app: FastAPI,
    *,
    auth_declarations: Mapping[tuple[str, str, str], str],
    require_user: Callable[..., dict[str, Any]],
    local_declarations: Sequence[EndpointPolicy] = (),
) -> None:
    policies = compile_team_policies(
        app,
        auth_declarations=auth_declarations,
        local_declarations=local_declarations,
    )
    expected = _routes(app)

    def verify() -> None:
        if _routes(app) != expected:
            raise TeamOuterPolicyError(
                "outer route inventory changed after neutral policy compilation"
            )

    app.router.on_startup.append(verify)
    app.add_middleware(_PolicyMiddleware, policies=policies, require_user=require_user)
    app.add_middleware(_ClientErrorBodyLimitMiddleware)


__all__ = ["TeamOuterPolicyError", "compile_team_policies", "install_outer_policy"]
