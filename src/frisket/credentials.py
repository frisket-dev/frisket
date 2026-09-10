"""Generic action-credential resolution.

An `ActionCatalogEntry.required_credentials` list (contracts/actions/schemas/
_base.py) names environment-variable-style credential names an action needs
before it can run (e.g. ``"CENSUS_API_KEY"`` for `enrich.census_demographics`,
actions/census.py). Resolution order mirrors the
existing env-var-first precedent (ops/geocode.py's OPENCAGE_API_KEY,
sdk/ops/census_demographics.py's CENSUS_API_KEY): a process-level environment variable (for
self-hosted/ops-managed deploys) wins, falling back to the project-scoped
secrets store (`Project.secret_plaintext`, store/project.py:1565 — the same
store the Settings -> Secrets panel writes, web/src/settings/
SettingsSections.tsx `ProjectSecretsSettings`).

`missing_required_credentials` is the single check both the queued run gate
(server/action_enqueue.py) and the project-aware catalog hints (server/
action_catalog_hints.py, so ActionPanel can show the gate proactively) call —
one source of truth for "can this action run right now."
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from frisket.ai.llm.types import CredentialSource
from frisket.execution.credential_use import (
    CredentialOwner,
    CredentialUseContext,
)


@dataclass(frozen=True)
class ResolvedCredential:
    """A credential value plus the non-secret provenance durable facts store."""

    value: str
    source: CredentialSource
    owner: CredentialOwner | None = None


def resolve_credential_with_source(
    project: Any, name: str
) -> ResolvedCredential | None:
    """Resolve one action credential without dropping who supplied it.

    Process-environment keys belong to the local/operator composition layer;
    project-secret keys belong to the project.  The returned source is the
    same closed vocabulary used by provider facts and never contains key
    material.
    """
    env_value = os.environ.get(name)
    if env_value:
        return ResolvedCredential(env_value, "local")
    secret_plaintext = getattr(project, "secret_plaintext", None)
    if secret_plaintext is None:
        return None
    value = secret_plaintext(name)
    return ResolvedCredential(value, "project_key") if value else None


def normalize_resolved_credential(
    credential: ResolvedCredential,
    *,
    context: CredentialUseContext | None,
) -> ResolvedCredential:
    """Normalize composition-specific provenance before the payer fence.

    ``local`` is the correct open-edition spelling for an environment key.
    On a hosted deployment the same physical lookup is the platform's
    credential, so the request composition supplies ``deployment_owner`` and
    this boundary returns ``platform_key`` plus that owner. Project secrets
    stay ``project_key``: normalization must not launder a user's/storage
    credential into the platform class.
    """

    if context is None:
        return credential
    source = credential.source
    owner = credential.owner
    if source == "local" and context.deployment_owner is not None:
        source = "platform_key"
        owner = context.deployment_owner
    elif source == "platform_key" and owner is None:
        selected = context.selected_owner
        owner = context.deployment_owner or (
            selected if selected is not None and selected.kind == "deployment" else None
        )
    elif source == "org_byok" and owner is None:
        selected = context.selected_owner
        if selected is not None and selected.kind == "organization":
            owner = selected
    return ResolvedCredential(credential.value, source, owner)


def resolve_credential_for_use(
    project: Any,
    name: str,
    *,
    context: CredentialUseContext | None,
) -> ResolvedCredential | None:
    """Resolve and normalize one credential for a paid external effect.

    Hosted composition may provide a request-scoped resolver (for example,
    funding-account BYOK). The open fallback remains the existing
    env-then-project lookup. In both cases normalization happens here, before
    either the fence or the provider fact reads provenance.
    """

    resolver = context.credential_resolver if context is not None else None
    if resolver is None:
        credential = resolve_credential_with_source(project, name)
    else:
        credential = resolver.resolve_action_credential(project, name)
        if credential is not None and not isinstance(credential, ResolvedCredential):
            raise TypeError(
                "action credential resolver must return ResolvedCredential or None"
            )
    if credential is None:
        return None
    return normalize_resolved_credential(credential, context=context)


def resolve_credential(project: Any, name: str) -> str | None:
    """The value for a named credential, or None if unconfigured.

    Checks the process environment first (self-hosted/ops-managed secrets),
    then the project's own secrets store.
    """
    resolved = resolve_credential_with_source(project, name)
    return resolved.value if resolved is not None else None


def missing_required_credentials(
    project: Any,
    required_credentials: list[str] | tuple[str, ...],
) -> list[str]:
    """Names from ``required_credentials`` that resolve to nothing, in the
    declared order (stable — used verbatim in error messages)."""
    return [
        name for name in required_credentials if not resolve_credential(project, name)
    ]
