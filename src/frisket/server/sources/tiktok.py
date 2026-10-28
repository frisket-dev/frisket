"""TikTok profile feeds: metadata only; the source host owns scheduling and dedupe."""

from __future__ import annotations

import importlib
import re
from collections.abc import Callable
from itertools import islice
from typing import Any
from urllib.parse import urlsplit

from .media_metadata import (
    entry_published_at,
    redact_provider_message,
    string_value,
    thumbnail_url,
)
from .progress import listing_progress_options
from .runtime import (
    SourcePollContext,
    SourcePollItem,
    SourcePollResult,
    register_source_poller,
)

TIKTOK_PROFILE_KIND = "tiktok_profile"
_HANDLE = re.compile(r"@[A-Za-z0-9._]{1,24}")


def _profile_config(source: dict[str, Any]) -> tuple[str, int | None]:
    config = source.get("config") or {}
    if not isinstance(config, dict):
        raise ValueError("TikTok profile config must be an object")
    reference = str(config.get("profile_url") or source.get("url") or "").strip()
    if _HANDLE.fullmatch(reference):
        handle = reference
    else:
        parsed = urlsplit(reference)
        handle = parsed.path.rstrip("/")
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname not in {"tiktok.com", "www.tiktok.com", "m.tiktok.com"}
            or parsed.username
            or parsed.password
            or parsed.port
            or not handle.startswith("/")
            or not _HANDLE.fullmatch(handle[1:])
        ):
            raise ValueError(
                "Use a TikTok profile URL (https://www.tiktok.com/@name) or @handle"
            )
        handle = handle[1:]
    cap = config.get("max_items_per_poll")
    if cap is not None and (type(cap) is not int or cap <= 0):
        raise ValueError("TikTok max items must be a positive whole number")
    return f"https://www.tiktok.com/{handle}", cap


def list_tiktok_profile(
    profile_url: str,
    *,
    item_cap: int | None,
    on_progress: Callable[[int, int | None], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Flat profile entries contain post metadata without fetching video bytes."""
    yt_dlp = importlib.import_module("yt_dlp")
    options = {
        "extract_flat": True,
        "skip_download": True,
        "ignoreerrors": False,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "socket_timeout": 30,
    }
    options.update(listing_progress_options(on_progress, should_cancel, item_cap))
    if item_cap is not None:
        options["playlistend"] = item_cap
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(profile_url, download=False)
        if not isinstance(info, dict) or info.get("entries") is None:
            raise ValueError("TikTok did not return a profile listing; try again later")
        return {**info, "entries": list(islice(info["entries"], item_cap))}


class TikTokProfilePoller:
    kind = TIKTOK_PROFILE_KIND

    def __init__(self, provider: Callable[..., dict[str, Any]] | None = None) -> None:
        self._provider = provider or list_tiktok_profile

    def validate_config(self, source: dict[str, Any]) -> str | None:
        try:
            _profile_config(source)
        except ValueError as exc:
            return str(exc)
        return None

    def poll(self, ctx: SourcePollContext) -> SourcePollResult:
        url, cap = _profile_config(ctx.source)
        try:
            progress = (
                {"on_progress": ctx.on_progress, "should_cancel": ctx.should_cancel}
                if self._provider is list_tiktok_profile
                else {}
            )
            listing = self._provider(url, item_cap=cap, **progress)
            if not isinstance(listing, dict) or listing.get("entries") is None:
                raise ValueError("TikTok did not return a profile listing")
            entries = list(islice(listing["entries"], cap))
        except Exception as exc:
            raise RuntimeError(
                f"TikTok profile listing failed: {redact_provider_message(exc)}"
            ) from exc
        items = []
        warnings = []
        handle = url.rsplit("/", 1)[-1]
        channel_title = (
            string_value(listing.get("uploader"))
            or string_value(listing.get("title"))
            or handle
        )
        for entry in entries:
            if not isinstance(entry, dict) or not str(entry.get("id") or "").isdigit():
                warnings.append("TikTok skipped an unavailable post")
                continue
            video_id = str(entry["id"])
            watch_url = f"{url}/video/{video_id}"
            published_at = entry_published_at(entry)
            row = {
                "video_id": video_id,
                "title": entry.get("title")
                or entry.get("description")
                or f"TikTok video {video_id}",
                "url": watch_url,
                "channel_title": entry.get("channel")
                or entry.get("uploader")
                or channel_title,
                "published_at": published_at,
                "description": entry.get("description"),
                "duration": entry.get("duration"),
                "thumbnail_url": thumbnail_url(entry),
            }
            item_id = f"tiktok:video:{video_id}"
            items.append(
                SourcePollItem(
                    dedupe_key=item_id,
                    source_item_id=item_id,
                    row=row,
                    title=str(row["title"]),
                    url=watch_url,
                    published_at=published_at,
                    # Retain useful metadata, not signed media URLs or entire format inventories.
                    raw=row,
                )
            )
        limited = cap is not None and len(entries) >= cap
        if limited:
            warnings.append(
                f"TikTok listing reached the {cap}-item limit; older posts may be omitted"
            )
        return SourcePollResult(
            items=items,
            warnings=warnings,
            summary={
                "channel_title": channel_title,
                "items_materialized": len(items),
                "truncated": limited,
            },
            provider_use=[
                {
                    "provider": "tiktok",
                    "service": "yt-dlp",
                    "source_kind": self.kind,
                    "download": False,
                    "external_api": True,
                    "cost_actual": 0.0,
                    "items_materialized": len(items),
                }
            ],
            cost={"cost_micro": 0},
        )


register_source_poller(TikTokProfilePoller())
