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
from typing import Any
from urllib.parse import parse_qs, urlparse

from frisket.server.sources.media_metadata import (
    entry_published_at as _entry_published_at,
    thumbnail_url as _thumbnail_url,
    string_value as _string_value,
    redact_provider_message as _redact_provider_message,
    redact_warning as _redact_warning,
)
from frisket.server.sources.progress import listing_progress_options
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
YOUTUBE_CURSOR_SCHEMA = "frisket.youtube_cursor.v1"
DEFAULT_PAGE_SIZE = 100
_PLAYLIST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{2,}$")
_CHANNEL_ID_RE = re.compile(r"^UC[A-Za-z0-9_-]{6,}$")
_CHANNEL_HANDLE_RE = re.compile(r"^@[A-Za-z0-9._-]{3,30}$")
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,}$")


class YouTubeProviderError(RuntimeError):
    """Raised by playlist providers for redacted source.poll failures."""


@dataclass(frozen=True)
class YouTubePlaylistListing:
    playlist_id: str
    entries: list[dict[str, Any]]
    playlist_title: str | None = None
    # The playlist-level owner, used as the poll-time fallback for entries whose
    # own channel fields are null (extract_flat omits per-entry channel data).
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


YouTubeListing = YouTubePlaylistListing | YouTubeChannelListing
_Identity = dict[str, str | None]


@dataclass(frozen=True)
class YouTubeCollectionKind:
    """What differs between the playlist and channel collections.

    ``identity`` keys are the provider keyword arguments, the persisted cursor
    keys and the receipt identity keys, so they must not change.
    """

    kind: str
    noun: str
    value_keys: tuple[str, ...]
    conflicting_keys: tuple[str, ...]
    conflict_error: str
    parse_identity: Callable[[Any], tuple[_Identity | None, str | None]]
    collection_url: Callable[[_Identity], str]
    list_with_ytdlp: Callable[..., YouTubeListing]
    title_field: str
    # Channel listings resolve the canonical id/handle; a playlist poll keeps
    # the configured playlist id in its cursor.
    poll_identity_from_listing: bool


@dataclass(frozen=True)
class YouTubeCollectionConfig:
    url: str
    identity: _Identity
    max_pages_per_poll: int | None
    item_cap: int | None


class YouTubeCollectionPoller:
    def __init__(
        self,
        spec: YouTubeCollectionKind,
        provider: Callable[..., YouTubeListing] | None = None,
    ) -> None:
        self.kind = spec.kind
        self._spec = spec
        self._provider = provider or spec.list_with_ytdlp
        self._uses_default_provider = provider is None

    def validate_config(self, source: dict[str, Any]) -> str | None:
        _config, error = _resolve_config(self._spec, source)
        return error

    def poll(self, ctx: SourcePollContext) -> SourcePollResult:
        spec = self._spec
        config, error = _resolve_config(spec, ctx.source)
        if error:
            raise ValueError(error)

        assert config is not None
        try:
            listing = self._provider(
                config.url,
                **config.identity,
                max_pages=config.max_pages_per_poll,
                item_cap=config.item_cap,
                **(
                    {"on_progress": ctx.on_progress, "should_cancel": ctx.should_cancel}
                    if self._uses_default_provider
                    else {}
                ),
            )
        except Exception as exc:  # noqa: BLE001 - source.poll records provider errors
            raise YouTubeProviderError(
                f"YouTube {spec.noun} provider failed: {_redact_provider_message(exc)}"
            ) from exc

        entries = list(listing.entries or [])
        effective_cap = _effective_item_cap(
            max_pages=config.max_pages_per_poll,
            item_cap=config.item_cap,
        )
        truncated = effective_cap is not None and len(entries) > effective_cap
        if effective_cap is not None:
            entries = entries[:effective_cap]

        identity = (
            _listing_identity(config, listing)
            if spec.poll_identity_from_listing
            else dict(config.identity)
        )
        warnings = [_redact_warning(warning) for warning in listing.warnings]
        items, item_warnings = _shape_entries(spec, config, listing, identity, entries)
        warnings.extend(item_warnings)
        seen_video_ids = [str(item.row["video_id"]) for item in items]
        newest_published_at = max(
            (item.published_at for item in items if item.published_at), default=None
        )
        if truncated:
            warnings.append(
                f"{spec.kind} item cap reached; limited to {effective_cap} items"
            )

        pages_fetched = _positive_int(
            listing.provider_facts.get("pages_fetched"),
            default=_estimated_pages(len(entries)),
            maximum=config.max_pages_per_poll,
        )
        truncated = bool(truncated or listing.provider_facts.get("truncated"))
        cursor_after = {
            "schema_version": YOUTUBE_CURSOR_SCHEMA,
            "provider": "youtube",
            "source_kind": spec.kind,
            **identity,
            "items_seen": len(seen_video_ids),
            "seen_video_ids_hash": _hash_json(sorted(seen_video_ids)),
            "newest_published_at": newest_published_at,
            "pages_fetched": pages_fetched,
            "max_pages_per_poll": config.max_pages_per_poll,
            "item_cap": config.item_cap,
            "truncated": truncated,
            "warning_count": len(warnings),
        }
        provider_use = [
            {
                "provider": "youtube",
                "service": str(listing.provider_facts.get("service") or "yt-dlp"),
                "source_kind": spec.kind,
                **identity,
                f"{spec.noun}_url_hash": _text_hash(config.url),
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
                **identity,
                spec.title_field: getattr(listing, spec.title_field),
                "channel_title": listing.channel_title,
                "items_seen": len(seen_video_ids),
                "items_materialized": len(items),
                "warnings": len(warnings),
                "pages_fetched": pages_fetched,
                "max_pages_per_poll": config.max_pages_per_poll,
                "item_cap": config.item_cap,
                "truncated": truncated,
                "newest_published_at": newest_published_at,
            },
        )


