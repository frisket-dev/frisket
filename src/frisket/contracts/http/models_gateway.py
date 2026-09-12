"""HTTP contracts for workspace and organization models-gateway setup."""

from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

from frisket.contracts.http.models import WireModel


class ModelsGatewayCandidateRequest(WireModel):
    """A complete candidate pair, or an empty body for explicit Recheck."""

    model_config = ConfigDict(extra="forbid", strict=True)

    origin: str | None = None
    token: str | None = Field(default=None, repr=False)

    @field_validator("origin", "token")
    @classmethod
    def non_blank_when_present(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("gateway candidate values must be non-blank or omitted")
        return value

    @model_validator(mode="after")
    def complete_pair_or_recheck(self) -> ModelsGatewayCandidateRequest:
        if (self.origin is None) != (self.token is None):
            raise ValueError("origin and token must be supplied together")
        return self


class ModelsGatewaySaveRequest(WireModel):
    origin: str
    token: str = Field(repr=False)
    validation_token: str = Field(repr=False)

    @field_validator("origin", "token", "validation_token")
    @classmethod
    def non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("gateway save values must be non-blank")
        return value


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
