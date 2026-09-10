"""HTTP registrar for open team organization and project invitations."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlencode

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from frisket.contracts.http.browser_auth import (
    BrowserAuthCompletePasswordRequest,
    BrowserAuthCompletePasswordResponse,
)
from frisket.contracts.http.project_collaboration import (
    CreateProjectInviteRequest,
    ProjectInviteCreate,
    ProjectInviteList,
    ProjectInviteRevoke,
)
from frisket.server.route_errors import http_error_responses
from frisket.team.identity_service import IdentityAuthService
from frisket.team.invite_service import (
    InviteConflict,
    InviteForbidden,
    InviteNotFound,
    InviteService,
)


class InviteBody(BaseModel):
    email: str = Field(max_length=320)
    role: str = "viewer"


RequireUser = Callable[[Request], dict[str, Any]]
SendMail = Callable[[str, str], Awaitable[bool]]


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    text = value.isoformat() if hasattr(value, "isoformat") else str(value)
    return text.replace("+00:00", "Z")


def _response(invite: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": invite["id"],
        "project_id": invite["slug"],
        "email": invite["email"],
        "role": invite["role"],
        **{
            key: _iso(invite.get(key))
            for key in ("created_at", "expires_at", "accepted_at", "revoked_at")
        },
    }


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, InviteNotFound):
        return HTTPException(404, str(exc))
    if isinstance(exc, InviteForbidden):
        return HTTPException(403, str(exc))
    if isinstance(exc, InviteConflict):
        return HTTPException(exc.status_code, str(exc))
    return HTTPException(400, str(exc))


def _set_cookie(response: Response, *, name: str, value: str, secure: bool) -> None:
    response.set_cookie(
        name, value, httponly=True, secure=secure, samesite="lax", max_age=30 * 86400
    )


def register_invite_routes(
    app: FastAPI,
    *,
    invites: InviteService,
    auth: IdentityAuthService,
    require_user: RequireUser,
    require_admin: RequireUser,
    base_url: str,
    session_cookie: str,
    secure_cookie: bool,
    send_mail: SendMail,
) -> None:
    """Register S2-I routes before mounting the protected core application."""

    @app.post("/api/admin/users/invite")
    async def admin_invite_user(request: Request, body: InviteBody) -> dict[str, Any]:
        actor = require_admin(request)
        try:
            invited = invites.create_org_invite(
                email=body.email,
                actor_user_id=int(actor["id"]),
                operator_actor=actor.get("auth") == "operator",
            )
        except (InviteConflict, InviteForbidden, InviteNotFound, ValueError) as exc:
            raise _error(exc) from exc
        token = auth.create_magic_link(invited["email"])
        await send_mail(invited["email"], f"{base_url}/auth/callback?token={token}")
        return {"sent": True, **invited}

    @app.delete("/api/admin/users/invites/{email}")
    def admin_revoke_invite(email: str, request: Request) -> dict[str, bool]:
        actor = require_admin(request)
        try:
            revoked = invites.revoke_org_invite(
                email=email,
                actor_user_id=int(actor["id"]),
                operator_actor=actor.get("auth") == "operator",
            )
        except (InviteConflict, InviteForbidden, InviteNotFound, ValueError) as exc:
            raise _error(exc) from exc
        return {"revoked": revoked}

    @app.get(
        "/api/projects/{pid}/invites",
        response_model=ProjectInviteList,
        response_model_exclude_unset=True,
        responses=http_error_responses(401, 403, 404, 500),
    )
    def list_project_invites(pid: str, request: Request) -> dict[str, Any]:
        actor = require_user(request)
        try:
            rows = invites.list_project_invites(
                actor_user_id=int(actor["id"]), slug=pid
            )
        except (InviteConflict, InviteForbidden, InviteNotFound, ValueError) as exc:
            raise _error(exc) from exc
        return {"invites": [_response(row) for row in rows]}

    @app.post(
        "/api/projects/{pid}/invites",
        response_model=ProjectInviteCreate,
        response_model_exclude_unset=True,
        responses=http_error_responses(400, 401, 403, 404, 422, 500),
    )
    async def create_project_invite(
        pid: str, request: Request, body: CreateProjectInviteRequest
    ) -> dict[str, Any]:
        actor = require_user(request)
        try:
            invite_row, token = invites.create_project_invite(
                actor_user_id=int(actor["id"]),
                slug=pid,
                email=body.email,
                role=body.role,
            )
        except (InviteConflict, InviteForbidden, InviteNotFound, ValueError) as exc:
            raise _error(exc) from exc
        await send_mail(
            invite_row["email"], f"{base_url}/auth/project-invites/{token}/accept"
        )
        return {"sent": True, "invite": _response(invite_row)}

    @app.delete(
        "/api/projects/{pid}/invites/{invite_id}",
        response_model=ProjectInviteRevoke,
        response_model_exclude_unset=True,
        responses=http_error_responses(401, 403, 404, 422, 500),
    )
    def revoke_project_invite(
        pid: str, invite_id: int, request: Request
    ) -> dict[str, bool]:
        actor = require_user(request)
        try:
            revoked = invites.revoke_project_invite(
                actor_user_id=int(actor["id"]), slug=pid, invite_id=invite_id
            )
        except (InviteConflict, InviteForbidden, InviteNotFound, ValueError) as exc:
            raise _error(exc) from exc
        return {"ok": True, "revoked": revoked}

    def accept(token: str, *, redirect: bool, password: str | None = None) -> Response:
        try:
            accepted = invites.accept_project_invite(token, password=password)
        except (InviteConflict, InviteForbidden, InviteNotFound, ValueError) as exc:
            raise _error(exc) from exc
        if accepted is None:
            raise HTTPException(404, "project invite not found")
        if redirect:
            response: Response = RedirectResponse("/", status_code=302)
        else:
            response = JSONResponse(
                BrowserAuthCompletePasswordResponse(ok=True).model_dump()
            )
        _set_cookie(
            response,
            name=session_cookie,
            value=str(accepted["session"]),
            secure=secure_cookie,
        )
        return response

    @app.post(
        "/auth/project-invites/{token}/accept",
        response_model=BrowserAuthCompletePasswordResponse,
        responses=http_error_responses(400, 403, 404, 410, 422, 500),
    )
    def accept_project_invite(
        token: str, body: BrowserAuthCompletePasswordRequest
    ) -> Response:
        return accept(token, redirect=False, password=body.password)

    @app.get("/auth/project-invites/{token}/accept")
    def accept_project_invite_link(token: str) -> Response:
        password_required = invites.project_invite_requires_password(token)
        if password_required is None:
            raise HTTPException(404, "project invite not found")
        if password_required:
            return RedirectResponse(
                f"/?{urlencode({'auth_kind': 'project-invite', 'auth_token': token})}",
                status_code=302,
            )
        return accept(token, redirect=True)


__all__ = ["register_invite_routes"]
