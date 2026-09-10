"""Public response contract for the team-only admin overview."""

from __future__ import annotations

from pydantic import ConfigDict, Field, JsonValue

from frisket.contracts.http.models import WireModel


class AdminOverviewTotals(WireModel):
    """The cross-edition totals plus edition-owned additive totals."""

    model_config = ConfigDict(extra="allow")

    # Pydantic validates extension values as JSON while retaining them through
    # FastAPI's response filter for a managed composition's renderer.
    __pydantic_extra__: dict[str, JsonValue] = Field(init=False)

    orgs: int
    # These open-team counts are absent from the managed producer. Keeping the
    # annotation non-null while defaulting to None makes each optional only by
    # omission: exclude_unset preserves managed bytes and explicit null fails.
    users: int = Field(default=None)
    projects: int = Field(default=None)
    pending_invites: int


class AdminOverviewResponse(WireModel):
    """Strict shared intersection with additive, JSON-only edition extensions."""

    model_config = ConfigDict(extra="allow")

    __pydantic_extra__: dict[str, JsonValue] = Field(init=False)

    totals: AdminOverviewTotals


__all__ = ["AdminOverviewResponse", "AdminOverviewTotals"]
