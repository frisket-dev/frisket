"""Open session/me/instance HTTP binding.

These are the identity endpoints, and they are open: `/api/me` answers "who am
I and what instance am I on", `/api/instance` brands the pre-sign-in page, and
neither carries a balance. The open payload has no commerce field at all.

An edition adds its own `/api/me` fields through `session.me_contributions()`
(frisket.team.session_service.IdentitySessionService) — an external managed
composition returns its customer's credit balance there. This module never
names that field: it only merges whatever the edition contributes, so the
open wire shape stays commerce-free by construction.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from frisket.contracts.http.identity_profile import (
    IdentityProfileResponse,
    ProfilePatchRequest,
)
from frisket.contracts.http.browser_auth import BrowserAuthLogoutResponse
from frisket.contracts.http.instance_runtime import InstanceInfoResponse
from frisket.server.route_errors import http_error_responses
from frisket.team.session_service import IdentitySessionService


PolicyActorCallback = Callable[[Request], dict[str, Any]]


def _me_payload(
    session: IdentitySessionService,
    user: dict[str, Any],
) -> dict[str, Any]:
    payload = {
        "email": user["email"],
        "display_name": user.get("name"),
        "avatar_seed": f"user:{user['email']}",
        "cost_preapproval_usd": user.get("cost_preapproval_usd"),
        "instance": session.org_identity(user),
    }
    payload.update(session.me_contributions(user))
    return payload


def register_session_routes(
    app: FastAPI,
    *,
    session: IdentitySessionService,
    session_cookie: str,
    require_user: PolicyActorCallback,
    require_browser_user: PolicyActorCallback,
) -> None:
    @app.post(
        "/auth/logout",
        response_model=BrowserAuthLogoutResponse,
        responses=http_error_responses(500),
    )
    def logout(request: Request) -> Response:
        session.revoke_browser_session(request.cookies.get(session_cookie, ""))
        resp = JSONResponse(BrowserAuthLogoutResponse(ok=True).model_dump())
        resp.delete_cookie(session_cookie)
        return resp

    @app.get(
        "/api/me",
        response_model=IdentityProfileResponse,
        response_model_exclude_unset=True,
        responses=http_error_responses(401, 500, 503),
    )
    def me(request: Request) -> IdentityProfileResponse:
        user = require_user(request)
        return IdentityProfileResponse.model_validate(_me_payload(session, user))

    @app.get(
        "/api/instance",
        response_model=InstanceInfoResponse,
        response_model_exclude_unset=True,
        responses=http_error_responses(500),
    )
    def instance_info() -> InstanceInfoResponse:
        # Unauthenticated: the sign-in page needs branding before there is a
        # session to read an org from.
        return InstanceInfoResponse.model_validate(session.instance_identity())

    @app.patch(
        "/api/me/profile",
        response_model=IdentityProfileResponse,
        response_model_exclude_unset=True,
        responses=http_error_responses(400, 401, 403, 422, 500, 503),
    )
    def update_profile(
        request: Request,
        body: ProfilePatchRequest,
    ) -> IdentityProfileResponse:
        try:
            user = require_browser_user(request)
            updated = session.update_profile(
                user,
                display_name=body.display_name,
                update_display_name="display_name" in body.model_fields_set,
                cost_preapproval_usd=(
                    body.cost_preapproval_usd
                    if "cost_preapproval_usd" in body.model_fields_set
                    else None
                ),
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return IdentityProfileResponse.model_validate(_me_payload(session, updated))
