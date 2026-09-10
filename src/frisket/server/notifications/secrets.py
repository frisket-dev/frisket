"""Notification secret resolution boundary."""

from __future__ import annotations

import os
from typing import Protocol, TYPE_CHECKING

if TYPE_CHECKING:
    # Typing-only: pins this resolver's crypto dependency to the open AEAD
    # envelope module (frisket.security.secrets) without importing it at
    # runtime — this module never decrypts directly, it delegates to its
    # OrgSecretAccess port.
    from frisket.team.security import secrets as _security_secrets  # noqa: F401


class NotificationSecretResolver(Protocol):
    def resolve(self, secret_ref: str, *, project_id: str) -> str | None: ...


class OrgSecretAccess(Protocol):
    """Open port for org-scoped encrypted env vars.

    Structural, and deliberately declared HERE rather than imported from the
    private tree: a typing-only import of the hosted secret-access service would
    still bind this open module to private symbols and break a source-only
    reading of the open repo (gate 1). The private
    ``HostedSecretAccessService`` satisfies this port as-is.
    """

    def env_var_plaintext(self, org_id: int, name: str) -> str | None: ...


class EnvNotificationSecretResolver:
    """Local/CLI resolver for explicit `env:NAME` refs."""

    def resolve(self, secret_ref: str, *, project_id: str) -> str | None:
        parsed = _parse_env_ref(secret_ref)
        if parsed is None:
            return None
        return os.environ.get(parsed)


class NullNotificationSecretResolver:
    """Fail-closed resolver for unsupported construction paths."""

    def resolve(self, secret_ref: str, *, project_id: str) -> str | None:
        return None


class HostedNotificationSecretResolver:
    """Hosted resolver backed by org-scoped encrypted env vars."""

    def __init__(
        self,
        secret_access: OrgSecretAccess,
        *,
        org_id: int,
    ) -> None:
        self._secret_access = secret_access
        self._org_id = int(org_id)

    def resolve(self, secret_ref: str, *, project_id: str) -> str | None:
        parsed = _parse_env_ref(secret_ref)
        if parsed is None:
            return None
        return self._secret_access.env_var_plaintext(self._org_id, parsed)


class StaticNotificationSecretResolver:
    """Test helper resolver."""

    def __init__(self, values: dict[str, str]) -> None:
        self._values = dict(values)

    def resolve(self, secret_ref: str, *, project_id: str) -> str | None:
        return self._values.get(secret_ref)


def _parse_env_ref(secret_ref: str) -> str | None:
    value = str(secret_ref or "").strip()
    if not value.startswith("env:"):
        return None
    name = value.removeprefix("env:").strip().upper()
    if not name or not all(c.isalnum() or c == "_" for c in name):
        return None
    return name
