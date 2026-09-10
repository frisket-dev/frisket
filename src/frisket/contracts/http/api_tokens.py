"""HTTP contracts for browser-managed organization API tokens.

The plaintext token is returned only by the create operation.  Inventory rows
carry display and audit facts already owned by the authenticated user and the
``api_tokens`` table; they never expose the stored hash or organization id.
"""

from __future__ import annotations

from pydantic import ConfigDict, RootModel

from frisket.contracts.http.models import WireModel


class ApiTokenCreateRequest(WireModel):
    name: str = ""


class ApiTokenCreateResponse(WireModel):
    id: int
    name: str
    token: str
    prefix: str


class ApiTokenInfo(WireModel):
    id: int
    user_id: int
    created_by: str
    name: str
    prefix: str
    created_at: str | None
    last_used_at: str | None
    revoked: bool


class ApiTokenList(RootModel[list[ApiTokenInfo]]):
    model_config = ConfigDict(strict=True)


class ApiTokenRevokeResponse(WireModel):
    ok: bool
    revoked: bool


__all__ = [
    "ApiTokenCreateRequest",
    "ApiTokenCreateResponse",
    "ApiTokenInfo",
    "ApiTokenList",
    "ApiTokenRevokeResponse",
]
