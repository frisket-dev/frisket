from __future__ import annotations

from typing import Any

import pytest

from frisket.server.sources.youtube import (
    YOUTUBE_COLLECTION_ENUMERATORS,
    YouTubeChannelListing,
    YouTubePlaylistListing,
    enumerate_youtube_collection,
    list_youtube_channel_with_ytdlp,
    list_youtube_playlist_with_ytdlp,
)
from frisket.features.url_classification import classify_url

CHANNEL_URL = "https://www.youtube.com/@investigative"
PLAYLIST_URL = "https://www.youtube.com/playlist?list=PLabcd1234"


def _entries(count: int) -> list[dict[str, Any]]:
    return [
        {
            "id": f"vid{index:07d}",
            "title": f"Video {index}",
            "url": f"https://www.youtube.com/watch?v=vid{index:07d}",
            "channel": "@investigative",
            "channel_id": "UCinvestigative123456",
        }
        for index in range(count)
    ]


class _FakeChannelProvider:
    def __init__(self, entries: list[dict[str, Any]], preview_count: int) -> None:
        self._entries = entries
        self._preview = preview_count
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        channel_url: str,
        *,
        channel_id: str | None,
        channel_handle: str | None,
        max_pages: int | None,
        item_cap: int | None,
    ) -> YouTubeChannelListing:
        self.calls.append(
            {
                "channel_url": channel_url,
                "max_pages": max_pages,
                "item_cap": item_cap,
            }
        )
        entries = self._entries
        limits = [limit for limit in (item_cap, max_pages and max_pages * 100) if limit]
        if limits:
            entries = entries[: min(limits)]
        return YouTubeChannelListing(
            entries=entries,
            channel_id=channel_id,
            channel_handle=channel_handle,
            channel_title="Investigative",
            provider_facts={"service": "yt-dlp", "playlist_count": self._preview},
        )


class _FakePlaylistProvider:
    def __init__(self, entries: list[dict[str, Any]], preview_count: int) -> None:
        self._entries = entries
        self._preview = preview_count

    def __call__(
        self,
        playlist_url: str,
        *,
        playlist_id: str,
        max_pages: int | None,
        item_cap: int | None,
    ) -> YouTubePlaylistListing:
        entries = self._entries
        limits = [limit for limit in (item_cap, max_pages and max_pages * 100) if limit]
        if limits:
            entries = entries[: min(limits)]
        return YouTubePlaylistListing(
            playlist_id=playlist_id,
            entries=entries,
            provider_facts={"service": "yt-dlp", "playlist_count": self._preview},
        )


# ---------------------------------------------------------------------------
# Classification → collection routing (what the executor keys on)
# ---------------------------------------------------------------------------


def test_channel_url_classifies_as_collection_with_channel_enumerator() -> None:
    classification = classify_url(CHANNEL_URL)
    assert classification is not None
    assert classification.kind == "collection"
    assert classification.provider == "youtube"
    assert classification.handler.action_kind == "derive.collection_expand"
    assert classification.expansion is not None
    assert classification.expansion.enumerator == "youtube_channel"
    assert classification.expansion.default_cap == 100
    assert classification.expansion.hard_cap is None


def test_playlist_url_classifies_as_collection_with_playlist_enumerator() -> None:
    classification = classify_url(PLAYLIST_URL)
    assert classification is not None
    assert classification.kind == "collection"
    assert classification.expansion is not None
    assert classification.expansion.enumerator == "youtube_playlist"
    assert classification.expansion.hard_cap is None


def test_watch_url_is_not_a_collection() -> None:
    classification = classify_url("https://www.youtube.com/watch?v=vid0000001")
    assert classification is not None
    assert classification.kind == "media"


# ---------------------------------------------------------------------------
# The shared enumerator: same providers/shaper as the recurring pollers
# ---------------------------------------------------------------------------


def test_youtube_collection_enumerators_are_the_poller_kinds() -> None:
    assert YOUTUBE_COLLECTION_ENUMERATORS == {"youtube_channel", "youtube_playlist"}


def test_enumerate_channel_shares_the_poller_provider_seam() -> None:
    provider = _FakeChannelProvider(_entries(3), preview_count=3)
    result = enumerate_youtube_collection(
        "youtube_channel", CHANNEL_URL, item_cap=1000, provider=provider
    )
    assert provider.calls, "the injected provider must be consulted (no network)"
    assert result.preview_count == 3
    assert len(result.rows) == 3
    # Rows carry the poller's shaped fields (video_id + watch url + title).
    first = result.rows[0]
    assert first["video_id"] == "vid0000000"
    assert first["url"] == "https://www.youtube.com/watch?v=vid0000000"
    assert first["title"] == "Video 0"


def test_enumerate_channel_honors_item_cap_above_legacy_provider_clamp() -> None:
    provider = _FakeChannelProvider(_entries(501), preview_count=501)

    result = enumerate_youtube_collection(
        "youtube_channel", CHANNEL_URL, item_cap=501, provider=provider
    )

    assert provider.calls[0]["item_cap"] == 501
    max_pages = provider.calls[0]["max_pages"]
    assert max_pages is None or int(max_pages) >= 6
    assert result.preview_count == 501
    assert len(result.rows) == 501
    assert result.truncated is False


def test_enumerate_playlist_shares_the_poller_provider_seam() -> None:
    provider = _FakePlaylistProvider(_entries(5), preview_count=5)
    result = enumerate_youtube_collection(
        "youtube_playlist", PLAYLIST_URL, item_cap=1000, provider=provider
    )
    assert result.preview_count == 5
    assert len(result.rows) == 5


def test_enumerate_preview_count_is_total_even_when_entries_truncated() -> None:
    # extract_flat reports the true total (playlist_count) while the entry list
    # is bounded — the preview count the cost-gate reads is the total.
    provider = _FakeChannelProvider(_entries(4), preview_count=500)
    result = enumerate_youtube_collection(
        "youtube_channel", CHANNEL_URL, item_cap=1000, provider=provider
    )
    assert result.preview_count == 500
    assert len(result.rows) == 4
    assert result.truncated is True


def test_enumerate_defaults_to_the_real_poller_providers() -> None:
    # No provider injected -> the shared enumerator uses the SAME real yt-dlp
    # provider functions the pollers use (identity check, no call/network).
    from frisket.server.sources import youtube as youtube_module

    assert youtube_module.list_youtube_channel_with_ytdlp is (
        list_youtube_channel_with_ytdlp
    )
    assert youtube_module.list_youtube_playlist_with_ytdlp is (
        list_youtube_playlist_with_ytdlp
    )


def test_enumerate_rejects_unknown_enumerator() -> None:
    with pytest.raises(ValueError):
        enumerate_youtube_collection("tiktok_profile", CHANNEL_URL, item_cap=10)


def test_enumerate_rejects_non_channel_url() -> None:
    with pytest.raises(ValueError):
        enumerate_youtube_collection(
            "youtube_channel",
            "https://example.com/not-a-channel",
            item_cap=10,
            provider=_FakeChannelProvider(_entries(1), preview_count=1),
        )
