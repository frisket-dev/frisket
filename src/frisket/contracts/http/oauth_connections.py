"""Closed browser projection for Team OAuth connected-account inventory."""

from __future__ import annotations

from frisket.contracts.http.models import WireModel


class OAuthConnection(WireModel):
    """Public metadata only; token material never crosses this boundary."""

    id: str
    connection_id: str | None = None
    provider: str
    external_subject: str | None = None
    external_email: str | None = None
    scopes: list[str] | None = None
    # These established hosted fields are harmless display metadata when an
    # edition supplies them. Defaults preserve the Team producer's current
    # omissions under response_model_exclude_unset.
    token_type: str | None = None
    refresh_token_hint: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    revoked_at: str | None = None


class OAuthConnectionList(WireModel):
    connections: list[OAuthConnection]


__all__ = ["OAuthConnection", "OAuthConnectionList"]
