"""Host-owned source poller envelope.

Pollers normalize provider data into ``SourcePollResult``. They do not receive a
Project handle; the executor owns dedupe, row materialization, source_runs, and
receipts.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class SourcePollContext:
    source: dict[str, Any]
    cursor_before: str | None


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


_POLLERS: dict[str, SourcePoller] = {}


def register_source_poller(poller: SourcePoller, *, replace: bool = False) -> None:
    kind = str(poller.kind).strip()
    if not kind:
        raise ValueError("source poller kind must be non-empty")
    existing = _POLLERS.get(kind)
    if existing is not None and existing is not poller and not replace:
        raise ValueError(f"source poller kind already registered: {kind}")
    _POLLERS[kind] = poller


def unregister_source_poller(kind: str) -> None:
    _POLLERS.pop(kind, None)


def get_source_poller(kind: str) -> SourcePoller | None:
    return _POLLERS.get(kind)


def registered_source_kinds() -> tuple[str, ...]:
    return tuple(sorted(_POLLERS))


def encode_cursor(value: str | dict[str, Any] | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True, separators=(",", ":"))
