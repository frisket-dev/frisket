"""YouTube playlist and channel source pollers.

These sources list YouTube metadata only. They never download video, audio, or
thumbnail bytes; downstream media actions remain explicit user work.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlparse

from frisket.server.sources.runtime import (
    SourcePollContext,
    SourcePollItem,
    SourcePollResult,
    register_source_poller,
)
from frisket.features.url_classification.matchers import host_matches_domain

# The registered-domain suffix match (music/m/www.youtube.com all resolve to
# youtube.com) shared with the URL classification registry, so the poller host
# check and the firstparty.youtube.* matchers cannot drift.
_YOUTUBE_DOMAIN = "youtube.com"
_YOUTU_BE_DOMAIN = "youtu.be"


def _is_youtube_host(host: str) -> bool:
    return host_matches_domain(host, _YOUTUBE_DOMAIN)


def _is_youtube_or_short_host(host: str) -> bool:
    return _is_youtube_host(host) or host_matches_domain(host, _YOUTU_BE_DOMAIN)


YOUTUBE_PLAYLIST_KIND = "youtube_playlist"
YOUTUBE_CHANNEL_KIND = "youtube_channel"
# The collection enumerators the one-shot ``derive.collection_expand`` shares
# with the recurring pollers (ExpansionHint.enumerator names one of these).
YOUTUBE_COLLECTION_ENUMERATORS = frozenset(
    {YOUTUBE_CHANNEL_KIND, YOUTUBE_PLAYLIST_KIND}
)
YOUTUBE_CURSOR_SCHEMA = "frisket.youtube_cursor.v1"
YOUTUBE_SOURCE_SCHEMA = "frisket.source.youtube.v1"
DEFAULT_PAGE_SIZE = 100
_PLAYLIST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{2,}$")
_CHANNEL_ID_RE = re.compile(r"^UC[A-Za-z0-9_-]{6,}$")
_CHANNEL_HANDLE_RE = re.compile(r"^@[A-Za-z0-9._-]{3,30}$")
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,}$")
_SENSITIVE_RE = re.compile(
    r"(?i)(api[_-]?key|authorization|cookie|token|password|secret)"
    r"(\s*[:=]\s*(?:bearer\s+)?)?[^\s,;]+"
)


class YouTubeProviderError(RuntimeError):
    """Raised by playlist providers for redacted source.poll failures."""


@dataclass(frozen=True)
class YouTubePlaylistListing:
    playlist_id: str
    entries: list[dict[str, Any]]
    playlist_title: str | None = None
    # The playlist-level owner (yt-dlp's top-level info.channel_id/channel —
    # see list_youtube_playlist_with_ytdlp), used as the poll-time fallback
    # for entries whose OWN channel fields are null (extract_flat omits
    # per-entry channel data).
    channel_id: str | None = None
    channel_title: str | None = None
    provider_facts: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class YouTubeChannelListing:
    entries: list[dict[str, Any]]
    channel_id: str | None = None
    channel_handle: str | None = None
    channel_title: str | None = None
    provider_facts: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class YouTubePlaylistConfig:
    playlist_id: str
    playlist_url: str
    max_pages_per_poll: int | None
    item_cap: int | None


@dataclass(frozen=True)
class YouTubeChannelConfig:
    channel_url: str
    max_pages_per_poll: int | None
    item_cap: int | None
    channel_id: str | None = None
    channel_handle: str | None = None


YouTubePlaylistProvider = Callable[
    [str],
    YouTubePlaylistListing,
]


class YouTubePlaylistPoller:
    kind = YOUTUBE_PLAYLIST_KIND

    def __init__(
        self,
        provider: Callable[..., YouTubePlaylistListing] | None = None,
    ) -> None:
        self._provider = provider or list_youtube_playlist_with_ytdlp

    def validate_config(self, source: dict[str, Any]) -> str | None:
        _config, error = _resolve_playlist_config(source)
        return error

    def poll(self, ctx: SourcePollContext) -> SourcePollResult:
        config, error = _resolve_playlist_config(ctx.source)
        if error:
            raise ValueError(error)

        assert config is not None
        try:
            listing = self._provider(
                config.playlist_url,
                playlist_id=config.playlist_id,
                max_pages=config.max_pages_per_poll,
                item_cap=config.item_cap,
            )
        except Exception as exc:  # noqa: BLE001 - source.poll records provider errors
            raise YouTubeProviderError(
                f"YouTube playlist provider failed: {_redact_provider_message(exc)}"
            ) from exc

        entries = list(listing.entries or [])
        effective_cap = _effective_item_cap(
            max_pages=config.max_pages_per_poll,
            item_cap=config.item_cap,
        )
        truncated = effective_cap is not None and len(entries) > effective_cap
        if effective_cap is not None:
            entries = entries[:effective_cap]

        items: list[SourcePollItem] = []
        warnings = [_redact_warning(warning) for warning in listing.warnings]
        seen_video_ids: list[str] = []
        newest_published_at: str | None = None
        for position, entry in enumerate(entries, start=1):
            item, item_warnings = _poll_item_from_entry(
                entry,
                playlist_id=config.playlist_id,
                position=position,
                # The playlist-level owner fills an entry's channel_id/
                # channel_title when the entry's own are absent (the
                # flat-listing case).
                channel_id_fallback=listing.channel_id,
                channel_title_fallback=listing.channel_title,
            )
            warnings.extend(item_warnings)
            if item is None:
                continue
            items.append(item)
            video_id = str(item.row["video_id"])
            seen_video_ids.append(video_id)
            published_at = item.published_at
            if published_at and (
                newest_published_at is None or published_at > newest_published_at
            ):
                newest_published_at = published_at

        if truncated:
            warnings.append(
                f"youtube_playlist item cap reached; limited to {effective_cap} items"
            )

        pages_fetched = _positive_int(
            listing.provider_facts.get("pages_fetched"),
            default=_estimated_pages(len(entries)),
            maximum=config.max_pages_per_poll,
        )
        cursor_after = {
            "schema_version": YOUTUBE_CURSOR_SCHEMA,
            "provider": "youtube",
            "source_kind": YOUTUBE_PLAYLIST_KIND,
            "playlist_id": config.playlist_id,
            "items_seen": len(seen_video_ids),
            "seen_video_ids_hash": _hash_json(sorted(seen_video_ids)),
            "newest_published_at": newest_published_at,
            "pages_fetched": pages_fetched,
            "max_pages_per_poll": config.max_pages_per_poll,
            "item_cap": config.item_cap,
            "truncated": bool(truncated or listing.provider_facts.get("truncated")),
            "warning_count": len(warnings),
        }
        provider_use = [
            {
                "provider": "youtube",
                "service": str(listing.provider_facts.get("service") or "yt-dlp"),
                "source_kind": YOUTUBE_PLAYLIST_KIND,
                "playlist_id": config.playlist_id,
                "playlist_url_hash": _text_hash(config.playlist_url),
                "items_returned": len(entries),
                "items_materialized": len(items),
                "pages_fetched": pages_fetched,
                "max_pages_per_poll": config.max_pages_per_poll,
                "item_cap": config.item_cap,
                "download": False,
                "external_api": True,
                "cost_actual": 0.0,
                **{
                    str(k): v
                    for k, v in listing.provider_facts.items()
                    if k not in {"service", "pages_fetched"}
                },
            }
        ]
        return SourcePollResult(
            items=items,
            cursor_after=cursor_after,
            warnings=warnings,
            provider_use=provider_use,
            cost={"cost_micro": 0},
            summary={
                "schema_version": YOUTUBE_CURSOR_SCHEMA,
                "playlist_id": config.playlist_id,
                "playlist_title": listing.playlist_title,
                "channel_title": listing.channel_title,
                "items_seen": len(seen_video_ids),
                "items_materialized": len(items),
                "warnings": len(warnings),
                "pages_fetched": pages_fetched,
                "max_pages_per_poll": config.max_pages_per_poll,
                "item_cap": config.item_cap,
                "truncated": bool(truncated or listing.provider_facts.get("truncated")),
                "newest_published_at": newest_published_at,
            },
        )


class YouTubeChannelPoller:
    kind = YOUTUBE_CHANNEL_KIND

    def __init__(
        self,
        provider: Callable[..., YouTubeChannelListing] | None = None,
    ) -> None:
        self._provider = provider or list_youtube_channel_with_ytdlp

    def validate_config(self, source: dict[str, Any]) -> str | None:
        _config, error = _resolve_channel_config(source)
        return error

    def poll(self, ctx: SourcePollContext) -> SourcePollResult:
        config, error = _resolve_channel_config(ctx.source)
        if error:
            raise ValueError(error)

        assert config is not None
        try:
            listing = self._provider(
                config.channel_url,
                channel_id=config.channel_id,
                channel_handle=config.channel_handle,
                max_pages=config.max_pages_per_poll,
                item_cap=config.item_cap,
            )
        except Exception as exc:  # noqa: BLE001 - source.poll records provider errors
            raise YouTubeProviderError(
                f"YouTube channel provider failed: {_redact_provider_message(exc)}"
            ) from exc

        entries = list(listing.entries or [])
        effective_cap = _effective_item_cap(
            max_pages=config.max_pages_per_poll,
            item_cap=config.item_cap,
        )
        truncated = effective_cap is not None and len(entries) > effective_cap
        if effective_cap is not None:
            entries = entries[:effective_cap]

        channel_id = listing.channel_id or config.channel_id
        channel_handle = listing.channel_handle or config.channel_handle
        channel_title = listing.channel_title
        items: list[SourcePollItem] = []
        warnings = [_redact_warning(warning) for warning in listing.warnings]
        seen_video_ids: list[str] = []
        newest_published_at: str | None = None
        for position, entry in enumerate(entries, start=1):
            item, item_warnings = _poll_item_from_entry(
                entry,
                playlist_id=None,
                position=position,
                warning_kind=YOUTUBE_CHANNEL_KIND,
                channel_id_fallback=channel_id,
                channel_title_fallback=channel_title,
            )
            warnings.extend(item_warnings)
            if item is None:
                continue
            items.append(item)
            video_id = str(item.row["video_id"])
            seen_video_ids.append(video_id)
            published_at = item.published_at
            if published_at and (
                newest_published_at is None or published_at > newest_published_at
            ):
                newest_published_at = published_at

        if truncated:
            warnings.append(
                f"youtube_channel item cap reached; limited to {effective_cap} items"
            )

        pages_fetched = _positive_int(
            listing.provider_facts.get("pages_fetched"),
            default=_estimated_pages(len(entries)),
            maximum=config.max_pages_per_poll,
        )
        cursor_after = {
            "schema_version": YOUTUBE_CURSOR_SCHEMA,
            "provider": "youtube",
            "source_kind": YOUTUBE_CHANNEL_KIND,
            "channel_id": channel_id,
            "channel_handle": channel_handle,
            "items_seen": len(seen_video_ids),
            "seen_video_ids_hash": _hash_json(sorted(seen_video_ids)),
            "newest_published_at": newest_published_at,
            "pages_fetched": pages_fetched,
            "max_pages_per_poll": config.max_pages_per_poll,
            "item_cap": config.item_cap,
            "truncated": bool(truncated or listing.provider_facts.get("truncated")),
            "warning_count": len(warnings),
        }
        provider_use = [
            {
                "provider": "youtube",
                "service": str(listing.provider_facts.get("service") or "yt-dlp"),
                "source_kind": YOUTUBE_CHANNEL_KIND,
                "channel_id": channel_id,
                "channel_handle": channel_handle,
                "channel_url_hash": _text_hash(config.channel_url),
                "items_returned": len(entries),
                "items_materialized": len(items),
                "pages_fetched": pages_fetched,
                "max_pages_per_poll": config.max_pages_per_poll,
                "item_cap": config.item_cap,
                "download": False,
                "external_api": True,
                "cost_actual": 0.0,
                **{
                    str(k): v
                    for k, v in listing.provider_facts.items()
                    if k not in {"service", "pages_fetched"}
                },
            }
        ]
        return SourcePollResult(
            items=items,
            cursor_after=cursor_after,
            warnings=warnings,
            provider_use=provider_use,
            cost={"cost_micro": 0},
            summary={
                "schema_version": YOUTUBE_CURSOR_SCHEMA,
                "channel_id": channel_id,
                "channel_handle": channel_handle,
                "channel_title": channel_title,
                "items_seen": len(seen_video_ids),
                "items_materialized": len(items),
                "warnings": len(warnings),
                "pages_fetched": pages_fetched,
                "max_pages_per_poll": config.max_pages_per_poll,
                "item_cap": config.item_cap,
                "truncated": bool(truncated or listing.provider_facts.get("truncated")),
                "newest_published_at": newest_published_at,
            },
        )


def list_youtube_playlist_with_ytdlp(
    playlist_url: str,
    *,
    playlist_id: str,
    max_pages: int | None,
    item_cap: int | None,
) -> YouTubePlaylistListing:
    """List playlist entries with yt-dlp metadata extraction only."""
    try:
        yt_dlp = importlib.import_module("yt_dlp")
    except ImportError as exc:
        raise YouTubeProviderError(
            "yt-dlp provider unavailable for YouTube playlist source polling"
        ) from exc

    bounded_cap = _effective_item_cap(max_pages=max_pages, item_cap=item_cap)
    opts = {
        "extract_flat": "in_playlist",
        "ignoreerrors": True,
        "noplaylist": False,
        "no_warnings": True,
        "noprogress": True,
        "quiet": True,
        "skip_download": True,
    }
    if bounded_cap is not None:
        opts["playlistend"] = bounded_cap
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(playlist_url, download=False)
    except Exception as exc:  # noqa: BLE001 - provider message is redacted upstream
        raise YouTubeProviderError(
            f"yt-dlp playlist listing failed: {_redact_provider_message(exc)}"
        ) from exc
    entries = [entry for entry in info["entries"] if isinstance(entry, dict)]
    facts = {
        "service": "yt-dlp",
        "extractor": info.get("extractor_key") or info.get("extractor"),
        "playlist_count": info.get("playlist_count") or len(entries),
        "pages_fetched": _estimated_pages(len(entries)),
        "truncated": bounded_cap is not None and len(entries) >= bounded_cap,
    }
    return YouTubePlaylistListing(
        playlist_id=str(info.get("id") or playlist_id),
        entries=entries,
        # The playlist's own title (distinct from any per-video title in
        # `entries`) -- yt-dlp's extract_flat playlist metadata carries it
        # directly on the top-level info dict, same as a channel's `title`
        # (see list_youtube_channel_with_ytdlp below): the URL
        # 'youtube.com/watch?v=...&list=...' is not a name -- this is.
        playlist_title=info.get("title"),
        # The playlist-LEVEL owner: yt-dlp's `extract_flat` per-entry dicts
        # carry NO channel/uploader/channel_id/uploader_id at all (confirmed
        # against yt-dlp's own extractor --
        # yt_dlp/extractor/youtube/_tab.py's `_extract_metadata_from_tabs`
        # populates `channel`/`channel_id`/`uploader`/`uploader_id` ONLY on
        # the top-level playlist `info` dict, from the playlist page's
        # owner/byline renderer -- never per-entry; YouTube's flat listing
        # omits per-video channel data entirely, only `title` survives).
        # These become `_poll_item_from_entry`'s channel_id_fallback/
        # channel_title_fallback below, same fallback keys the channel
        # poller already reads (`info.get("channel_id") or
        # info.get("uploader_id")`, `info.get("channel") or
        # info.get("uploader")`). ASSUMPTION: single-owner playlist -- every
        # entry in a YouTube playlist shares one uploader, so applying the
        # playlist's owner to every null-channel entry is safe unconditionally
        # (nothing to skip). A playlist provider that someday reports
        # heterogeneous per-entry ownership should carry it on the entry
        # itself, which already wins over this fallback in
        # `_poll_item_from_entry`.
        channel_id=info.get("channel_id") or info.get("uploader_id"),
        channel_title=info.get("channel") or info.get("uploader"),
        provider_facts={k: v for k, v in facts.items() if v is not None},
    )


def list_youtube_channel_with_ytdlp(
    channel_url: str,
    *,
    channel_id: str | None,
    channel_handle: str | None,
    max_pages: int | None,
    item_cap: int | None,
) -> YouTubeChannelListing:
    """List channel uploads with yt-dlp metadata extraction only."""
    try:
        yt_dlp = importlib.import_module("yt_dlp")
    except ImportError as exc:
        raise YouTubeProviderError(
            "yt-dlp provider unavailable for YouTube channel source polling"
        ) from exc

    bounded_cap = _effective_item_cap(max_pages=max_pages, item_cap=item_cap)
    opts = {
        # Channel tab entries returned by flat extraction omit per-video fields
        # such as upload_date/timestamp and description. Resolve each bounded
        # entry's metadata so the source contract can populate published_at and
        # description; skip_download still prevents media acquisition.
        "extract_flat": False,
        "ignoreerrors": True,
        "noplaylist": False,
        "no_warnings": True,
        "noprogress": True,
        "quiet": True,
        "skip_download": True,
    }
    if bounded_cap is not None:
        opts["playlistend"] = bounded_cap
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(channel_url, download=False)
    except Exception as exc:  # noqa: BLE001 - provider message is redacted upstream
        raise YouTubeProviderError(
            f"yt-dlp channel listing failed: {_redact_provider_message(exc)}"
        ) from exc
    entries = [entry for entry in info["entries"] if isinstance(entry, dict)]
    resolved_channel_id = (
        info.get("channel_id") or info.get("uploader_id") or channel_id
    )
    resolved_handle = info.get("channel") or channel_handle
    if resolved_handle and not resolved_handle.startswith("@"):
        resolved_handle = channel_handle
    facts = {
        "service": "yt-dlp",
        "extractor": info.get("extractor_key") or info.get("extractor"),
        "playlist_count": info.get("playlist_count") or len(entries),
        "pages_fetched": _estimated_pages(len(entries)),
        "truncated": bounded_cap is not None and len(entries) >= bounded_cap,
    }
    return YouTubeChannelListing(
        entries=entries,
        channel_id=resolved_channel_id,
        channel_handle=resolved_handle,
        channel_title=info.get("title") or info.get("uploader"),
        provider_facts={k: v for k, v in facts.items() if v is not None},
    )


def register_youtube_playlist_poller(
    provider: Callable[..., YouTubePlaylistListing] | None = None,
    *,
    replace: bool = False,
) -> YouTubePlaylistPoller:
    poller = YouTubePlaylistPoller(provider=provider)
    register_source_poller(poller, replace=replace)
    return poller


def register_youtube_channel_poller(
    provider: Callable[..., YouTubeChannelListing] | None = None,
    *,
    replace: bool = False,
) -> YouTubeChannelPoller:
    poller = YouTubeChannelPoller(provider=provider)
    register_source_poller(poller, replace=replace)
    return poller


def _effective_item_cap(*, max_pages: int | None, item_cap: int | None) -> int | None:
    limits = [
        limit
        for limit in (
            item_cap,
            None if max_pages is None else max_pages * DEFAULT_PAGE_SIZE,
        )
        if limit is not None
    ]
    return min(limits) if limits else None


@dataclass(frozen=True)
class CollectionEnumeration:
    """One-shot enumeration of a YouTube collection (channel/playlist).

    The read-only sibling of a recurring source poll: same config resolution
    (``_resolve_*_config``), same yt-dlp provider (``list_youtube_*_with_ytdlp``),
    and the same ``_poll_item_from_entry`` row shaping — so the recurring poller
    and the one-shot ``derive.collection_expand`` cannot drift.

    ``preview_count`` is the extract_flat TOTAL (the cost-gate input, from
    ``playlist_count``); ``rows`` is the shaped, provider-capped list of item rows.
    """

    enumerator: str
    rows: list[dict[str, Any]]
    preview_count: int
    truncated: bool
    identity: dict[str, Any]
    provider_facts: dict[str, Any]


def _shape_collection_rows(
    entries: list[dict[str, Any]],
    *,
    playlist_id: str | None,
    warning_kind: str,
    channel_id_fallback: str | None = None,
    channel_title_fallback: str | None = None,
) -> list[dict[str, Any]]:
    """Shape enumerated entries into item rows via the SAME per-entry shaper the
    pollers use (``_poll_item_from_entry``), skipping unavailable/id-less entries."""
    rows: list[dict[str, Any]] = []
    for position, entry in enumerate(entries, start=1):
        item, _warnings = _poll_item_from_entry(
            entry,
            playlist_id=playlist_id,
            position=position,
            warning_kind=warning_kind,
            channel_id_fallback=channel_id_fallback,
            channel_title_fallback=channel_title_fallback,
        )
        if item is None:
            continue
        rows.append(dict(item.row))
    return rows


def enumerate_youtube_collection(
    enumerator: str,
    url: str,
    *,
    item_cap: int | None,
    provider: Callable[..., Any] | None = None,
) -> CollectionEnumeration:
    """One-shot enumerate a channel/playlist URL, sharing the poller machinery.

    ``enumerator`` is an ExpansionHint enumerator key (``youtube_channel`` /
    ``youtube_playlist``). When present, ``item_cap`` bounds the materialized entry
    list; the returned
    ``preview_count`` is the true collection total for the cost-gate. ``provider``
    injects a fake at the SAME seam the poller tests use (no network).

    Raises ``ValueError`` for an unknown enumerator or a URL the poller config
    resolution rejects (not a channel/playlist URL for the given kind).
    """
    if enumerator not in YOUTUBE_COLLECTION_ENUMERATORS:
        raise ValueError(f"unknown youtube collection enumerator: {enumerator}")
    bounded_cap = None if item_cap is None else max(1, int(item_cap))

    if enumerator == YOUTUBE_CHANNEL_KIND:
        config, error = _resolve_channel_config({"config": {"url": url}})
        if error:
            raise ValueError(error)
        assert config is not None
        call = provider or list_youtube_channel_with_ytdlp
        listing = call(
            config.channel_url,
            channel_id=config.channel_id,
            channel_handle=config.channel_handle,
            max_pages=config.max_pages_per_poll,
            item_cap=bounded_cap,
        )
        entries = (
            listing.entries if bounded_cap is None else listing.entries[:bounded_cap]
        )
        identity = {
            "channel_id": listing.channel_id or config.channel_id,
            "channel_handle": listing.channel_handle or config.channel_handle,
            "channel_title": listing.channel_title,
            "channel_url": config.channel_url,
        }
        provider_facts = dict(listing.provider_facts)
        rows = _shape_collection_rows(
            entries,
            playlist_id=None,
            warning_kind=YOUTUBE_CHANNEL_KIND,
            channel_id_fallback=identity["channel_id"],
            channel_title_fallback=identity["channel_title"],
        )
    else:  # YOUTUBE_PLAYLIST_KIND
        config, error = _resolve_playlist_config({"config": {"url": url}})
        if error:
            raise ValueError(error)
        assert config is not None
        call = provider or list_youtube_playlist_with_ytdlp
        listing = call(
            config.playlist_url,
            playlist_id=config.playlist_id,
            max_pages=config.max_pages_per_poll,
            item_cap=bounded_cap,
        )
        entries = (
            listing.entries if bounded_cap is None else listing.entries[:bounded_cap]
        )
        identity = {
            "playlist_id": listing.playlist_id or config.playlist_id,
            "playlist_url": config.playlist_url,
            "playlist_title": listing.playlist_title,
        }
        provider_facts = dict(listing.provider_facts)
        rows = _shape_collection_rows(
            entries,
            playlist_id=config.playlist_id,
            warning_kind=YOUTUBE_PLAYLIST_KIND,
            # Same playlist-owner poll-time fallback as
            # YouTubePlaylistPoller.poll -- this one-shot enumerator shares
            # _poll_item_from_entry with the recurring poller, so it must
            # share the fallback too.
            channel_id_fallback=listing.channel_id,
            channel_title_fallback=listing.channel_title,
        )

    preview_count = _positive_int(
        provider_facts.get("playlist_count"), default=len(rows), maximum=None
    )
    preview_count = max(preview_count, len(rows))
    truncated = preview_count > len(rows) or bool(provider_facts.get("truncated"))
    return CollectionEnumeration(
        enumerator=enumerator,
        rows=rows,
        preview_count=preview_count,
        truncated=truncated,
        identity=identity,
        provider_facts=provider_facts,
    )


_COLLECTION_KIND_LABEL = {
    YOUTUBE_PLAYLIST_KIND: "YouTube playlist",
    YOUTUBE_CHANNEL_KIND: "YouTube channel",
}


def collection_display_name(enumerator: str, identity: dict[str, Any], url: str) -> str:
    """Name a YouTube playlist source after the playlist itself. yt-dlp's
    enumeration
    metadata carries the collection's own title (``identity["playlist_title"]``
    / ``identity["channel_title"]`` -- see ``enumerate_youtube_collection``);
    use it. Fall back to a URL-labeled placeholder ONLY when the title is
    absent (a private/unlisted playlist yt-dlp couldn't name, or a provider
    that never returned one) -- never let a raw watch/playlist URL stand in
    for a name when a real title is available.
    """
    title_key = (
        "channel_title" if enumerator == YOUTUBE_CHANNEL_KIND else "playlist_title"
    )
    title = _string_value(identity.get(title_key))
    if title:
        return title
    label = _COLLECTION_KIND_LABEL.get(enumerator, "YouTube collection")
    return f"{label}: {url}"


def _resolve_playlist_config(
    source: dict[str, Any],
) -> tuple[YouTubePlaylistConfig | None, str | None]:
    config = source.get("config") if isinstance(source.get("config"), dict) else {}
    assert isinstance(config, dict)
    if config.get("channel_id") or config.get("channel_handle"):
        return None, "youtube_playlist supports playlist URL or playlist_id only"
    playlist_value = (
        config.get("playlist_id")
        or config.get("playlist_url")
        or config.get("url")
        or source.get("url")
    )
    playlist_id, id_error = _playlist_id_from_value(playlist_value)
    if id_error:
        return None, id_error

    assert playlist_id is not None
    max_pages, pages_error = _config_positive_int(
        config,
        "max_pages_per_poll",
        default=None,
        maximum=None,
        source_kind=YOUTUBE_PLAYLIST_KIND,
    )
    if pages_error:
        return None, pages_error
    item_cap, cap_error = _config_positive_int(
        config,
        "max_items_per_poll",
        default=(None if max_pages is None else max_pages * DEFAULT_PAGE_SIZE),
        maximum=None,
        source_kind=YOUTUBE_PLAYLIST_KIND,
    )
    if cap_error:
        return None, cap_error
    return (
        YouTubePlaylistConfig(
            playlist_id=playlist_id,
            playlist_url=_playlist_url(playlist_id),
            max_pages_per_poll=max_pages,
            item_cap=item_cap,
        ),
        None,
    )


def _resolve_channel_config(
    source: dict[str, Any],
) -> tuple[YouTubeChannelConfig | None, str | None]:
    config = source.get("config") if isinstance(source.get("config"), dict) else {}
    assert isinstance(config, dict)
    if config.get("playlist_id") or config.get("playlist_url"):
        return None, "youtube_channel supports channel URL, channel_id, or @handle only"
    channel_value = (
        config.get("channel_id")
        or config.get("channel_handle")
        or config.get("channel_url")
        or config.get("url")
        or source.get("url")
    )
    parsed, id_error = _channel_identity_from_value(channel_value)
    if id_error:
        return None, id_error

    assert parsed is not None
    max_pages, pages_error = _config_positive_int(
        config,
        "max_pages_per_poll",
        default=None,
        maximum=None,
        source_kind=YOUTUBE_CHANNEL_KIND,
    )
    if pages_error:
        return None, pages_error
    item_cap, cap_error = _config_positive_int(
        config,
        "max_items_per_poll",
        default=(None if max_pages is None else max_pages * DEFAULT_PAGE_SIZE),
        maximum=None,
        source_kind=YOUTUBE_CHANNEL_KIND,
    )
    if cap_error:
        return None, cap_error
    return (
        YouTubeChannelConfig(
            channel_url=_channel_url(parsed),
            channel_id=parsed.get("channel_id"),
            channel_handle=parsed.get("channel_handle"),
            max_pages_per_poll=max_pages,
            item_cap=item_cap,
        ),
        None,
    )


def _playlist_id_from_value(value: Any) -> tuple[str | None, str | None]:
    if not isinstance(value, str) or not value.strip():
        return None, "youtube_playlist sources require playlist_id or playlist URL"
    text = value.strip()
    parsed = urlparse(text)
    if parsed.scheme in {"http", "https"}:
        host = parsed.netloc.lower().split("@")[-1].split(":", 1)[0]
        if not _is_youtube_or_short_host(host):
            return None, "youtube_playlist URL must be a YouTube playlist URL"
        playlist_id = (parse_qs(parsed.query).get("list") or [""])[0].strip()
        if playlist_id:
            if _PLAYLIST_ID_RE.fullmatch(playlist_id):
                return playlist_id, None
            return None, "youtube_playlist playlist_id is invalid"
        path = parsed.path.rstrip("/")
        if path.startswith(("/channel/", "/@", "/c/", "/user/")):
            return None, "youtube_playlist does not support channel config yet"
        return None, "youtube_playlist URL must include a list= playlist id"
    if text.startswith("@"):
        return None, "youtube_playlist does not support channel handles yet"
    if not _PLAYLIST_ID_RE.fullmatch(text):
        return None, "youtube_playlist playlist_id is invalid"
    return text, None


def _channel_identity_from_value(
    value: Any,
) -> tuple[dict[str, str] | None, str | None]:
    if not isinstance(value, str) or not value.strip():
        return (
            None,
            "youtube_channel sources require channel_id, @handle, or channel URL",
        )
    text = value.strip()
    parsed = urlparse(text)
    if parsed.scheme in {"http", "https"}:
        host = parsed.netloc.lower().split("@")[-1].split(":", 1)[0]
        if not _is_youtube_host(host):
            return None, "youtube_channel URL must be a YouTube channel URL"
        if parse_qs(parsed.query).get("list"):
            return None, "youtube_channel does not support playlist config"
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) >= 2 and parts[0] == "channel":
            channel_id = parts[1].strip()
            if _CHANNEL_ID_RE.fullmatch(channel_id):
                return {"channel_id": channel_id}, None
            return None, "youtube_channel channel_id is invalid"
        if len(parts) >= 1 and parts[0].startswith("@"):
            handle = parts[0].strip()
            if _CHANNEL_HANDLE_RE.fullmatch(handle):
                return {"channel_handle": handle}, None
            return None, "youtube_channel channel_handle is invalid"
        if len(parts) >= 2 and parts[0] in {"c", "user"}:
            slug = parts[1].strip()
            if re.fullmatch(r"[A-Za-z0-9._-]{3,80}", slug):
                return {"channel_handle": f"@{slug}"}, None
            return None, "youtube_channel channel URL slug is invalid"
        return None, "youtube_channel URL must identify a channel or @handle"
    if text.startswith("@"):
        if _CHANNEL_HANDLE_RE.fullmatch(text):
            return {"channel_handle": text}, None
        return None, "youtube_channel channel_handle is invalid"
    if _CHANNEL_ID_RE.fullmatch(text):
        return {"channel_id": text}, None
    return None, "youtube_channel channel_id or @handle is invalid"


def _config_positive_int(
    config: dict[str, Any],
    field_name: str,
    *,
    default: int | None,
    maximum: int | None,
    source_kind: str,
) -> tuple[int | None, str | None]:
    value = config.get(field_name, default)
    if value is None and default is None:
        return None, None
    if type(value) is not int or value <= 0:
        return None, f"{source_kind} {field_name} must be a positive integer"
    if maximum is not None and value > maximum:
        return None, f"{source_kind} {field_name} must be <= {maximum}"
    return int(value), None


def _poll_item_from_entry(
    entry: dict[str, Any],
    *,
    playlist_id: str | None,
    position: int,
    warning_kind: str = YOUTUBE_PLAYLIST_KIND,
    channel_id_fallback: str | None = None,
    channel_title_fallback: str | None = None,
) -> tuple[SourcePollItem | None, list[str]]:
    video_id = _entry_video_id(entry)
    title = entry.get("title")
    if _entry_unavailable(entry):
        label = title or video_id or f"position {position}"
        return None, [f"{warning_kind} skipped unavailable video: {label}"]
    if not video_id:
        label = title or f"position {position}"
        return None, [f"{warning_kind} skipped entry without video_id: {label}"]

    watch_url = _watch_url(video_id)
    channel_id = entry.get("channel_id") or channel_id_fallback
    channel_title = entry.get("channel") or channel_title_fallback
    published_at = _entry_published_at(entry)
    description = entry.get("description")
    thumbnail_url = _thumbnail_url(entry)
    row = {
        "video_id": video_id,
        "title": title or f"YouTube video {video_id}",
        "url": watch_url,
        "channel_id": channel_id,
        "channel_title": channel_title,
        "published_at": published_at,
        "position": position,
        "description": description,
        "thumbnail_url": thumbnail_url,
        "raw": entry,
    }
    if playlist_id is not None:
        row["playlist_id"] = playlist_id
    source_item_id = f"youtube:video:{video_id}"
    media_item = {
        "kind": "youtube_video",
        "provider": "youtube",
        "video_id": video_id,
        "watch_url": watch_url,
        **({"playlist_id": playlist_id} if playlist_id else {}),
        **({"channel_id": channel_id} if channel_id else {}),
    }
    media = [media_item]
    return (
        SourcePollItem(
            source_item_id=source_item_id,
            dedupe_key=source_item_id,
            item_hash=_normalized_item_hash(row, media),
            title=str(row["title"]),
            url=watch_url,
            published_at=published_at,
            row=row,
            raw=entry,
            media=media,
        ),
        [],
    )


def _entry_video_id(entry: dict[str, Any]) -> str | None:
    video_id = entry.get("id")
    return (
        video_id
        if isinstance(video_id, str) and _VIDEO_ID_RE.fullmatch(video_id)
        else None
    )


def _entry_unavailable(entry: dict[str, Any]) -> bool:
    values = [
        _string_value(entry.get("availability")),
        _string_value(entry.get("availability_status")),
        _string_value(entry.get("live_status")),
        _string_value(entry.get("title")),
    ]
    lowered = " ".join(value.lower() for value in values if value)
    return any(
        marker in lowered
        for marker in (
            "private video",
            "deleted video",
            "unavailable",
            "removed",
            "needs auth",
            "subscriber_only",
            "premium_only",
        )
    )


def _entry_published_at(entry: dict[str, Any]) -> str | None:
    for key in ("timestamp", "release_timestamp", "modified_timestamp"):
        value = entry.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return _iso_from_timestamp(float(value))
    for key in ("upload_date", "release_date", "published_at", "timestamp"):
        normalized = _date_or_iso(entry.get(key))
        if normalized:
            return normalized
    return None


def _date_or_iso(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if re.fullmatch(r"\d{8}", text):
        return (
            datetime(
                int(text[0:4]),
                int(text[4:6]),
                int(text[6:8]),
                tzinfo=UTC,
            )
            .isoformat()
            .replace("+00:00", "Z")
        )
    if "T" in text:
        return text
    return None


def _iso_from_timestamp(value: float) -> str:
    return datetime.fromtimestamp(value, tz=UTC).isoformat().replace("+00:00", "Z")


def _thumbnail_url(entry: dict[str, Any]) -> str | None:
    direct = _string_value(entry.get("thumbnail"))
    if direct:
        return direct
    thumbnails = entry.get("thumbnails")
    if isinstance(thumbnails, list):
        for item in reversed(thumbnails):
            if isinstance(item, dict):
                url = _string_value(item.get("url"))
                if url:
                    return url
    return None


def _string_value(value: Any) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return None


def _playlist_url(playlist_id: str) -> str:
    return f"https://www.youtube.com/playlist?list={playlist_id}"


def _channel_url(identity: dict[str, str]) -> str:
    channel_id = identity.get("channel_id")
    if channel_id:
        return f"https://www.youtube.com/channel/{channel_id}/videos"
    handle = identity["channel_handle"]
    return f"https://www.youtube.com/{handle}/videos"


def _watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def _estimated_pages(item_count: int) -> int:
    if item_count <= 0:
        return 0
    return max(1, (item_count + DEFAULT_PAGE_SIZE - 1) // DEFAULT_PAGE_SIZE)


def _positive_int(value: Any, *, default: int, maximum: int | None) -> int:
    if type(value) is int and value >= 0:
        return value if maximum is None else min(value, maximum)
    return default if maximum is None else min(default, maximum)


def _normalized_item_hash(row: dict[str, Any], media: list[dict[str, Any]]) -> str:
    payload = {
        "row": {
            key: row.get(key)
            for key in (
                "video_id",
                "title",
                "url",
                "channel_id",
                "channel_title",
                "playlist_id",
                "published_at",
                "position",
                "description",
                "thumbnail_url",
            )
        },
        "media": media,
    }
    return _hash_json(payload)


def _hash_json(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _text_hash(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _redact_provider_message(exc: BaseException) -> str:
    message = str(exc) or exc.__class__.__name__
    return _SENSITIVE_RE.sub(r"\1=<redacted>", message)


def _redact_warning(warning: Any) -> str:
    return _SENSITIVE_RE.sub(r"\1=<redacted>", str(warning))


register_youtube_playlist_poller()
register_youtube_channel_poller()
