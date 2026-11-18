"""Host-owned feed checker envelope.

Feed checkers normalize provider data into ``SourcePollResult``. They do not receive a
Project handle; the executor owns dedupe, row materialization, source_runs, and
receipts.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol
from collections.abc import Callable

from frisket.redaction import redact_text


@dataclass(frozen=True)
class SourcePollContext:
    source: dict[str, Any]
    cursor_before: str | None
    on_progress: Callable[[int, int | None], None] | None = None
    should_cancel: Callable[[], bool] | None = None


@dataclass(frozen=True)
class SourcePollItem:
    dedupe_key: str
    row: dict[str, Any]
    source_item_id: str | None = None
    item_hash: str | None = None
    title: str | None = None
    url: str | None = None
    published_at: str | None = None
    updated_at: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    media: list[dict[str, Any]] = field(default_factory=list)
    artifacts: list[dict[str, Any]] = field(default_factory=list)

    def stable_item_id(self) -> str:
        return self.source_item_id or self.dedupe_key

    def stable_hash(self) -> str:
        if self.item_hash:
            return self.item_hash
        payload = {
            "row": self.row,
            "raw": self.raw,
            "media": self.media,
            "artifacts": self.artifacts,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SourcePollResult:
    items: list[SourcePollItem] = field(default_factory=list)
    cursor_after: str | dict[str, Any] | None = None
    warnings: list[str] = field(default_factory=list)
    provider_use: list[dict[str, Any]] = field(default_factory=list)
    cost: dict[str, Any] | None = None
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)


class SourcePoller(Protocol):
    kind: str

    def validate_config(self, source: dict[str, Any]) -> str | None: ...

    def poll(self, ctx: SourcePollContext) -> SourcePollResult: ...


_FEED_CHECKERS: dict[str, SourcePoller] = {}


def register_feed_checker(feed_checker: SourcePoller, *, replace: bool = False) -> None:
    kind = str(feed_checker.kind).strip()
    if not kind:
        raise ValueError("source poller kind must be non-empty")
    existing = _FEED_CHECKERS.get(kind)
    if existing is not None and existing is not feed_checker and not replace:
        raise ValueError(f"source poller kind already registered: {kind}")
    _FEED_CHECKERS[kind] = feed_checker


def unregister_feed_checker(kind: str) -> None:
    _FEED_CHECKERS.pop(kind, None)


def get_feed_checker(kind: str) -> SourcePoller | None:
    return _FEED_CHECKERS.get(kind)


def registered_source_kinds() -> tuple[str, ...]:
    return tuple(sorted(_FEED_CHECKERS))


def encode_cursor(value: str | dict[str, Any] | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def free_listing_result(
    items: list[SourcePollItem],
    warnings: list[str],
    *,
    cursor: dict[str, Any],
    summary: dict[str, Any],
    provider_use: dict[str, Any],
) -> SourcePollResult:
    """Result of an unbilled external listing poll.

    ``provider_use`` keys (including provider-reported facts) win over the
    external/zero-cost markers; warning counts are taken from the final list.
    """
    return SourcePollResult(
        items=items,
        cursor_after={**cursor, "warning_count": len(warnings)},
        warnings=warnings,
        provider_use=[{"external_api": True, "cost_actual": 0.0, **provider_use}],
        cost={"cost_micro": 0},
        summary={**summary, "warnings": len(warnings)},
    )


def provider_error_text(exc: BaseException) -> str:
    """Redacted provider failure text for source-run records and warnings."""
    return redact_text(str(exc) or exc.__class__.__name__)


def config_positive_int(
    config: dict[str, Any],
    field_name: str,
    *,
    source_kind: str,
    default: int | None,
    maximum: int | None,
) -> tuple[int | None, str | None]:
    """Read an optional positive-int config knob; the error names the knob."""
    value = config.get(field_name, default)
    if value is None and default is None:
        return None, None
    if type(value) is not int or value <= 0:
        return None, f"{source_kind} {field_name} must be a positive integer"
    if maximum is not None and value > maximum:
        return None, f"{source_kind} {field_name} must be <= {maximum}"
    return int(value), None


def hash_json(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return text_hash(encoded)


def text_hash(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def iso_timestamp(value: datetime) -> str:
    dt = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")
