"""HTTP contracts for the local-only provider configuration surface."""

from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict, field_validator, model_validator

from frisket.contracts.http.models import NamedCoerciveRequest, WireModel


class _CompatibleRequest(NamedCoerciveRequest):
    """Retain the route bodies' pre-contract coercion and extra-field policy."""


class ProviderKeyRequest(_CompatibleRequest):
    key: str
    validation_token: str | None = None


class LocalEndpointCreateRequest(WireModel):
    display_name: str
    origin: str
    inference_token: str | None = None
    provisioning_token: str | None = None
    edge_auth: bool = False
    pull_enabled: bool = False

    @field_validator("display_name", "origin")
    @classmethod
    def non_blank_identity(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("local endpoint identity fields cannot be blank")
        return value

    @field_validator("inference_token", "provisioning_token")
    @classmethod
    def non_blank_token(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("endpoint tokens must be non-blank or null")
        return value


class LocalEndpointPatchRequest(WireModel):
    model_config = ConfigDict(json_schema_extra={"minProperties": 1})

    display_name: str = ""
    inference_token: str | None = None
    provisioning_token: str | None = None
    edge_auth: bool = False
    pull_enabled: bool = False

    @field_validator("display_name")
    @classmethod
    def non_blank_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("display_name cannot be blank")
        return value

    @field_validator("inference_token", "provisioning_token")
    @classmethod
    def non_blank_token(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("endpoint tokens must be non-blank or null")
        return value

    @model_validator(mode="after")
    def at_least_one_change(self) -> LocalEndpointPatchRequest:
        if not self.model_fields_set:
            raise ValueError("local endpoint patch must include at least one field")
        return self


class LocalEndpointDiscoveryCandidate(WireModel):
    label: str
    origin: str
    outcome: Literal["added", "already_added", "not_found"]


class LocalEndpointDiscoveryResponse(WireModel):
    candidates: list[LocalEndpointDiscoveryCandidate]


class ProviderValidationRequest(_CompatibleRequest):
    key: str | None = None


class ArtifactPullRequest(_CompatibleRequest):
    ref: str
    unpinned_acknowledged: bool = False


class ArtifactUninstallRequest(_CompatibleRequest):
    ref: str


class LocalProviderPrice(WireModel):
    input: float
    output: float


class LocalProviderModel(WireModel):
    id: str
    label: str
    price: LocalProviderPrice | None
    local: bool


class PlatformProvider(WireModel):
    id: str
    label: str
    kind: Literal["platform_api"]
    models: list[LocalProviderModel]
    configured: bool
    source: Literal["env", "local_file"] | None
    hint: str | None


class LocalHttpEndpointProvider(WireModel):
    endpoint_id: str
    label: str
    kind: Literal["local_http"]
    read_only: bool
    models: list[LocalProviderModel]
    reachable: bool
    origin: str
    authority: Literal["instance", "organization"]
    source: Literal["stored", "environment"]
    detail: str | None
    installed_models: list[str] = None
    protocol: Literal["ollama_native", "openai_compatible", "unknown"]
    auth_status: Literal["ok", "unauthorized", "unenforced", "unknown"]
    token_configured: bool
    provisioning_token_configured: bool
    edge_auth: bool
    pull_enabled: bool


class LocalProviderCatalog(WireModel):
    schemaVersion: Literal["frisket.providers.v1"]
    tier: Literal["local"]
    providers: list[PlatformProvider | LocalHttpEndpointProvider]
    network: Literal["off"] = None


class LocalEndpointCatalog(WireModel):
    schemaVersion: Literal["frisket.local_endpoints.v1"]
    endpoints: list[LocalHttpEndpointProvider]


class ModelPullError(WireModel):
    code: str | None
    message: str | None


class ModelPullArtifact(WireModel):
    kind: str
    source_url: str | None
    license: str | None
    manifest_version: str | None


class ModelPull(WireModel):
    schemaVersion: Literal["frisket.model_pull.v3"]
    id: int
    model: str
    status: Literal[
        "pending",
        "running",
        "done",
        "failed",
        "cancelled",
        "uninstalled",
    ]
    phase: str | None
    total_bytes: int | None
    completed_bytes: int | None
    error: ModelPullError | None
    resolved_digest: str | None
    resolved_size: int | None
    created_at: str
    started_at: str | None
    finished_at: str | None
    cancel_requested: bool
    endpoint_id: str | None
    endpoint_origin: str | None
    initiated_by: str | None
    artifact: ModelPullArtifact | None


class ModelPullStartResponse(WireModel):
    pull: ModelPull
    deduplicated: bool


class ModelPullListResponse(WireModel):
    pulls: list[ModelPull]


class ProviderValidationResponse(WireModel):
    provider: str
    ok: bool
    reachable: bool
    status: int | None
    detail: str | None
    validation_token: str = None


__all__ = [
    "ArtifactPullRequest",
    "ArtifactUninstallRequest",
    "LocalEndpointCreateRequest",
    "LocalEndpointCatalog",
    "LocalEndpointDiscoveryCandidate",
    "LocalEndpointDiscoveryResponse",
    "LocalEndpointPatchRequest",
    "LocalProviderCatalog",
    "ModelPull",
    "ModelPullListResponse",
    "ModelPullStartResponse",
    "ProviderKeyRequest",
    "ProviderValidationRequest",
    "ProviderValidationResponse",
]
