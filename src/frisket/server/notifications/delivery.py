"""Notification delivery provider boundary."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import re
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit, urlunsplit

from frisket.server.notifications.secrets import (
    EnvNotificationSecretResolver,
    NotificationSecretResolver,
)
from frisket.project_identity import ProjectStorageKey


@dataclass(frozen=True)
class NotificationRenderedMessage:
    project_id: str
    delivery_request_id: int
    route_id: int | None
    channel_id: int
    delivery_kind: str
    title: str
    summary: str
    source_kind: str | None = None
    severity: str | None = None
    notification_id: int | None = None
    digest_run_id: int | None = None
    deep_link: dict[str, Any] = field(default_factory=dict)
    provider_idempotency_key: str | None = None


@dataclass(frozen=True)
class DeliveryProviderResult:
    status: str
    provider_ref: str | None = None
    retry_after_seconds: float | None = None
    error: str | None = None
    response_meta: dict[str, Any] = field(default_factory=dict)
    transient: bool = False
    outcome_unknown: bool = False


class NotificationDeliveryProvider(Protocol):
    def send(
        self,
        message: NotificationRenderedMessage,
        *,
        channel: dict[str, Any],
    ) -> DeliveryProviderResult: ...


class NotificationDeliveryTransientError(RuntimeError):
    """Raised when the worker should retry the same delivery request."""


@dataclass(frozen=True)
class NotificationDeliveryEffect:
    """Stable identity presented to a deployment-owned delivery ledger."""

    effect_id: str
    project_key: ProjectStorageKey | None
    delivery_request_id: int
    provider_kind: str
    provider_idempotency_key: str | None


@dataclass(frozen=True)
class NotificationDeliveryEffectDecision:
    """The only three safe outcomes of a pre-egress reservation."""

    status: Literal["send", "completed", "reconciliation_required"]
    result: DeliveryProviderResult | None = None


class NotificationDeliveryEffectGuard(Protocol):
    """Deployment-owned durable reserve/complete seam for provider egress.

    ``reserve`` must return ``reconciliation_required`` for an existing
    reservation whose provider outcome is unknown, unless the effect carries
    a real provider idempotency key.  A completed effect returns its durable
    result and never sends again.  ``finish`` resolves a reservation only
    after the provider response is known; it is deliberately not called for
    an ambiguous transport exception.
    """

    def reserve(
        self, effect: NotificationDeliveryEffect
    ) -> NotificationDeliveryEffectDecision: ...

    def finish(
        self,
        effect: NotificationDeliveryEffect,
        result: DeliveryProviderResult,
    ) -> None: ...


@dataclass
class NotificationDeliveryRuntime:
    provider_factory: Callable[[dict[str, Any]], NotificationDeliveryProvider]
    secret_resolver: NotificationSecretResolver
    effect_guard: NotificationDeliveryEffectGuard | None = None


NotificationDeliveryRuntimeFactory = Callable[
    [ProjectStorageKey], NotificationDeliveryRuntime
]


class FakeNotificationDeliveryProvider:
    """Deterministic provider for tests and local default wiring."""

    def __init__(
        self,
        result: DeliveryProviderResult | None = None,
    ) -> None:
        self.result = result or DeliveryProviderResult(
            status="skipped",
            error="notification delivery provider not configured",
        )

    def send(
        self,
        message: NotificationRenderedMessage,
        *,
        channel: dict[str, Any],
    ) -> DeliveryProviderResult:
        return self.result


def default_delivery_runtime(
    *,
    secret_resolver: NotificationSecretResolver | None = None,
) -> NotificationDeliveryRuntime:
    from frisket.server.notifications.providers import notification_provider_for_channel

    return NotificationDeliveryRuntime(
        provider_factory=notification_provider_for_channel,
        secret_resolver=secret_resolver or EnvNotificationSecretResolver(),
    )


_SENSITIVE_KEY_PARTS = (
    "api_key",
    "apikey",
    "authorization",
    "bearer",
    "cookie",
    "credential",
    "password",
    "secret",
    "session",
    "token",
    "webhook_url",
)
_URL_RE = re.compile(r"https?://[^\s\"'<>)]+")
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{6,}")
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(api[_-]?key|apikey|token|secret|authorization|webhook_url)"
    r"\b\s*[:=]\s*[^,\s;]+"
)


def redact_delivery_provider_result(
    result: DeliveryProviderResult,
    *,
    secret_values: list[str | None] | tuple[str | None, ...] = (),
) -> DeliveryProviderResult:
    return DeliveryProviderResult(
        status=result.status,
        provider_ref=redact_delivery_value(
            result.provider_ref, secret_values=secret_values
        )
        if result.provider_ref is not None
        else None,
        retry_after_seconds=result.retry_after_seconds,
        error=redact_delivery_value(result.error, secret_values=secret_values)
        if result.error is not None
        else None,
        response_meta=redact_delivery_value(
            result.response_meta, secret_values=secret_values
        ),
        transient=result.transient,
        outcome_unknown=result.outcome_unknown,
    )


def redact_delivery_value(
    value: Any,
    *,
    secret_values: list[str | None] | tuple[str | None, ...] = (),
) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, child in value.items():
            key_text = str(key)
            lowered = key_text.lower()
            if any(part in lowered for part in _SENSITIVE_KEY_PARTS):
                out[key_text] = "[redacted]"
            else:
                out[key_text] = redact_delivery_value(
                    child, secret_values=secret_values
                )
        return out
    if isinstance(value, list):
        return [
            redact_delivery_value(child, secret_values=secret_values) for child in value
        ]
    if isinstance(value, str):
        return _redact_text(value, secret_values=secret_values)
    return value


def _redact_text(
    value: str,
    *,
    secret_values: list[str | None] | tuple[str | None, ...],
) -> str:
    text = _URL_RE.sub(_redact_url_match, str(value))
    text = _BEARER_RE.sub("Bearer [redacted]", text)
    text = _SECRET_ASSIGNMENT_RE.sub(
        lambda match: f"{match.group(1)}=[redacted]",
        text,
    )
    for secret in sorted(
        {str(secret) for secret in secret_values if secret and len(str(secret)) >= 4},
        key=len,
        reverse=True,
    ):
        text = text.replace(secret, "[redacted]")
    return text


def _redact_url_match(match: re.Match[str]) -> str:
    raw = match.group(0)
    parsed = urlsplit(raw)
    if not parsed.scheme or not parsed.netloc:
        return "[redacted-url]"
    if parsed.path or parsed.query or parsed.fragment:
        return urlunsplit((parsed.scheme, parsed.netloc, "/[redacted]", "", ""))
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
