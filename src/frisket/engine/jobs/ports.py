"""Explicit edition credentials and admission for the worker.

Organization-scoped editions (team and cloud) supply their credential and
admission ports at composition time. Local solo runs resolve workspace/project
credentials without a control plane. Provider purchases own their accounting
independently of job completion. Ports are checked structurally at composition
time.
"""

from __future__ import annotations

import dataclasses
import enum
from collections.abc import Mapping
from typing import Any, NewType, Protocol, runtime_checkable

from frisket.execution.credential_use import ActionCredentialResolver
from frisket.ai.models.gateway_config import (
    ModelsGatewayConnection,
    resolve_models_gateway_env,
)


@runtime_checkable
class CredentialPort(Protocol):
    """Resolves the provider keys a run may use on behalf of an organization."""

    def provider_keys(
        self, *, org_id: int, control_database_url: str | None
    ) -> Mapping[str, str]:
        """Provider name -> plaintext key. Empty when the organization has none."""
        ...


@runtime_checkable
class SearchCredentialPort(Protocol):
    """Resolves only web-search keys for one trusted organization."""

    def search_provider_keys(
        self, *, org_id: int, control_database_url: str | None
    ) -> Mapping[str, str]: ...


@runtime_checkable
class ActionCredentialPort(Protocol):
    """Resolve non-LLM action credentials for a trusted queued organization."""

    def action_credential_resolver(
        self, *, org_id: int, control_database_url: str | None
    ) -> ActionCredentialResolver: ...


@runtime_checkable
class ModelsGatewayPort(Protocol):
    """Resolves an organization gateway through row-backed trusted worker context."""

    def models_gateway_connection(
        self, *, org_id: int, control_database_url: str | None
    ) -> ModelsGatewayConnection | None: ...


@runtime_checkable
class AdmissionPort(Protocol):
    """Decides whether a queued body may execute at all.

    Returns True to REFUSE. The open team worker has no admission port: it is a
    trusted runtime and runs what it is given. An external edition injects one
    because public-hosted tenants must never execute arbitrary code.
    """

    def execution_denied(self, *, body: Mapping[str, Any]) -> bool: ...


TrustedJobOrganizationId = NewType("TrustedJobOrganizationId", int)


class TrustedJobOrganizationUnavailable(enum.Enum):
    """Why a handler invocation has no organization attested by a claimed job row.

    Neither arm authorizes an execution adapter to recover an identity from
    the handler payload, the storage owner, or ambient process state. A hosted
    adapter that requires a funding organization must refuse before an effect or ledger
    mutation unless its separate, typed non-queued contract proves one.
    """

    NO_JOB_ROW = "no_job_row"
    JOB_ROW_HAS_NO_ORGANIZATION = "job_row_has_no_org"


type TrustedJobOrganization = (
    TrustedJobOrganizationId | TrustedJobOrganizationUnavailable
)


@dataclasses.dataclass(frozen=True)
class JobHandlerContext:
    """Immutable row-backed facts delivered separately from a job payload."""

    trusted_job_organization_id: TrustedJobOrganization
    job_id: int | None = None
    job_attempt: int | None = None
    handler_authority_id: str | None = None

    def __post_init__(self) -> None:
        value = self.trusted_job_organization_id
        if isinstance(value, TrustedJobOrganizationUnavailable):
            return
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(
                "trusted job org id must be a positive integer or an explicit "
                "TrustedJobOrganizationUnavailable arm"
            )

    @classmethod
    def from_claimed_job(
        cls,
        *,
        trusted_organization_id: int | None,
        job_id: int | None = None,
        job_attempt: int | None = None,
        handler_authority_id: str | None = None,
    ) -> JobHandlerContext:
        """Mint context from the worker's normalized immutable job column."""
        return cls(
            TrustedJobOrganizationUnavailable.JOB_ROW_HAS_NO_ORGANIZATION
            if trusted_organization_id is None
            else TrustedJobOrganizationId(trusted_organization_id),
            job_id=job_id,
            job_attempt=job_attempt,
            handler_authority_id=handler_authority_id,
        )

    @classmethod
    def without_job_row(cls) -> JobHandlerContext:
        """Explicit context for direct/non-queued handler invocation."""
        return cls(TrustedJobOrganizationUnavailable.NO_JOB_ROW)


class EnvironmentModelsGatewayPort:
    """Open default preserving operator environment configuration for Team."""

    def models_gateway_connection(
        self, *, org_id: int, control_database_url: str | None
    ) -> ModelsGatewayConnection | None:
        del org_id, control_database_url
        return resolve_models_gateway_env()


def _require_port(value: Any, protocol: type, role: str) -> Any:
    """Fail closed: a port object that cannot answer its question is refused."""
    if value is None:
        return None
    if not isinstance(value, protocol):
        raise TypeError(
            f"{role} port {value!r} does not satisfy {protocol.__name__} "
            f"(expected the {protocol.__name__} method surface). Worker ports "
            "are injected explicitly at composition; an object that cannot "
            "answer its question is refused rather than silently ignored."
        )
    return value


@dataclasses.dataclass(frozen=True)
class WorkerPorts:
    """The port carrier a composition hands to the open worker registration."""

    # Deliberately absent by default. An organization-scoped edition supplies
    # its own control-plane credential authority at composition time.
    credential_port: CredentialPort | None = None
    admission_port: AdmissionPort | None = None
    models_gateway_port: ModelsGatewayPort | None = dataclasses.field(
        default_factory=EnvironmentModelsGatewayPort
    )
    # Deliberately absent by default. An organization-scoped edition must opt into its
    # own search-secret authority; worker environment keys never fill this.
    # Append optional ports to preserve the existing positional carrier order.
    search_credential_port: SearchCredentialPort | None = None
    action_credential_port: ActionCredentialPort | None = None

    def __post_init__(self) -> None:
        _require_port(
            self.action_credential_port, ActionCredentialPort, "action credential"
        )
        _require_port(self.credential_port, CredentialPort, "credential")
        _require_port(
            self.search_credential_port, SearchCredentialPort, "search credential"
        )
        _require_port(
            self.models_gateway_port,
            ModelsGatewayPort,
            "models gateway",
        )
        _require_port(self.admission_port, AdmissionPort, "admission")

    def credentials(self) -> CredentialPort | None:
        """The injected organization credential port, or None when absent."""
        return self.credential_port

    def search_credentials(self) -> SearchCredentialPort | None:
        return self.search_credential_port

    def models_gateway(self) -> ModelsGatewayPort:
        """The gateway port, falling back to the operator environment."""

        return self.models_gateway_port or EnvironmentModelsGatewayPort()


def coerce_worker_ports(worker_ports: Any) -> WorkerPorts:
    """Normalize a caller's `worker_ports` argument, refusing junk."""
    if worker_ports is None:
        return WorkerPorts()
    if not isinstance(worker_ports, WorkerPorts):
        raise TypeError(
            f"worker_ports must be a {WorkerPorts.__name__} carrier, got "
            f"{type(worker_ports).__name__}. Compose the worker with explicit "
            "ports; monkey-patching the worker's module globals is not a "
            "supported seam."
        )
    return worker_ports
