"""Response helpers shared by the open team route registrars."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Response
from frisket.team.invite_service import InviteConflict, InviteForbidden, InviteNotFound


def iso_z(value: Any) -> str | None:
    if value is None:
        return None
    text = value.isoformat() if hasattr(value, "isoformat") else str(value)
    return text.replace("+00:00", "Z")


def invite_http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, InviteNotFound):
        return HTTPException(404, str(exc))
    if isinstance(exc, InviteForbidden):
        return HTTPException(403, str(exc))
    if isinstance(exc, InviteConflict):
        return HTTPException(exc.status_code, str(exc))
    return HTTPException(400, str(exc))


def set_session_cookie(
    response: Response, *, name: str, value: str, secure: bool
) -> None:
    response.set_cookie(
        name,
        value,
        httponly=True,
        secure=secure,
        samesite="lax",
        max_age=30 * 86400,
    )
