"""HTTP contracts for generic runtime projection artifact reads."""

from __future__ import annotations

from pydantic import ConfigDict, Field, JsonValue, RootModel

from frisket.contracts.http.models import WireModel


class RuntimeProjectionArtifactRequest(WireModel):
    """Closed artifact lookup envelope with plugin-owned JSON objects."""

    model_config = ConfigDict(validate_by_alias=True, validate_by_name=False)

    projection_kind: str = Field(alias="projectionKind", min_length=1)
    artifact_id: str = Field(alias="artifactId", min_length=1)
    target: dict[str, JsonValue] = Field(default_factory=dict)
    params: dict[str, JsonValue] = Field(default_factory=dict)


class RuntimeProjectionArtifactResponse(RootModel[dict[str, JsonValue]]):
    """Lossless trusted-plugin artifact body, constrained to JSON values."""

    model_config = ConfigDict(strict=True)


__all__ = [
    "RuntimeProjectionArtifactRequest",
    "RuntimeProjectionArtifactResponse",
]
