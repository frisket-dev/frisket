"""HTTP contracts for the local-only provider configuration surface."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import ConfigDict, Field, model_validator

from frisket.contracts.http.models import NamedCoerciveRequest, WireModel
from frisket.contracts.actions.schemas._validators import nonblank


class _CompatibleRequest(NamedCoerciveRequest):
    """Retain the route bodies' pre-contract coercion and extra-field policy."""


class ProviderKeyRequest(_CompatibleRequest):
    key: str
    validation_token: str | None = None


class LocalEndpointCreateRequest(WireModel):
    display_name: Annotated[
        str, nonblank("local endpoint identity fields cannot be blank", strip=False)
    ]
    origin: Annotated[
        str, nonblank("local endpoint identity fields cannot be blank", strip=False)
    ]
    inference_token: Annotated[
        str | None, nonblank("endpoint tokens must be non-blank or null", strip=False)
    ] = None
    provisioning_token: Annotated[
        str | None, nonblank("endpoint tokens must be non-blank or null", strip=False)
    ] = None
    edge_auth: bool = False
    pull_enabled: bool = False


class ModelServerModelPrice(WireModel):
    input: Annotated[float, Field(ge=0, allow_inf_nan=False)]
    output: Annotated[float, Field(ge=0, allow_inf_nan=False)]


class ModelServerModelEntry(WireModel):
    """One model configured on a model server: typed by hand (``entered``) or
    a listed model carrying a price. ``price`` is USD per million tokens."""

    name: Annotated[str, nonblank("model name cannot be blank")]
    entered: bool
    price: ModelServerModelPrice | None


class LocalEndpointPatchRequest(WireModel):
    model_config = ConfigDict(json_schema_extra={"minProperties": 1})

    display_name: Annotated[
        str, nonblank("display_name cannot be blank", strip=False)
    ] = ""
    inference_token: Annotated[
        str | None, nonblank("endpoint tokens must be non-blank or null", strip=False)
    ] = None
    provisioning_token: Annotated[
        str | None, nonblank("endpoint tokens must be non-blank or null", strip=False)
    ] = None
    edge_auth: bool = False
    pull_enabled: bool = False
    models: list[ModelServerModelEntry] = Field(default_factory=list)

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


class EngineSetupRequest(_CompatibleRequest):
    setup_ref: str


class LocalProviderPrice(WireModel):
    input: float
    output: float


class LocalProviderModel(WireModel):
    id: str
    label: str
    price: LocalProviderPrice | None
    local: bool
    # A model id typed by hand for a model server, not listed by it.
    entered: bool = False


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
    local: bool
    configured_models: list[ModelServerModelEntry]
    installed_models: list[str] = Field(default_factory=list)
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
    network: Literal["off"] = "off"


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


class ModelPullCapabilities(WireModel):
    cancel: bool
    retry: bool
    remove: bool


class ModelPull(WireModel):
    schemaVersion: Literal["frisket.model_pull.v4"]
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
    operation_kind: Literal["artifact", "local_model", "engine_setup"]
    display_name: str
    capabilities: ModelPullCapabilities


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
    validation_token: str = ""


__all__ = [
    "ArtifactPullRequest",
    "ArtifactUninstallRequest",
    "EngineSetupRequest",
    "LocalEndpointCreateRequest",
    "LocalEndpointCatalog",
    "LocalEndpointDiscoveryCandidate",
    "LocalEndpointDiscoveryResponse",
    "LocalEndpointPatchRequest",
    "LocalProviderCatalog",
    "ModelPull",
    "ModelPullCapabilities",
    "ModelPullListResponse",
    "ModelPullStartResponse",
    "ProviderKeyRequest",
    "ProviderValidationRequest",
    "ProviderValidationResponse",
]
