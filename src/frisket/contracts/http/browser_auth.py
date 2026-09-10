"""Typed browser-session authentication HTTP contracts."""

from __future__ import annotations

from typing import Literal

from frisket.contracts.http.models import WireModel


class BrowserAuthRequestLinkRequest(WireModel):
    email: str = ""


class BrowserAuthRequestLinkResponse(WireModel):
    sent: Literal[True]


class BrowserAuthPasswordLoginRequest(WireModel):
    email: str = ""
    password: str = ""


class BrowserAuthPasswordLoginResponse(WireModel):
    ok: Literal[True]


class BrowserAuthCompletePasswordRequest(WireModel):
    password: str = ""


class BrowserAuthCompletePasswordResponse(WireModel):
    ok: Literal[True]


class BrowserAuthLogoutResponse(WireModel):
    ok: Literal[True]


__all__ = [
    "BrowserAuthCompletePasswordRequest",
    "BrowserAuthCompletePasswordResponse",
    "BrowserAuthPasswordLoginRequest",
    "BrowserAuthPasswordLoginResponse",
    "BrowserAuthLogoutResponse",
    "BrowserAuthRequestLinkRequest",
    "BrowserAuthRequestLinkResponse",
]
