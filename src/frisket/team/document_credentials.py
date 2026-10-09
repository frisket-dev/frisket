"""Organization document keys for request and worker execution composition."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from frisket.credentials import ResolvedCredential, resolve_credential_with_source
from frisket.execution.credential_use import CredentialOwner
from frisket.provider_definitions import providers_for
from frisket.team.control_plane import control_plane_engine, org_provider_keys

_DOCUMENT_ENV = {
    p.env_var: p.id for p in providers_for(category="document", scope="organization")
}


@dataclass(frozen=True)
class OrganizationDocumentCredentials:
    org_id: int
    keys: Mapping[str, str] = field(repr=False)

    def __post_init__(self):
        if type(self.org_id) is not int or self.org_id <= 0:
            raise ValueError("document credentials require a trusted organization ID")
        object.__setattr__(self, "keys", MappingProxyType(dict(self.keys)))

    def resolve_action_credential(
        self, project: Any, name: str
    ) -> ResolvedCredential | None:
        provider = _DOCUMENT_ENV.get(name)
        if provider is None:
            return resolve_credential_with_source(project, name)
        # Project overrides match the Team LLM policy. A missing org key must
        # never silently select the deployment operator's paid API key.
        project_key = resolve_credential_with_source(project, name, env={})
        if project_key is not None:
            return project_key
        value = self.keys.get(provider)
        return (
            ResolvedCredential(
                value, "org_byok", CredentialOwner.organization(self.org_id)
            )
            if value
            else None
        )


class TeamOrgDocumentCredentialPort:
    def __init__(self, decryptor: Callable[[str], str]):
        self._decryptor = decryptor

    def action_credential_resolver(
        self, *, org_id: int, control_database_url: str | None
    ) -> OrganizationDocumentCredentials:
        keys = (
            org_provider_keys(
                control_plane_engine(control_database_url),
                org_id=org_id,
                providers=tuple(_DOCUMENT_ENV.values()),
                decryptor=self._decryptor,
            )
            if control_database_url
            else {}
        )
        return OrganizationDocumentCredentials(org_id, keys)
