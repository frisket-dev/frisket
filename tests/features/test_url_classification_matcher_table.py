"""url-classification-registry-core-v1: golden matcher-table regression net.

Deterministic and offline. Asserts the first-party
matcher table maps a URL to (kind, provider, matcher_id) exactly, covering the
routing distinctions the source ask leads with — youtube video vs channel vs
playlist, tiktok video vs profile, vimeo media, the page fallback, plus the
parsed-parts security property and URL normalization (scheme/case/
credentials/port/tracking-params).
"""

from __future__ import annotations

import pytest

from frisket.features.url_classification import classify_url

# (url) -> (kind, provider, matcher_id)
GOLDEN: list[tuple[str, tuple[str, str, str]]] = [
    # --- youtube video (watch / shorts / youtu.be) -> media ---
    (
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        ("media", "youtube", "firstparty.youtube.video.watch"),
    ),
    (
        "https://youtube.com/watch?v=abc123",
        ("media", "youtube", "firstparty.youtube.video.watch"),
    ),
    # m./music./www. host variants collapse to one registered domain
    (
        "https://m.youtube.com/watch?v=abc123",
        ("media", "youtube", "firstparty.youtube.video.watch"),
    ),
    (
        "https://music.youtube.com/watch?v=abc123",
        ("media", "youtube", "firstparty.youtube.video.watch"),
    ),
    (
        "https://www.youtube.com/shorts/abc123def",
        ("media", "youtube", "firstparty.youtube.video.shorts"),
    ),
    (
        "https://youtu.be/dQw4w9WgXcQ",
        ("media", "youtube", "firstparty.youtube.video.short"),
    ),
    # a watch URL that ALSO carries a playlist context is still the video
    (
        "https://www.youtube.com/watch?v=abc123&list=PLxyz",
        ("media", "youtube", "firstparty.youtube.video.watch"),
    ),
    # --- youtube collection (channel @handle / /channel/ / /c/ / /user/) ---
    (
        "https://www.youtube.com/@veritasium",
        ("collection", "youtube", "firstparty.youtube.channel"),
    ),
    (
        "https://www.youtube.com/channel/UCHnyfMqiRRG1u-2MsSQLbXA",
        ("collection", "youtube", "firstparty.youtube.channel"),
    ),
    (
        "https://www.youtube.com/c/Veritasium",
        ("collection", "youtube", "firstparty.youtube.channel"),
    ),
    (
        "https://www.youtube.com/user/1veritasium",
        ("collection", "youtube", "firstparty.youtube.channel"),
    ),
    # --- youtube playlist -> collection ---
    (
        "https://www.youtube.com/playlist?list=PLZHQObOWTQDPD3MizzM2xVFitgF8hE_ab",
        ("collection", "youtube", "firstparty.youtube.playlist"),
    ),
    # --- tiktok video; unsupported profile collections fall back to page ---
    (
        "https://www.tiktok.com/@scout2015/video/6718335390845095173",
        ("media", "tiktok", "firstparty.tiktok.video"),
    ),
    (
        "https://vm.tiktok.com/ZM123abc/",
        ("media", "tiktok", "firstparty.tiktok.video.short"),
    ),
    (
        "https://www.tiktok.com/@scout2015",
        ("page", "generic", "firstparty.page"),
    ),
    # --- vimeo media ---
    (
        "https://vimeo.com/123456789",
        ("media", "vimeo", "firstparty.vimeo.video"),
    ),
    (
        "https://player.vimeo.com/video/123456789",
        ("media", "vimeo", "firstparty.vimeo.video"),
    ),
    # --- page fallback ---
    (
        "https://www.example.com/some/article",
        ("page", "generic", "firstparty.page"),
    ),
    (
        "https://news.ycombinator.com/item?id=1",
        ("page", "generic", "firstparty.page"),
    ),
    # --- security: youtube.com in the QUERY of another host is NOT youtube ---
    (
        "http://evil.com/?redirect=https://youtube.com/watch?v=abc123",
        ("page", "generic", "firstparty.page"),
    ),
    # --- normalization: case / credentials / port stripped ---
    (
        "https://User:Pass@YouTube.COM:443/watch?v=abc123",
        ("media", "youtube", "firstparty.youtube.video.watch"),
    ),
    # --- normalization: tracking params do not change routing ---
    (
        "https://youtu.be/dQw4w9WgXcQ?si=Xabc123DEF&utm_source=share",
        ("media", "youtube", "firstparty.youtube.video.short"),
    ),
]


@pytest.mark.parametrize("url,expected", GOLDEN, ids=[g[0] for g in GOLDEN])
def test_matcher_table_golden(url: str, expected: tuple[str, str, str]) -> None:
    result = classify_url(url)
    assert result is not None, url
    assert (result.kind, result.provider, result.matcher_id) == expected


def test_non_http_scheme_is_unclassified() -> None:
    assert classify_url("ftp://youtube.com/watch?v=abc123") is None
    assert classify_url("file:///etc/passwd") is None
    assert classify_url("javascript:alert(1)") is None


def test_garbage_input_is_unclassified() -> None:
    assert classify_url("") is None
    assert classify_url("not a url at all") is None
    assert classify_url("   ") is None


def test_media_classifications_declare_download_capability() -> None:
    result = classify_url("https://www.youtube.com/watch?v=abc123")
    assert result is not None
    assert result.kind == "media"
    assert "external:media_download" in result.capabilities
    assert result.handler.action_kind == "media.ytdlp_download"


def test_collection_classification_carries_expansion() -> None:
    result = classify_url("https://www.youtube.com/@veritasium")
    assert result is not None
    assert result.kind == "collection"
    assert result.expansion is not None
    assert result.expansion.enumerator == "youtube_channel"
    assert result.handler.action_kind == "derive.collection_expand"


def test_page_classification_routes_to_fetch_url() -> None:
    result = classify_url("https://www.example.com/article")
    assert result is not None
    assert result.kind == "page"
    assert result.handler.action_kind == "media.fetch_url"
    assert result.expansion is None