def _listing_identity(
    config: YouTubeCollectionConfig, listing: YouTubeListing
) -> _Identity:
    return {
        key: getattr(listing, key) or value for key, value in config.identity.items()
    }


def _shape_entries(
    spec: YouTubeCollectionKind,
    config: YouTubeCollectionConfig,
    listing: YouTubeListing,
    identity: _Identity,
    entries: list[dict[str, Any]],
) -> tuple[list[SourcePollItem], list[str]]:
    """Shape listed entries into poll items, skipping unavailable/id-less ones.

    The listing-level owner fills an entry's channel fields when the entry's
    own are absent (yt-dlp's flat playlist listing omits them per entry).
    """
    items: list[SourcePollItem] = []
    warnings: list[str] = []
    for position, entry in enumerate(entries, start=1):
        item, item_warnings = _poll_item_from_entry(
            entry,
            playlist_id=config.identity.get("playlist_id"),
            position=position,
            warning_kind=spec.kind,
            channel_id_fallback=listing.channel_id or identity.get("channel_id"),
            channel_title_fallback=listing.channel_title,
        )
        warnings.extend(item_warnings)
        if item is not None:
            items.append(item)
    return items, warnings


def _ytdlp_listing(
    url: str,
    *,
    noun: str,
    extract_flat: str | bool,
    bounded_cap: int | None,
    progress: tuple[Any, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    """List a collection with yt-dlp metadata extraction only."""
    try:
        yt_dlp = importlib.import_module("yt_dlp")
    except ImportError as exc:
        raise YouTubeProviderError(
            f"yt-dlp provider unavailable for YouTube {noun} source polling"
        ) from exc

    opts = {
        "extract_flat": extract_flat,
        "ignoreerrors": True,
        "noplaylist": False,
        "no_warnings": True,
        "noprogress": True,
        "quiet": True,
        "skip_download": True,
    }
    opts.update(listing_progress_options(*progress, bounded_cap))
    if bounded_cap is not None:
        opts["playlistend"] = bounded_cap
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            entries = [entry for entry in info["entries"] if isinstance(entry, dict)]
    except Exception as exc:  # noqa: BLE001 - provider message is redacted upstream
        raise YouTubeProviderError(
            f"yt-dlp {noun} listing failed: {_redact_provider_message(exc)}"
        ) from exc
    facts = {
        "service": "yt-dlp",
        "extractor": info.get("extractor_key") or info.get("extractor"),
        "playlist_count": info.get("playlist_count") or len(entries),
        "pages_fetched": _estimated_pages(len(entries)),
        "truncated": bounded_cap is not None and len(entries) >= bounded_cap,
    }
    return info, entries, {k: v for k, v in facts.items() if v is not None}


def list_youtube_playlist_with_ytdlp(
    playlist_url: str,
    *,
    playlist_id: str,
    max_pages: int | None,
    item_cap: int | None,
    on_progress=None,
    should_cancel=None,
) -> YouTubePlaylistListing:
    info, entries, facts = _ytdlp_listing(
        playlist_url,
        noun="playlist",
        extract_flat="in_playlist",
        bounded_cap=_effective_item_cap(max_pages=max_pages, item_cap=item_cap),
        progress=(on_progress, should_cancel),
    )
    return YouTubePlaylistListing(
        playlist_id=str(info.get("id") or playlist_id),
        entries=entries,
        playlist_title=info.get("title"),
        # yt-dlp's extract_flat per-entry dicts carry no channel/uploader data;
        # the playlist page's owner is only on the top-level info dict. A
        # playlist has one uploader, so the owner applies to every entry whose
        # own channel fields are absent.
        channel_id=info.get("channel_id") or info.get("uploader_id"),
        channel_title=info.get("channel") or info.get("uploader"),
        provider_facts=facts,
    )


def list_youtube_channel_with_ytdlp(
    channel_url: str,
    *,
    channel_id: str | None,
    channel_handle: str | None,
    max_pages: int | None,
    item_cap: int | None,
    on_progress=None,
    should_cancel=None,
) -> YouTubeChannelListing:
    info, entries, facts = _ytdlp_listing(
        channel_url,
        noun="channel",
        # Channel tab entries returned by flat extraction omit per-video fields
        # such as upload_date/timestamp and description. Resolve each bounded
        # entry's metadata so the source contract can populate published_at
        # and description; skip_download still prevents media acquisition.
        extract_flat=False,
        bounded_cap=_effective_item_cap(max_pages=max_pages, item_cap=item_cap),
        progress=(on_progress, should_cancel),
    )
    resolved_handle = info.get("channel") or channel_handle
    if resolved_handle and not resolved_handle.startswith("@"):
        resolved_handle = channel_handle
    return YouTubeChannelListing(
        entries=entries,
        channel_id=info.get("channel_id") or info.get("uploader_id") or channel_id,
        channel_handle=resolved_handle,
        channel_title=info.get("title") or info.get("uploader"),
        provider_facts=facts,
    )


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

    The read-only sibling of a recurring source poll: the same config
    resolution, provider and row shaping as ``YouTubeCollectionPoller``.

    ``preview_count`` is the extract_flat TOTAL (the cost-gate input, from
    ``playlist_count``); ``rows`` is the shaped, provider-capped list of item rows.
    """

    enumerator: str
    rows: list[dict[str, Any]]
    preview_count: int
    truncated: bool
    identity: dict[str, Any]
    provider_facts: dict[str, Any]


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
    spec = _COLLECTION_KINDS[enumerator]
    bounded_cap = None if item_cap is None else max(1, int(item_cap))
    config, error = _resolve_config(spec, {"config": {"url": url}})
    if error:
        raise ValueError(error)
    assert config is not None
    listing = (provider or spec.list_with_ytdlp)(
        config.url,
        **config.identity,
        max_pages=config.max_pages_per_poll,
        item_cap=bounded_cap,
    )
    entries = listing.entries if bounded_cap is None else listing.entries[:bounded_cap]
    resolved = _listing_identity(config, listing)
    identity = {
        **resolved,
        spec.title_field: getattr(listing, spec.title_field),
        f"{spec.noun}_url": config.url,
    }
    items, _warnings = _shape_entries(spec, config, listing, resolved, entries)
    rows = [dict(item.row) for item in items]
    provider_facts = dict(listing.provider_facts)
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


def collection_display_name(enumerator: str, identity: dict[str, Any], url: str) -> str:
    """Name a YouTube collection source after the collection itself.

    Falls back to a URL-labeled placeholder only when the title is absent (a
    private/unlisted playlist yt-dlp couldn't name, or a provider that never
    returned one): a raw watch/playlist URL is not a name.
    """
    spec = _COLLECTION_KINDS.get(enumerator)
    title_field = spec.title_field if spec else "playlist_title"
    title = _string_value(identity.get(title_field))
    if title:
        return title
    label = f"YouTube {spec.noun}" if spec else "YouTube collection"
    return f"{label}: {url}"


def _resolve_config(
    spec: YouTubeCollectionKind,
    source: dict[str, Any],
) -> tuple[YouTubeCollectionConfig | None, str | None]:
    config = source.get("config") if isinstance(source.get("config"), dict) else {}
    assert isinstance(config, dict)
    if any(config.get(key) for key in spec.conflicting_keys):
        return None, spec.conflict_error
    for key in spec.value_keys:
        value = config.get(key)
        if value:
            break
    else:
        value = source.get("url")
    identity, id_error = spec.parse_identity(value)
    if id_error:
        return None, id_error

    assert identity is not None
    max_pages, pages_error = _config_positive_int(
        config,
        "max_pages_per_poll",
        default=None,
        maximum=None,
        source_kind=spec.kind,
    )
    if pages_error:
        return None, pages_error
    item_cap, cap_error = _config_positive_int(
        config,
        "max_items_per_poll",
        default=(None if max_pages is None else max_pages * DEFAULT_PAGE_SIZE),
        maximum=None,
        source_kind=spec.kind,
    )
    if cap_error:
        return None, cap_error
    return (
        YouTubeCollectionConfig(
            url=spec.collection_url(identity),
            identity=identity,
            max_pages_per_poll=max_pages,
            item_cap=item_cap,
        ),
        None,
    )


def _playlist_identity_from_value(value: Any) -> tuple[_Identity | None, str | None]:
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
                return {"playlist_id": playlist_id}, None
            return None, "youtube_playlist playlist_id is invalid"
        path = parsed.path.rstrip("/")
        if path.startswith(("/channel/", "/@", "/c/", "/user/")):
            return None, "youtube_playlist does not support channel config yet"
        return None, "youtube_playlist URL must include a list= playlist id"
    if text.startswith("@"):
        return None, "youtube_playlist does not support channel handles yet"
    if not _PLAYLIST_ID_RE.fullmatch(text):
        return None, "youtube_playlist playlist_id is invalid"
    return {"playlist_id": text}, None


def _channel_identity_from_value(value: Any) -> tuple[_Identity | None, str | None]:
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
                return {"channel_id": channel_id, "channel_handle": None}, None
            return None, "youtube_channel channel_id is invalid"
        if len(parts) >= 1 and parts[0].startswith("@"):
            handle = parts[0].strip()
            if _CHANNEL_HANDLE_RE.fullmatch(handle):
                return {"channel_id": None, "channel_handle": handle}, None
            return None, "youtube_channel channel_handle is invalid"
        if len(parts) >= 2 and parts[0] in {"c", "user"}:
            slug = parts[1].strip()
            if re.fullmatch(r"[A-Za-z0-9._-]{3,80}", slug):
                return {"channel_id": None, "channel_handle": f"@{slug}"}, None
            return None, "youtube_channel channel URL slug is invalid"
        return None, "youtube_channel URL must identify a channel or @handle"
    if text.startswith("@"):
        if _CHANNEL_HANDLE_RE.fullmatch(text):
            return {"channel_id": None, "channel_handle": text}, None
        return None, "youtube_channel channel_handle is invalid"
    if _CHANNEL_ID_RE.fullmatch(text):
        return {"channel_id": text, "channel_handle": None}, None
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


def _playlist_url(identity: _Identity) -> str:
    return f"https://www.youtube.com/playlist?list={identity['playlist_id']}"


def _channel_url(identity: _Identity) -> str:
    channel_id = identity["channel_id"]
    if channel_id:
        return f"https://www.youtube.com/channel/{channel_id}/videos"
    return f"https://www.youtube.com/{identity['channel_handle']}/videos"


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


YOUTUBE_PLAYLIST = YouTubeCollectionKind(
    kind=YOUTUBE_PLAYLIST_KIND,
    noun="playlist",
    value_keys=("playlist_id", "playlist_url", "url"),
    conflicting_keys=("channel_id", "channel_handle"),
    conflict_error="youtube_playlist supports playlist URL or playlist_id only",
    parse_identity=_playlist_identity_from_value,
    collection_url=_playlist_url,
    list_with_ytdlp=list_youtube_playlist_with_ytdlp,
    title_field="playlist_title",
    poll_identity_from_listing=False,
)
YOUTUBE_CHANNEL = YouTubeCollectionKind(
    kind=YOUTUBE_CHANNEL_KIND,
    noun="channel",
    value_keys=("channel_id", "channel_handle", "channel_url", "url"),
    conflicting_keys=("playlist_id", "playlist_url"),
    conflict_error="youtube_channel supports channel URL, channel_id, or @handle only",
    parse_identity=_channel_identity_from_value,
    collection_url=_channel_url,
    list_with_ytdlp=list_youtube_channel_with_ytdlp,
    title_field="channel_title",
    poll_identity_from_listing=True,
)
_COLLECTION_KINDS = {spec.kind: spec for spec in (YOUTUBE_PLAYLIST, YOUTUBE_CHANNEL)}
# The collection enumerators the one-shot ``derive.collection_expand`` shares
# with the recurring pollers (ExpansionHint.enumerator names one of these).
YOUTUBE_COLLECTION_ENUMERATORS = frozenset(_COLLECTION_KINDS)

for _spec in _COLLECTION_KINDS.values():
    register_source_poller(YouTubeCollectionPoller(_spec))
