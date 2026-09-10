"""RSS source.poll adapter."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from frisket import ingest
from frisket.ingest import (
    IngestError,
    _content_fingerprint,
    _entry_enclosures,
    _item_key,
    _map_entry,
)
from frisket.server.sources.runtime import (
    SourcePollContext,
    SourcePollItem,
    SourcePollResult,
    get_source_poller,
    register_source_poller,
)


RSS_KIND = "rss"


@dataclass
class RssPoller:
    fetch: Callable[[str], str] | None = None
    kind: str = RSS_KIND

    def validate_config(self, source: dict[str, Any]) -> str | None:
        if not source.get("url"):
            return "RSS sources require a url"
        config = source.get("config") or {}
        if isinstance(config, str):
            try:
                config = json.loads(config or "{}")
            except (TypeError, ValueError):
                return "RSS source config must be valid JSON"
        if not isinstance(config, dict):
            return "RSS source config must be an object"
        merge = config.get("merge_strategy", "append-new")
        if merge != "append-new":
            return "RSS source.poll supports only append-new merge strategy"
        return None

    def poll(self, ctx: SourcePollContext) -> SourcePollResult:
        import feedparser

        url = str(ctx.source.get("url") or "")
        if not url:
            raise IngestError("rss source has no url")
        text = (self.fetch or ingest.fetch_feed_text)(url)
        parsed = feedparser.parse(text)
        if getattr(parsed, "bozo", False):
            exc = getattr(parsed, "bozo_exception", None)
            raise IngestError(f"rss parse error: {exc}" if exc else "rss parse error")

        cursor = _cursor_dict(ctx.cursor_before)
        next_cursor = dict(cursor)
        items: list[SourcePollItem] = []
        for entry in parsed.entries:
            key = _item_key(entry)
            row = _map_entry(entry, key)
            fingerprint = _content_fingerprint(row)
            next_cursor[key] = fingerprint
            enclosures = _entry_enclosures(entry)
            items.append(
                SourcePollItem(
                    dedupe_key=key,
                    source_item_id=key,
                    item_hash=fingerprint,
                    row=row,
                    title=row.get("title"),
                    url=row.get("link"),
                    published_at=row.get("published"),
                    raw=row,
                    media=enclosures,
                )
            )
        feed = getattr(parsed, "feed", {}) or {}
        return SourcePollResult(
            items=items,
            cursor_after=next_cursor,
            provider_use=[
                {
                    "provider": "rss",
                    "service": "rss_fetch",
                    "external_api": True,
                    "cost_actual": 0.0,
                }
            ],
            summary={"feed_title": feed.get("title"), "item_count": len(items)},
        )


def register_rss_poller(*, replace: bool = False) -> RssPoller:
    poller = RssPoller()
    register_source_poller(poller, replace=replace)
    return poller


def ensure_rss_poller() -> None:
    if get_source_poller(RSS_KIND) is None:
        register_rss_poller()


def rss_poller_for_fetch_override(
    source_kind: str,
    fetch: Callable[[str], str] | None,
) -> RssPoller | None:
    if source_kind != RSS_KIND or fetch is None:
        return None
    return RssPoller(fetch=fetch)


def _cursor_dict(raw: str | None) -> dict[str, str]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    if not isinstance(value, dict):
        return {}
    return {str(key): str(item) for key, item in value.items()}
