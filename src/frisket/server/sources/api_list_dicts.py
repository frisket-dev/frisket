"""Bounded public JSON API source poller for list-of-dicts payloads."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qsl, urlparse

from frisket.ops.netguard import url_is_safe
from frisket.server.sources.runtime import (
    SourcePollContext,
    SourcePollItem,
    SourcePollResult,
    register_source_poller,
)

API_LIST_DICTS_KIND = "api_list_dicts"
API_LIST_DICTS_CURSOR_SCHEMA = "frisket.api_list_dicts_cursor.v1"
API_LIST_DICTS_SOURCE_SCHEMA = "frisket.source.api_list_dicts.v1"
DEFAULT_MAX_BYTES = 5_000_000
DEFAULT_MAX_ITEMS: int | None = None
_ROOT_POINTERS = {"", "/"}
_MISSING = object()
_SENSITIVE_RE = re.compile(
    r"(?i)(api[_-]?key|authorization|cookie|token|password|secret)"
    r"(\s*[:=]\s*(?:bearer\s+)?)?[^\s,;&]+"
)
_DISALLOWED_CONFIG_KEYS = frozenset(
    {
        "auth",
        "authorization",
        "body",
        "connection_id",
        "cookies",
        "data",
        "headers",
        "json",
        "oauth",
        "post_body",
        "request_body",
        "secret",
        "secret_ref",
        "secrets",
    }
)
_SENSITIVE_QUERY_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "auth",
        "authorization",
        "client_secret",
        "password",
        "secret",
        "token",
    }
)
_ALLOWED_CONFIG_KEYS = frozenset(
    {
        "schema_version",
        "method",
        "url",
        "list_path",
        "item_id_path",
        "updated_at_path",
        "max_bytes",
        "max_items",
        "schema_policy",
    }
)


class ApiListDictsProviderError(RuntimeError):
    """Raised for redacted HTTP/provider failures."""


@dataclass(frozen=True)
class ApiListDictsHttpResponse:
    status_code: int
    body: bytes
    final_url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ApiListDictsConfig:
    url: str
    method: str
    list_path: str
    item_id_path: str | None
    updated_at_path: str | None
    max_bytes: int
    max_items: int | None
    schema_policy: str


class ApiListDictsPoller:
    kind = API_LIST_DICTS_KIND

    def __init__(
        self,
        http_get: Callable[..., ApiListDictsHttpResponse] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._http_get = http_get or fetch_api_list_dicts_get
        self._now = now or (lambda: datetime.now(UTC))

    def validate_config(self, source: dict[str, Any]) -> str | None:
        _config, error = _resolve_config(source)
        return error

    def poll(self, ctx: SourcePollContext) -> SourcePollResult:
        config, error = _resolve_config(ctx.source)
        if error:
            raise ValueError(error)
        assert config is not None

        fetched_at = _iso_timestamp(self._now())
        try:
            response = self._http_get(config.url, max_bytes=config.max_bytes)
        except Exception as exc:  # noqa: BLE001 - source.poll records provider errors
            raise ApiListDictsProviderError(
                f"api_list_dicts HTTP GET failed: {_redact_provider_message(exc)}"
            ) from exc
        response_bytes = len(response.body)
        if response_bytes > config.max_bytes:
            raise ApiListDictsProviderError(
                f"api_list_dicts response exceeded max_bytes={config.max_bytes}"
            )
        if response.status_code < 200 or response.status_code >= 300:
            raise ApiListDictsProviderError(
                f"api_list_dicts HTTP GET returned status {response.status_code}"
            )

        records_value = _resolve_pointer(json.loads(response.body), config.list_path)
        if records_value is _MISSING:
            raise ValueError(f"api_list_dicts list_path not found: {config.list_path}")
        if not isinstance(records_value, list):
            raise ValueError("api_list_dicts list_path must resolve to a JSON array")
        truncated = (
            config.max_items is not None and len(records_value) > config.max_items
        )
        records = (
            records_value
            if config.max_items is None
            else records_value[: config.max_items]
        )
        warnings: list[str] = []
        if truncated:
            warnings.append(
                f"api_list_dicts item cap reached; limited to {config.max_items} items"
            )

        items: list[SourcePollItem] = []
        source_item_ids: list[str] = []
        record_keys: list[str] = []
        for position, record in enumerate(records):
            if not isinstance(record, dict):
                warnings.append(
                    "api_list_dicts skipped non-dict item at "
                    f"{config.list_path}[{position}]"
                )
                continue
            for key in record:
                if key not in record_keys:
                    record_keys.append(key)
            item, item_warnings = _poll_item_from_record(
                record,
                config=config,
                source_url=config.url,
                fetched_at=fetched_at,
                position=position,
            )
            warnings.extend(item_warnings)
            items.append(item)
            source_item_ids.append(item.stable_item_id())

        cursor_after = {
            "schema_version": API_LIST_DICTS_CURSOR_SCHEMA,
            "source_kind": API_LIST_DICTS_KIND,
            "url_hash": _text_hash(config.url),
            "url_host": _url_host(config.url),
            "list_path": config.list_path,
            "item_id_path": config.item_id_path,
            "updated_at_path": config.updated_at_path,
            "response_bytes": response_bytes,
            "items_seen": len(records),
            "items_materialized": len(items),
            "record_keys": record_keys,
            "source_item_ids_hash": _hash_json(sorted(source_item_ids)),
            "max_bytes": config.max_bytes,
            "max_items": config.max_items,
            "truncated": truncated,
            "warning_count": len(warnings),
            "pages_fetched": 1,
        }
        provider_use = [
            {
                "provider": "http",
                "service": "https_get",
                "source_kind": API_LIST_DICTS_KIND,
                "url_hash": _text_hash(config.url),
                "url_host": _url_host(config.url),
                "status_code": response.status_code,
                "response_bytes": response_bytes,
                "items_seen": len(records),
                "items_materialized": len(items),
                "max_bytes": config.max_bytes,
                "max_items": config.max_items,
                "pages_fetched": 1,
                "external_api": True,
                "cost_actual": 0.0,
            }
        ]
        return SourcePollResult(
            items=items,
            cursor_after=cursor_after,
            warnings=warnings,
            provider_use=provider_use,
            cost={"cost_micro": 0},
            summary={
                "schema_version": API_LIST_DICTS_CURSOR_SCHEMA,
                "source_kind": API_LIST_DICTS_KIND,
                "url_hash": _text_hash(config.url),
                "url_host": _url_host(config.url),
                "list_path": config.list_path,
                "item_id_path": config.item_id_path,
                "updated_at_path": config.updated_at_path,
                "items_seen": len(records),
                "items_materialized": len(items),
                "record_keys": record_keys,
                "response_bytes": response_bytes,
                "max_bytes": config.max_bytes,
                "max_items": config.max_items,
                "truncated": truncated,
                "warnings": len(warnings),
                "pages_fetched": 1,
            },
        )


def fetch_api_list_dicts_get(
    url: str,
    *,
    max_bytes: int,
    timeout: float = 20.0,
) -> ApiListDictsHttpResponse:
    """Fetch one public HTTPS JSON response with SSRF and byte guards."""
    import httpx

    if not url_is_safe(url):
        raise ApiListDictsProviderError(f"blocked URL: {_redact_url(url)}")
    with httpx.Client(follow_redirects=False, timeout=timeout) as client:
        with client.stream(
            "GET",
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "frisket-api-list-dicts-source/1",
            },
        ) as resp:
            content_length = resp.headers.get("content-length")
            if content_length is not None:
                try:
                    expected = int(content_length)
                except ValueError:
                    expected = None
                if expected is not None and expected > max_bytes:
                    raise ApiListDictsProviderError(
                        f"api_list_dicts response exceeded max_bytes={max_bytes}"
                    )
            chunks: list[bytes] = []
            total = 0
            for chunk in resp.iter_bytes():
                total += len(chunk)
                if total > max_bytes:
                    raise ApiListDictsProviderError(
                        f"api_list_dicts response exceeded max_bytes={max_bytes}"
                    )
                chunks.append(chunk)
            return ApiListDictsHttpResponse(
                status_code=int(resp.status_code),
                body=b"".join(chunks),
                final_url=str(resp.url),
                headers={
                    "content-type": resp.headers.get("content-type", ""),
                    "content-length": resp.headers.get("content-length", ""),
                },
            )


def register_api_list_dicts_poller(
    http_get: Callable[..., ApiListDictsHttpResponse] | None = None,
    *,
    now: Callable[[], datetime] | None = None,
    replace: bool = False,
) -> ApiListDictsPoller:
    poller = ApiListDictsPoller(http_get=http_get, now=now)
    register_source_poller(poller, replace=replace)
    return poller


def _resolve_config(
    source: dict[str, Any],
) -> tuple[ApiListDictsConfig | None, str | None]:
    raw_config = source.get("config") if isinstance(source.get("config"), dict) else {}
    assert isinstance(raw_config, dict)
    disallowed = sorted(set(raw_config) & _DISALLOWED_CONFIG_KEYS)
    if disallowed:
        return (
            None,
            "api_list_dicts does not support auth, headers, request bodies, "
            f"or secrets in source config: {', '.join(disallowed)}",
        )
    unsupported = sorted(set(raw_config) - _ALLOWED_CONFIG_KEYS)
    if unsupported:
        return None, f"api_list_dicts unsupported config field: {unsupported[0]}"

    method = raw_config.get("method", "GET")
    if not isinstance(method, str) or method.strip().upper() != "GET":
        return None, "api_list_dicts method must be GET"
    url, url_error = _validated_url(raw_config.get("url") or source.get("url"))
    if url_error:
        return None, url_error
    assert url is not None

    list_path, path_error = _validated_pointer(
        raw_config.get("list_path", "/"),
        field_name="list_path",
        allow_none=False,
    )
    if path_error:
        return None, path_error
    item_id_path, id_path_error = _validated_pointer(
        raw_config.get("item_id_path"),
        field_name="item_id_path",
        allow_none=True,
    )
    if id_path_error:
        return None, id_path_error
    updated_at_path, updated_path_error = _validated_pointer(
        raw_config.get("updated_at_path"),
        field_name="updated_at_path",
        allow_none=True,
    )
    if updated_path_error:
        return None, updated_path_error

    max_bytes, bytes_error = _config_positive_int(
        raw_config,
        "max_bytes",
        default=DEFAULT_MAX_BYTES,
        maximum=None,
    )
    if bytes_error:
        return None, bytes_error
    max_items, items_error = _config_positive_int(
        raw_config,
        "max_items",
        default=DEFAULT_MAX_ITEMS,
        maximum=DEFAULT_MAX_ITEMS,
    )
    if items_error:
        return None, items_error
    schema_policy = raw_config.get("schema_policy", "additive")
    if schema_policy != "additive":
        return None, "api_list_dicts schema_policy must be additive"

    return (
        ApiListDictsConfig(
            url=url,
            method="GET",
            list_path=list_path or "/",
            item_id_path=item_id_path,
            updated_at_path=updated_at_path,
            max_bytes=max_bytes,
            max_items=max_items,
            schema_policy="additive",
        ),
        None,
    )


def _validated_url(value: Any) -> tuple[str | None, str | None]:
    if not isinstance(value, str) or not value.strip():
        return None, "api_list_dicts sources require an HTTPS url"
    url = value.strip()
    parsed = urlparse(url)
    if parsed.scheme.lower() != "https":
        return None, "api_list_dicts url must use HTTPS"
    if not parsed.netloc or not parsed.hostname:
        return None, "api_list_dicts url must include a host"
    if parsed.username or parsed.password:
        return None, "api_list_dicts url must not include credentials"
    if parsed.fragment:
        return None, "api_list_dicts url must not include a fragment"
    for key, _value in parse_qsl(parsed.query, keep_blank_values=True):
        normalized = key.strip().lower().replace("-", "_")
        if normalized in _SENSITIVE_QUERY_KEYS:
            return None, "api_list_dicts url must not include secret query parameters"
    return url, None


def _validated_pointer(
    value: Any,
    *,
    field_name: str,
    allow_none: bool,
) -> tuple[str | None, str | None]:
    if value is None:
        if allow_none:
            return None, None
        value = "/"
    if not isinstance(value, str):
        return None, f"api_list_dicts {field_name} must be a JSON Pointer"
    text = value.strip()
    if text in _ROOT_POINTERS:
        return "/", None
    if not text.startswith("/"):
        return None, f"api_list_dicts {field_name} must be a JSON Pointer"
    for segment in text.split("/")[1:]:
        index = 0
        while index < len(segment):
            if segment[index] != "~":
                index += 1
                continue
            if index + 1 >= len(segment) or segment[index + 1] not in {"0", "1"}:
                return None, f"api_list_dicts {field_name} has invalid escape"
            index += 2
    return text, None


def _config_positive_int(
    config: dict[str, Any],
    field_name: str,
    *,
    default: int | None,
    maximum: int | None,
) -> tuple[int | None, str | None]:
    value = config.get(field_name, default)
    if value is None and default is None:
        return None, None
    if type(value) is not int or value <= 0:
        return None, f"api_list_dicts {field_name} must be a positive integer"
    if maximum is not None and value > maximum:
        return None, f"api_list_dicts {field_name} must be <= {maximum}"
    return int(value), None


def _poll_item_from_record(
    raw: dict[str, Any],
    *,
    config: ApiListDictsConfig,
    source_url: str,
    fetched_at: str,
    position: int,
) -> tuple[SourcePollItem, list[str]]:
    warnings: list[str] = []
    source_item_id = _record_source_item_id(raw, config.item_id_path)
    if source_item_id is None:
        source_item_id = _hash_json(raw)
        if config.item_id_path is not None:
            warnings.append(
                "api_list_dicts item_id_path did not resolve to a scalar at "
                f"{config.list_path}[{position}]; using record hash"
            )
    source_updated_at = _record_scalar_text(raw, config.updated_at_path)

    row = dict(raw)
    row["source_fetched_at"] = fetched_at
    if source_updated_at is not None:
        row["source_updated_at"] = source_updated_at
    return (
        SourcePollItem(
            source_item_id=source_item_id,
            dedupe_key=source_item_id,
            item_hash=_hash_json(raw),
            url=source_url,
            row=row,
            raw=raw,
        ),
        warnings,
    )


def _record_source_item_id(record: dict[str, Any], path: str | None) -> str | None:
    if path is None:
        return None
    value = _resolve_pointer(record, path)
    if value is _MISSING or value is None or isinstance(value, dict | list):
        return None
    if isinstance(value, str):
        return value.strip() or None
    return json.dumps(value, separators=(",", ":"))


def _record_scalar_text(record: dict[str, Any], path: str | None) -> str | None:
    if path is None:
        return None
    value = _resolve_pointer(record, path)
    if value is _MISSING or value is None or isinstance(value, dict | list):
        return None
    if isinstance(value, str):
        return value.strip() or None
    return json.dumps(value, separators=(",", ":"))


def _resolve_pointer(value: Any, pointer: str | None) -> Any:
    if pointer in _ROOT_POINTERS or pointer is None:
        return value
    current = value
    for raw_segment in pointer.split("/")[1:]:
        segment = raw_segment.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict):
            if segment not in current:
                return _MISSING
            current = current[segment]
            continue
        if isinstance(current, list):
            if not segment.isdecimal():
                return _MISSING
            index = int(segment)
            if index >= len(current):
                return _MISSING
            current = current[index]
            continue
        return _MISSING
    return current


def _iso_timestamp(value: datetime) -> str:
    dt = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _hash_json(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _text_hash(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _url_host(url: str) -> str | None:
    return urlparse(url).hostname


def _redact_provider_message(exc: BaseException) -> str:
    message = str(exc) or exc.__class__.__name__
    return _SENSITIVE_RE.sub(r"\1=<redacted>", message)


def _redact_url(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.netloc.split("@")[-1]
    base = f"{parsed.scheme}://{host}{parsed.path}"
    return base + ("?<redacted>" if parsed.query else "")


register_api_list_dicts_poller()
