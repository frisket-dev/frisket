"""Notification secret resolution boundary."""

from __future__ import annotations

import os
from typing import Protocol


class NotificationSecretResolver(Protocol):
    def resolve(self, secret_ref: str, *, project_id: str) -> str | None: ...


class EnvNotificationSecretResolver:
    """Local/CLI resolver for explicit `env:NAME` refs."""

    def resolve(self, secret_ref: str, *, project_id: str) -> str | None:
        parsed = _parse_env_ref(secret_ref)
        if parsed is None:
            return None
        return os.environ.get(parsed)


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
