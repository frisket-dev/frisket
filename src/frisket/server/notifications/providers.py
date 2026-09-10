"""Concrete notification delivery providers."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
import hashlib
import hmac
from html import escape
import json
import time
from typing import Any
from urllib.parse import urlsplit

import httpx

from frisket.server.notifications.delivery import (
    DeliveryProviderResult,
    FakeNotificationDeliveryProvider,
    NotificationDeliveryProvider,
    NotificationRenderedMessage,
)
from frisket.ops.netguard import url_is_safe

DEFAULT_RESEND_ENDPOINT = "https://api.resend.com/emails"
DEFAULT_RESEND_FROM = "frisket <notifications@resend.dev>"
DEFAULT_PROVIDER_TIMEOUT_SECONDS = 10.0
DEFAULT_WEBHOOK_MAX_REQUEST_BYTES = 256 * 1024
DEFAULT_WEBHOOK_MAX_RESPONSE_BYTES = 64 * 1024
DEFAULT_WEBHOOK_MAX_REDIRECTS = 2
DEFAULT_WEBHOOK_SIGNATURE_HEADER = "X-Frisket-Signature"
WEBHOOK_TIMESTAMP_HEADER = "X-Frisket-Timestamp"


@dataclass(frozen=True)
class ProviderHttpResponse:
    status_code: int
    text: str = ""
    json_body: dict[str, Any] | None = None
    headers: Mapping[str, str] = field(default_factory=dict)


HttpPost = Callable[
    [str, Mapping[str, str], dict[str, Any], float], ProviderHttpResponse
]


def notification_provider_for_channel(
    channel: dict[str, Any],
    *,
    http_post: HttpPost | None = None,
    timeout_seconds: float = DEFAULT_PROVIDER_TIMEOUT_SECONDS,
) -> NotificationDeliveryProvider:
    kind = str(channel.get("kind") or "").strip().lower()
    if kind == "email":
        return ResendEmailNotificationProvider(
            http_post=http_post,
            timeout_seconds=timeout_seconds,
        )
    if kind == "slack":
        return SlackIncomingWebhookNotificationProvider(
            http_post=http_post,
            timeout_seconds=timeout_seconds,
        )
    if kind == "webhook":
        return GenericWebhookNotificationProvider(
            http_post=http_post,
            timeout_seconds=timeout_seconds,
        )
    return FakeNotificationDeliveryProvider(
        DeliveryProviderResult(
            status="skipped",
            error=f"{kind or 'unknown'} notification provider not configured",
        )
    )


class WebhookDeliverySecurityError(RuntimeError):
    """Raised when generic webhook delivery is blocked by egress policy."""


class SlackWebhookDeliverySecurityError(WebhookDeliverySecurityError):
    """Raised when Slack webhook delivery is blocked by egress policy."""


class ResendEmailNotificationProvider:
    def __init__(
        self,
        *,
        endpoint: str = DEFAULT_RESEND_ENDPOINT,
        http_post: HttpPost | None = None,
        timeout_seconds: float = DEFAULT_PROVIDER_TIMEOUT_SECONDS,
    ) -> None:
        self.endpoint = endpoint
        self._http_post = http_post or _http_post_json
        self._timeout_seconds = float(timeout_seconds)

    def send(
        self,
        message: NotificationRenderedMessage,
        *,
        channel: dict[str, Any],
    ) -> DeliveryProviderResult:
        config = _channel_config(channel)
        secret = _required_secret(channel)
        recipient = str(config.get("to") or "").strip()
        if not recipient:
            return DeliveryProviderResult(
                status="failed",
                error="email notification channel missing recipient",
            )
        payload = {
            "from": str(
                config.get("from") or config.get("from_address") or DEFAULT_RESEND_FROM
            ).strip(),
            "to": recipient,
            "subject": message.title,
            "text": _plain_message(message),
            "html": _html_message(message),
            "tags": [
                {
                    "name": "delivery_request_id",
                    "value": str(message.delivery_request_id),
                }
            ],
        }
        headers = {
            "Authorization": f"Bearer {secret}",
            "Content-Type": "application/json",
        }
        if message.provider_idempotency_key:
            headers["Idempotency-Key"] = message.provider_idempotency_key
        response = self._http_post(
            self.endpoint,
            headers,
            payload,
            self._timeout_seconds,
        )
        provider_ref = _response_provider_ref(response, provider="resend")
        if _is_success(response.status_code):
            return DeliveryProviderResult(
                status="sent",
                provider_ref=provider_ref,
                response_meta=_response_meta("resend", response),
            )
        return DeliveryProviderResult(
            status="failed",
            provider_ref=provider_ref,
            retry_after_seconds=_retry_after_seconds(response.headers),
            error=(
                "resend email delivery failed with status "
                f"{response.status_code}: {_response_excerpt(response)}"
            ),
            response_meta=_response_meta("resend", response),
            transient=_is_transient(response.status_code),
        )


class SlackIncomingWebhookNotificationProvider:
    def __init__(
        self,
        *,
        http_post: HttpPost | None = None,
        safety_check: Callable[[str], bool] = url_is_safe,
        timeout_seconds: float = DEFAULT_PROVIDER_TIMEOUT_SECONDS,
    ) -> None:
        self._http_post = http_post or _http_post_json
        self._safety_check = safety_check
        self._timeout_seconds = float(timeout_seconds)

    def send(
        self,
        message: NotificationRenderedMessage,
        *,
        channel: dict[str, Any],
    ) -> DeliveryProviderResult:
        try:
            webhook_url = _slack_webhook_url(
                channel,
                safety_check=self._safety_check,
            )
        except SlackWebhookDeliverySecurityError as exc:
            return DeliveryProviderResult(
                status="failed",
                error=str(exc),
                transient=False,
            )
        response = self._http_post(
            webhook_url,
            {"Content-Type": "application/json"},
            {"text": _slack_text(message)},
            self._timeout_seconds,
        )
        provider_ref = f"slack:webhook:{message.delivery_request_id}"
        if _is_success(response.status_code):
            return DeliveryProviderResult(
                status="sent",
                provider_ref=provider_ref,
                response_meta=_response_meta("slack", response),
            )
        return DeliveryProviderResult(
            status="failed",
            provider_ref=provider_ref,
            retry_after_seconds=_retry_after_seconds(response.headers),
            error=(
                "slack notification delivery failed with status "
                f"{response.status_code}: {_response_excerpt(response)}"
            ),
            response_meta=_response_meta("slack", response),
            transient=_is_transient(response.status_code),
        )


class GenericWebhookNotificationProvider:
    def __init__(
        self,
        *,
        http_post: HttpPost | None = None,
        safety_check: Callable[[str], bool] = url_is_safe,
        timeout_seconds: float = DEFAULT_PROVIDER_TIMEOUT_SECONDS,
    ) -> None:
        self._http_post = http_post
        self._safety_check = safety_check
        self._timeout_seconds = float(timeout_seconds)

    def send(
        self,
        message: NotificationRenderedMessage,
        *,
        channel: dict[str, Any],
    ) -> DeliveryProviderResult:
        config = _channel_config(channel)
        try:
            webhook_url = _webhook_url(channel)
            signing_secret = _webhook_signing_secret(channel)
            _require_safe_webhook_url(webhook_url, safety_check=self._safety_check)
            payload = _webhook_payload(message)
            body = _canonical_json_bytes(payload)
            max_request_bytes = _webhook_max_request_bytes(config)
            if len(body) > max_request_bytes:
                raise WebhookDeliverySecurityError(
                    f"webhook request exceeded max_request_bytes={max_request_bytes}"
                )
            timestamp = str(int(time.time()))
            signature_header = _webhook_signature_header(config)
            headers = {
                "Content-Type": "application/json",
                "User-Agent": "frisket-notification-webhook/1",
                WEBHOOK_TIMESTAMP_HEADER: timestamp,
                signature_header: _webhook_signature(
                    signing_secret,
                    timestamp=timestamp,
                    body=body,
                ),
            }
            if self._http_post is not None:
                response = self._http_post(
                    webhook_url,
                    headers,
                    payload,
                    self._timeout_seconds,
                )
            else:
                response = _guarded_webhook_post_json(
                    webhook_url,
                    headers,
                    payload,
                    self._timeout_seconds,
                    max_response_bytes=_webhook_max_response_bytes(config),
                    max_redirects=_webhook_max_redirects(config),
                    safety_check=self._safety_check,
                )
        except WebhookDeliverySecurityError as exc:
            return DeliveryProviderResult(
                status="failed",
                error=f"webhook delivery blocked: {exc}",
            )
        except ValueError as exc:
            return DeliveryProviderResult(
                status="failed",
                error=f"webhook delivery invalid: {exc}",
            )
        except httpx.HTTPError:
            return DeliveryProviderResult(
                status="failed",
                error="webhook provider outcome is unknown",
                transient=True,
                outcome_unknown=True,
            )
        provider_ref = _response_provider_ref(response, provider="webhook")
        if provider_ref is None:
            provider_ref = f"webhook:delivery:{message.delivery_request_id}"
        if _is_success(response.status_code):
            return DeliveryProviderResult(
                status="sent",
                provider_ref=provider_ref,
                response_meta=_response_meta("webhook", response),
            )
        return DeliveryProviderResult(
            status="failed",
            provider_ref=provider_ref,
            retry_after_seconds=_retry_after_seconds(response.headers),
            error=(
                "webhook notification delivery failed with status "
                f"{response.status_code}: {_response_excerpt(response)}"
            ),
            response_meta=_response_meta("webhook", response),
            transient=_is_transient(response.status_code),
        )


def _http_post_json(
    url: str,
    headers: Mapping[str, str],
    payload: dict[str, Any],
    timeout_seconds: float,
) -> ProviderHttpResponse:
    with httpx.Client(follow_redirects=False, timeout=timeout_seconds) as client:
        response = client.post(url, headers=dict(headers), json=payload)
    json_body: dict[str, Any] | None = None
    try:
        parsed = response.json()
    except ValueError:
        parsed = None
    if isinstance(parsed, dict):
        json_body = parsed
    return ProviderHttpResponse(
        status_code=int(response.status_code),
        text=response.text,
        json_body=json_body,
        headers=dict(response.headers),
    )


def _guarded_webhook_post_json(
    url: str,
    headers: Mapping[str, str],
    payload: dict[str, Any],
    timeout_seconds: float,
    *,
    max_response_bytes: int = DEFAULT_WEBHOOK_MAX_RESPONSE_BYTES,
    max_redirects: int = DEFAULT_WEBHOOK_MAX_REDIRECTS,
    client_factory: Callable[..., Any] = httpx.Client,
    safety_check: Callable[[str], bool] = url_is_safe,
) -> ProviderHttpResponse:
    current = _require_safe_webhook_url(url, safety_check=safety_check)
    body = _canonical_json_bytes(payload)
    seen_redirects = 0
    with client_factory(follow_redirects=False, timeout=timeout_seconds) as client:
        while True:
            with client.stream(
                "POST",
                current,
                headers=dict(headers),
                content=body,
            ) as response:
                if response.is_redirect:
                    seen_redirects += 1
                    if seen_redirects > max_redirects:
                        raise WebhookDeliverySecurityError("too many webhook redirects")
                    next_url = str(response.next_request.url)
                    current = _require_safe_webhook_url(
                        next_url,
                        safety_check=safety_check,
                        prefix="blocked webhook redirect target",
                    )
                    continue
                response_body = _bounded_response_body(
                    response,
                    max_response_bytes=max_response_bytes,
                )
                json_body: dict[str, Any] | None = None
                try:
                    parsed = json.loads(response_body.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    parsed = None
                if isinstance(parsed, dict):
                    json_body = parsed
                return ProviderHttpResponse(
                    status_code=int(response.status_code),
                    text=response_body.decode("utf-8", errors="replace"),
                    json_body=json_body,
                    headers=dict(response.headers),
                )


def _channel_config(channel: dict[str, Any]) -> dict[str, Any]:
    config = channel.get("config")
    return config if isinstance(config, dict) else {}


def _required_secret(channel: dict[str, Any]) -> str:
    secret = str(channel.get("secret_value") or "").strip()
    if not secret:
        raise ValueError("notification provider missing resolved secret")
    return secret


def _webhook_url(channel: dict[str, Any]) -> str:
    url = _required_secret(channel)
    _require_https_url(url)
    return url


def _slack_webhook_url(
    channel: dict[str, Any],
    *,
    safety_check: Callable[[str], bool],
) -> str:
    url = _required_secret(channel)
    try:
        _require_safe_webhook_url(url, safety_check=safety_check)
        parsed = urlsplit(str(url))
        host = (parsed.hostname or "").lower()
        if host not in {"hooks.slack.com", "hooks.slack-gov.com"}:
            raise SlackWebhookDeliverySecurityError(
                f"blocked Slack webhook host: {_redacted_url_for_error(url)}"
            )
        config_host = str(_channel_config(channel).get("webhook_host") or "").strip()
        if config_host and config_host.lower() != host:
            raise SlackWebhookDeliverySecurityError(
                "Slack webhook host does not match channel metadata"
            )
    except WebhookDeliverySecurityError as exc:
        if isinstance(exc, SlackWebhookDeliverySecurityError):
            raise
        raise SlackWebhookDeliverySecurityError(
            str(exc).replace("webhook", "Slack webhook")
        ) from exc
    return url


def _webhook_signing_secret(channel: dict[str, Any]) -> str:
    secret = str(channel.get("signing_secret_value") or "").strip()
    if not secret:
        raise ValueError("webhook notification provider missing signing secret")
    return secret


def _webhook_signature_header(config: dict[str, Any]) -> str:
    header = str(config.get("signature_header") or DEFAULT_WEBHOOK_SIGNATURE_HEADER)
    header = header.strip()
    return header or DEFAULT_WEBHOOK_SIGNATURE_HEADER


def _webhook_max_response_bytes(config: dict[str, Any]) -> int:
    raw = config.get("max_response_bytes")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_WEBHOOK_MAX_RESPONSE_BYTES
    return min(max(value, 1), DEFAULT_WEBHOOK_MAX_RESPONSE_BYTES)


def _webhook_max_request_bytes(config: dict[str, Any]) -> int:
    raw = config.get("max_request_bytes")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_WEBHOOK_MAX_REQUEST_BYTES
    return min(max(value, 1), DEFAULT_WEBHOOK_MAX_REQUEST_BYTES)


def _webhook_max_redirects(config: dict[str, Any]) -> int:
    raw = config.get("max_redirects")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_WEBHOOK_MAX_REDIRECTS
    return min(max(value, 0), DEFAULT_WEBHOOK_MAX_REDIRECTS)


def _webhook_payload(message: NotificationRenderedMessage) -> dict[str, Any]:
    return {
        "schema_version": "frisket.notification_webhook.v1",
        "project_id": message.project_id,
        "delivery_request_id": message.delivery_request_id,
        "route_id": message.route_id,
        "channel_id": message.channel_id,
        "delivery_kind": message.delivery_kind,
        "notification_id": message.notification_id,
        "digest_run_id": message.digest_run_id,
        "title": message.title,
        "summary": message.summary,
        "severity": message.severity,
        "source_kind": message.source_kind,
        "deep_link": message.deep_link,
    }


def _canonical_json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _webhook_signature(secret: str, *, timestamp: str, body: bytes) -> str:
    signed = timestamp.encode("utf-8") + b"." + body
    digest = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    return f"v1={digest}"


def _require_safe_webhook_url(
    url: str,
    *,
    safety_check: Callable[[str], bool],
    prefix: str = "blocked webhook URL",
) -> str:
    _require_https_url(url)
    if not safety_check(url):
        raise WebhookDeliverySecurityError(f"{prefix}: {_redacted_url_for_error(url)}")
    return url


def _require_https_url(url: str) -> None:
    parsed = urlsplit(str(url))
    if parsed.scheme != "https" or not parsed.netloc:
        raise WebhookDeliverySecurityError("webhook URL must be HTTPS")


def _redacted_url_for_error(url: str) -> str:
    parsed = urlsplit(str(url))
    if not parsed.scheme or not parsed.netloc:
        return "[redacted-url]"
    if parsed.path or parsed.query or parsed.fragment:
        return f"{parsed.scheme}://{parsed.netloc}/[redacted]"
    return f"{parsed.scheme}://{parsed.netloc}"


def _bounded_response_body(response: Any, *, max_response_bytes: int) -> bytes:
    content_length = response.headers.get("content-length")
    if content_length is not None:
        try:
            expected = int(content_length)
        except ValueError:
            expected = None
        if expected is not None and expected > max_response_bytes:
            raise WebhookDeliverySecurityError(
                f"webhook response exceeded max_response_bytes={max_response_bytes}"
            )
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_bytes():
        total += len(chunk)
        if total > max_response_bytes:
            raise WebhookDeliverySecurityError(
                f"webhook response exceeded max_response_bytes={max_response_bytes}"
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _plain_message(message: NotificationRenderedMessage) -> str:
    lines = [message.title, "", message.summary]
    if message.severity:
        lines.extend(["", f"Severity: {message.severity}"])
    if message.source_kind:
        lines.append(f"Source: {message.source_kind}")
    if message.deep_link:
        lines.append(f"Link context: {message.deep_link}")
    return "\n".join(lines)


def _html_message(message: NotificationRenderedMessage) -> str:
    body = escape(message.summary).replace("\n", "<br>")
    details = []
    if message.severity:
        details.append(f"<p><strong>Severity:</strong> {escape(message.severity)}</p>")
    if message.source_kind:
        details.append(f"<p><strong>Source:</strong> {escape(message.source_kind)}</p>")
    return f"<h2>{escape(message.title)}</h2><p>{body}</p>{''.join(details)}"


def _slack_text(message: NotificationRenderedMessage) -> str:
    lines = [f"*{message.title}*", message.summary]
    suffix = []
    if message.severity:
        suffix.append(f"severity: {message.severity}")
    if message.source_kind:
        suffix.append(f"source: {message.source_kind}")
    if suffix:
        lines.append("_" + " | ".join(suffix) + "_")
    return "\n".join(lines)


def _is_success(status_code: int) -> bool:
    return 200 <= int(status_code) < 300


def _is_transient(status_code: int) -> bool:
    code = int(status_code)
    return code in {408, 409, 425, 429} or code >= 500


def _retry_after_seconds(headers: Mapping[str, str]) -> float | None:
    value = headers.get("retry-after") or headers.get("Retry-After")
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


def _response_provider_ref(
    response: ProviderHttpResponse, *, provider: str
) -> str | None:
    if response.json_body:
        provider_id = response.json_body.get("id") or response.json_body.get(
            "message_id"
        )
        if provider_id:
            return f"{provider}:{provider_id}"
    return None


def _response_meta(
    provider: str,
    response: ProviderHttpResponse,
) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "provider": provider,
        "status_code": int(response.status_code),
    }
    if response.json_body:
        meta["response"] = response.json_body
    elif response.text:
        meta["response_text"] = _truncate(response.text)
    retry_after = _retry_after_seconds(response.headers)
    if retry_after is not None:
        meta["retry_after_seconds"] = retry_after
    return meta


def _response_excerpt(response: ProviderHttpResponse) -> str:
    if response.json_body:
        for key in ("message", "error", "detail"):
            value = response.json_body.get(key)
            if value:
                return _truncate(str(value))
    if response.text:
        return _truncate(response.text)
    return "provider returned no response body"


def _truncate(value: str, limit: int = 500) -> str:
    text = str(value)
    if len(text) <= limit:
        return text
    return f"{text[:limit]}..."
