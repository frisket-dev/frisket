"""HTTP contracts for organization environment and media operations."""

from __future__ import annotations

from pydantic import ConfigDict, RootModel

from frisket.contracts.http.models import WireModel


class OrganizationEnvInfo(WireModel):
    name: str
    hint: str
    # Hosted deployments persist this additive fact. Open team does not, so
    # absence is honest and distinct from an explicitly unknown timestamp.
    created_at: str | None = None


class OrganizationEnvList(RootModel[list[OrganizationEnvInfo]]):
    model_config = ConfigDict(strict=True)


class OrganizationEnvSaveRequest(WireModel):
    name: str
    value: str


class OrganizationEnvSave(WireModel):
    name: str
    # Open team returns the key hint; hosted returns an additive success bit.
    # Each is optional by omission without admitting JSON null on the wire.
    hint: str = None  # type: ignore[assignment]
    ok: bool = None  # type: ignore[assignment]


class OrganizationEnvDelete(WireModel):
    deleted: bool
    ok: bool = None  # type: ignore[assignment]


class OrganizationMediaProxyStatus(WireModel):
    configured: bool
    connected: bool | None
    can_configure: bool


__all__ = [
    "OrganizationEnvDelete",
    "OrganizationEnvInfo",
    "OrganizationEnvList",
    "OrganizationEnvSave",
    "OrganizationEnvSaveRequest",
    "OrganizationMediaProxyStatus",
]
