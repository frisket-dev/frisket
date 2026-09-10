"""Public HTTP contracts for per-project configuration."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from frisket.contracts.http.models import WireModel


def _optional_by_omission(schema: dict[str, object]) -> None:
    """Keep only non-required nullable fields optional by omission.

    A required ``T | None`` field is a different wire contract from an
    optional ``T`` field: the former must retain its JSON null branch.
    """

    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return
    required = set(schema.get("required", ()))
    for name, value in properties.items():
        if name in required:
            continue
        if not isinstance(value, dict):
            continue
        variants = value.get("anyOf")
        if not isinstance(variants, list):
            continue
        non_null = [item for item in variants if item != {"type": "null"}]
        if len(non_null) == 1 and len(non_null) < len(variants):
            replacement = non_null[0]
            if isinstance(replacement, dict):
                del value["anyOf"]
                for key, child in replacement.items():
                    value.setdefault(key, child)


class CoerciveProjectConfigRequest(BaseModel):
    """Preserve the legacy body behavior: coercion and ignored extra keys."""

    model_config = ConfigDict(json_schema_extra=_optional_by_omission)


class ProjectRetentionRequest(CoerciveProjectConfigRequest):
    default_evidence: str | None = None
    pin_evidence_by_default: bool | None = None
    no_compact: bool | None = None


class ProjectNetworkRequest(CoerciveProjectConfigRequest):
    mode: str


class ProjectSettingsRequest(CoerciveProjectConfigRequest):
    """Settings are the one strict body: misspellings must not be ignored."""

    model_config = ConfigDict(extra="forbid", json_schema_extra=_optional_by_omission)

    media_allow_private_hosts: bool | None = None


class ProjectProviderKeyRequest(CoerciveProjectConfigRequest):
    provider: str
    key: str
    spend_cap_usd: float | None = None
    validation_token: str | None = None


class ProjectProviderKeyValidateRequest(CoerciveProjectConfigRequest):
    provider: str
    key: str | None = None


class ProjectSecretRequest(CoerciveProjectConfigRequest):
    name: str
    value: str


class ProjectRetentionResponse(WireModel):
    schema_version: Literal["frisket.project_retention_policy.v1"] = Field(
        alias="schemaVersion"
    )
    default_evidence: Literal["compactable", "pinned", "materialized"]
    pin_evidence_by_default: bool
    no_compact: bool
    supported_default_evidence: list[Literal["compactable", "pinned", "materialized"]]


class ProjectNetworkResponse(WireModel):
    schema_version: Literal["frisket.project_network_policy.v1"] = Field(
        alias="schemaVersion"
    )
    mode: Literal["inherit", "on", "off"]
    # Materialized in every project bundle; None means the built-in org
    # default of "on" remains in effect.
    org_default: Literal["on", "off"] | None
    effective: Literal["on", "off"]


class ProjectSettingsResponse(WireModel):
    media_allow_private_hosts: bool
    media_allow_private_hosts_locked: bool


class ProjectCompactResponse(WireModel):
    skipped: bool
    reason: str | None
    results_pruned: int
    blobs_removed: int
    bytes_freed: int
    db_bytes_before: int
    db_bytes_after: int
    db_bytes_reclaimed: int


class ProjectProviderKey(WireModel):
    id: str
    label: str
    secret_name: str
    kind: str
    policy_fields: list[str]
    configured: bool
    hint: str | None
    spend_cap_usd: float | None
    spent_usd: int | float
    unmetered_calls: int
    updated_at: str | None


class ProjectProviderKeyCatalogResponse(WireModel):
    schema_version: Literal["frisket.project_provider_keys.v1"] = Field(
        alias="schemaVersion"
    )
    project_id: str = Field(alias="projectId")
    providers: list[ProjectProviderKey]


class ProjectProviderKeyValidationResponse(WireModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        populate_by_name=True,
        json_schema_extra=_optional_by_omission,
    )

    provider: str
    ok: bool
    reachable: bool
    status: int | None
    detail: str | None
    # Omitted unless this request's candidate key validates.  ``str`` (rather
    # than ``str | None``) rejects an explicit null at the model boundary.
    validation_token: str = Field(default=None)

    @model_validator(mode="after")
    def validation_token_requires_success(self) -> ProjectProviderKeyValidationResponse:
        if self.validation_token is not None and not self.ok:
            raise ValueError(
                "validation_token is only issued for a successful validation"
            )
        return self


class ProjectProviderKeyDeleteResponse(WireModel):
    ok: bool
    deleted: bool
    provider: str


class ProjectSecretConsumer(WireModel):
    kind: Literal["plugin", "source", "job", "mcp_connector"]
    id: str


class ProjectSecret(WireModel):
    name: str
    hint: str
    configured: bool
    updated_at: str = Field(alias="updatedAt")
    consumers: list[ProjectSecretConsumer]


class ProjectSecretConflict(WireModel):
    # Exact existing project_secret_migration_conflicts select and Settings UI
    # fields. These intentionally remain snake_case legacy wire bytes.
    plugin_id: str
    name: str
    hint: str | None
    status: str
    created_at: str


class ProjectSecretCatalogResponse(WireModel):
    schema_version: Literal["frisket.project_secrets.v1"] = Field(alias="schemaVersion")
    project_id: str = Field(alias="projectId")
    secrets: list[ProjectSecret]
    conflicts: list[ProjectSecretConflict]


class ProjectSecretDeleteResponse(WireModel):
    ok: bool
    deleted: bool
    name: str
