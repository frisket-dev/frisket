"""HTTP contracts for the shared signed-in identity/profile pair.

The known identity object stays closed except for named edition contributions at
its root.  Contributions are recursive JSON values: hosted can retain its exact
``balance_credits`` number without allowing arbitrary Python objects onto the
wire. Edition fields are optional by omission and keep their producer names.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, JsonValue, RootModel

from frisket.contracts.http.models import WireModel


class IdentityEditionValue(RootModel[JsonValue]):
    """One edition-owned recursive JSON response value."""

    model_config = ConfigDict(strict=True)


class IdentityInstance(WireModel):
    display_name: str
    welcome_message: str | None
    support_contact: str | None


class IdentityProfileResponse(BaseModel):
    """Shared identity fields plus strictly JSON edition-owned root fields."""

    model_config = ConfigDict(extra="allow", strict=True)
    __pydantic_extra__: dict[str, IdentityEditionValue] = Field(init=False)

    email: str
    display_name: str | None
    avatar_seed: str
    instance: IdentityInstance
    cost_preapproval_usd: str | None


class ProfilePatchRequest(BaseModel):
    """Keep the route's existing tolerant body coercion and extra-key posture."""

    display_name: str | None = Field(default=None, max_length=200)
    cost_preapproval_usd: str | None = None


__all__ = [
    "IdentityEditionValue",
    "IdentityInstance",
    "IdentityProfileResponse",
    "ProfilePatchRequest",
]
