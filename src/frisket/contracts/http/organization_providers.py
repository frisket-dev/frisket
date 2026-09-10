"""HTTP contracts for organization-wide provider configuration."""

from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict, RootModel

from frisket.contracts.http.models import NamedCoerciveRequest, WireModel


class _CompatibleRequest(NamedCoerciveRequest):
    """Keep the existing operational request coercion at this boundary."""


class OrganizationProvider(WireModel):
    id: str
    label: str
    secret_name: str
    kind: str
    policy_fields: list[str]


class OrganizationProviderCatalog(WireModel):
    schemaVersion: Literal["frisket.provider_catalog.v1"]
    providers: list[OrganizationProvider]


class OrganizationKeyInfo(WireModel):
    provider: str
    hint: str


class OrganizationKeyList(RootModel[list[OrganizationKeyInfo]]):
    model_config = ConfigDict(strict=True)


class OrganizationKeySave(WireModel):
    provider: str
    hint: str


class OrganizationKeyValidation(WireModel):
    provider: str
    ok: bool
    reachable: bool
    status: int | None
    detail: str | None
    validation_token: str | None


class OrganizationKeyDelete(WireModel):
    deleted: bool


class OrganizationKeySaveRequest(_CompatibleRequest):
    provider: str = ""
    key: str = ""
    validation_token: str | None = None


class OrganizationKeyValidationRequest(_CompatibleRequest):
    provider: str = ""
    key: str = ""


__all__ = [
    "OrganizationKeyDelete",
    "OrganizationKeyInfo",
    "OrganizationKeyList",
    "OrganizationKeySave",
    "OrganizationKeySaveRequest",
    "OrganizationKeyValidation",
    "OrganizationKeyValidationRequest",
    "OrganizationProvider",
    "OrganizationProviderCatalog",
]
