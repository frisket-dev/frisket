"""HTTP contracts for workspace and organization models-gateway setup."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import ConfigDict, Field, model_validator

from frisket.contracts.http.models import WireModel
from frisket.contracts.actions.schemas._validators import nonblank


class ModelsGatewayCandidateRequest(WireModel):
    """A complete candidate pair, or an empty body for explicit Recheck."""

    model_config = ConfigDict(extra="forbid", strict=True)

    origin: Annotated[
        str | None,
        nonblank("gateway candidate values must be non-blank or omitted", strip=False),
    ] = None
    token: Annotated[
        str | None,
        nonblank("gateway candidate values must be non-blank or omitted", strip=False),
    ] = Field(default=None, repr=False)

    @model_validator(mode="after")
    def complete_pair_or_recheck(self) -> ModelsGatewayCandidateRequest:
        if (self.origin is None) != (self.token is None):
            raise ValueError("origin and token must be supplied together")
        return self


class ModelsGatewaySaveRequest(WireModel):
    origin: Annotated[
        str, nonblank("gateway save values must be non-blank", strip=False)
    ]
    token: Annotated[
        str, nonblank("gateway save values must be non-blank", strip=False)
    ] = Field(repr=False)
    validation_token: Annotated[
        str, nonblank("gateway save values must be non-blank", strip=False)
    ] = Field(repr=False)


class ModelsGatewayEngineCapability(WireModel):
    name: str
    route: str
    available: bool
    loaded: bool
    models: list[str]
    error: str | None
    contract_versions: list[str] | None = None
    revision: str | None = None
    runtime_image_id: str | None = None
    options: dict[str, bool | int | float | str | None] | None = None


class ModelsGatewayProbe(WireModel):
    ok: bool
    reachable: bool
    status: int | None
    detail: str | None
    service: Literal["frisket-models"] | None
    version: str | None
    engines: list[ModelsGatewayEngineCapability]


class ModelsGatewayStatus(WireModel):
    schemaVersion: Literal["frisket.models_gateway.v1"]
    configured: bool
    source: Literal["environment", "stored"] | None
    origin: str | None
    token_configured: bool
    token_hint: str | None
    authority: Literal["workspace", "organization"]
    can_mutate: bool
    environment_names: list[Literal["FRISKET_MODELS_URL", "FRISKET_MODELS_TOKEN"]]
    error: str | None
    probe: ModelsGatewayProbe | None


class ModelsGatewayValidationResponse(WireModel):
    schemaVersion: Literal["frisket.models_gateway_validation.v1"]
    normalized_origin: str | None
    validation_token: str | None
    probe: ModelsGatewayProbe


__all__ = [
    "ModelsGatewayCandidateRequest",
    "ModelsGatewayEngineCapability",
    "ModelsGatewayProbe",
    "ModelsGatewaySaveRequest",
    "ModelsGatewayStatus",
    "ModelsGatewayValidationResponse",
]
