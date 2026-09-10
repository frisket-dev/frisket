"""The worker's explicit ports.

The open worker runs jobs. Three decisions around a job are EDITION policy, not
worker mechanics, so each is a port the composition injects rather than an
import the worker reaches for (the public/private package boundary rejects
monkey-patching and import-time side effects as seam mechanisms):

* credential — which provider keys may this run use?
* admission  — may this body execute at all?
* settlement — what does the run's outcome cost, and who pays?

Only `credential` has an OPEN default (`OrgKeyCredentialPort`): the open team
worker resolves org BYOK keys from the identity control plane. `admission` and
`settlement` default to ABSENT, which is what the open team edition wants — a
trusted local/team worker admits every action it is asked to run, and has no
commerce to settle. An external edition injects all three from its own worker
composition.

Ports are structural (`typing.Protocol`), so an edition never subclasses
anything from here; it just supplies an object with the right method. A carrier
that does not satisfy its protocol is REFUSED at composition time (TypeError) —
a worker never silently ignores a port it cannot understand.
"""

from __future__ import annotations

import dataclasses
import enum
from collections.abc import Mapping
from typing import Any, Literal, NewType, Protocol, runtime_checkable

from frisket.team.control_plane import control_plane_engine, org_provider_keys


@runtime_checkable
class CredentialPort(Protocol):
    """Resolves the provider keys a run may use on behalf of an org."""

    def provider_keys(
        self, *, org_id: int, control_database_url: str | None
    ) -> Mapping[str, str]:
        """Provider name -> plaintext key. Empty when the org has none."""
        ...


@runtime_checkable
class AdmissionPort(Protocol):
    """Decides whether a queued body may execute at all.

    Returns True to REFUSE. The open team worker has no admission port: it is a
    trusted runtime and runs what it is given. An external edition injects one
    because public-hosted tenants must never execute arbitrary code.
    """

    def execution_denied(self, *, body: Mapping[str, Any]) -> bool: ...


TrustedJobOrgId = NewType("TrustedJobOrgId", int)


class TrustedJobOrgUnavailable(enum.Enum):
    """Why a handler invocation has no org attested by a claimed job row.

    Neither arm authorizes a settlement adapter to recover an identity from
    the handler payload, the storage owner, or ambient process state. A hosted
    adapter that requires a funding org must refuse before an effect or ledger
    mutation unless its separate, typed non-queued contract proves one.
    """

    NO_JOB_ROW = "no_job_row"
    JOB_ROW_HAS_NO_ORG = "job_row_has_no_org"


type TrustedJobOrg = TrustedJobOrgId | TrustedJobOrgUnavailable


@dataclasses.dataclass(frozen=True)
class JobHandlerContext:
    """Immutable row-backed facts delivered separately from a job payload."""

    trusted_job_org_id: TrustedJobOrg

    def __post_init__(self) -> None:
        value = self.trusted_job_org_id
        if isinstance(value, TrustedJobOrgUnavailable):
            return
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(
                "trusted job org id must be a positive integer or an explicit "
                "TrustedJobOrgUnavailable arm"
            )

    @classmethod
    def from_claimed_job(cls, *, trusted_org_id: int | None) -> JobHandlerContext:
        """Mint context from the worker's normalized immutable job column."""
        if trusted_org_id is None:
            return cls(TrustedJobOrgUnavailable.JOB_ROW_HAS_NO_ORG)
        return cls(TrustedJobOrgId(trusted_org_id))

    @classmethod
    def without_job_row(cls) -> JobHandlerContext:
        """Explicit context for direct/non-queued handler invocation."""
        return cls(TrustedJobOrgUnavailable.NO_JOB_ROW)


@runtime_checkable
class SettlementPort(Protocol):
    """Settles terminal run and runless-receipt outcomes.

    Optional by construction: the open editions never settle anything, so
    this port is OMISSIBLE and the worker simply skips settlement when it is
    absent. The port itself is omissible; its identity FACT is not:
    ``trusted_job_org_id`` is a required argument. A positive id is minted from
    the normalized immutable ``job.org_id`` column. An unavailable arm means
    no row-backed funding identity exists and is never permission to infer one
    from payload, storage ownership, or ambient state.
    """

    def settle_run(
        self,
        *,
        project: Any,
        project_id: str,
        run_id: int,
        trusted_job_org_id: TrustedJobOrg,
        control_database_url: str | None,
        key_providers: set[str],
        terminal_status: Literal["completed", "failed", "cancelled"],
    ) -> None: ...

    def settle_action_receipt(
        self,
        *,
        project: Any,
        project_id: str,
        receipt_id: str,
        trusted_job_org_id: TrustedJobOrg,
        control_database_url: str | None,
    ) -> None:
        """Settle one durably terminal runless action receipt.

        The receipt owns its terminal posture and exact neutral provider-fact
        links. The adapter receives only its id plus immutable queue-backed
        tenant identity; it must not infer either identity from payload data.
        """
        ...


class OrgKeyCredentialPort:
    """The OPEN default: org BYOK keys read from the identity control plane.

    No spend caps and no platform-key fallback — both are commerce policy. An
    org with no stored key simply gets no key, and the run fails the way any
    unkeyed run fails.
    """

    def provider_keys(
        self, *, org_id: int, control_database_url: str | None
    ) -> Mapping[str, str]:
        if not control_database_url:
            return {}
        engine = control_plane_engine(control_database_url)
        return org_provider_keys(engine, org_id=org_id)


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

    credential_port: CredentialPort | None = dataclasses.field(
        default_factory=OrgKeyCredentialPort
    )
    admission_port: AdmissionPort | None = None
    settlement_port: SettlementPort | None = None

    def __post_init__(self) -> None:
        _require_port(self.credential_port, CredentialPort, "credential")
        _require_port(self.admission_port, AdmissionPort, "admission")
        _require_port(self.settlement_port, SettlementPort, "settlement")

    def credentials(self) -> CredentialPort:
        """The credential port, falling back to the OPEN default."""
        return self.credential_port or OrgKeyCredentialPort()


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
