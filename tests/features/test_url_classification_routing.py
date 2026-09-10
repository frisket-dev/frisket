from __future__ import annotations

import asyncio

import pytest

from frisket.server.action_catalog_hints import (
    project_action_catalog_launcher_hints,
)
from frisket.features.url_classification import (
    classify_url,
    is_supported_ytdlp_media_url,
)

from frisket.ops.http_urls import is_http_url


def test_ops_media_input_url_accepts_arbitrary_http_url(tmp_path, monkeypatch) -> None:
    from frisket.engine.executor import media_download
    from frisket.engine.executor.row_file_stage import RowFileStager
    from frisket.engine.store import Project
    from frisket.ops.ytdlp import DownloadedMedia

    seen = []

    def spy(url):
        seen.append(url)
        return is_http_url(url)

    monkeypatch.setattr(media_download, "is_http_url", spy)
    monkeypatch.setattr(
        "frisket.ops.ytdlp.download_media",
        lambda url, **kw: DownloadedMedia(b"video", "video/mp4", "clip.mp4"),
    )
    value = "https://media.example.org/watch/anything"
    project = Project.create(tmp_path / "urls.frisket")
    stager = RowFileStager(project)
    owner = media_download.AdmittedMediaDownloader(project, stager)

    async def run():
        try:
            output = await owner.bind_row(
                {}, sheet_id=1, row_id=1, sources={}
            ).download(value)
            assert output.video.size == 5
        finally:
            await owner.aclose()

    try:
        asyncio.run(run())
    finally:
        stager.close()
        project.close()
    assert seen == [value]


@pytest.mark.parametrize(
    "url",
    [
        "https://example.org/video?utm_source=share&v=1",
        "  HTTP://User:secret@EXAMPLE.org:8080/a%20b  ",
        "http://127.0.0.1:8080/video",
        "http://[::1]/video",
        "https://example.org:",
        "file:///tmp/video.mp4",
        "ftp://example.org/video",
        "https:///missing-host",
        "",
        "not a URL",
        None,
    ],
)
def test_url_routing_and_explicit_admission_share_syntax(url):
    from frisket.features.url_classification.matchers import normalize_url

    assert is_http_url(url) == (normalize_url(url) is not None)


def test_explicit_download_candidate_accepts_any_http_url() -> None:
    assert is_http_url("https://example.org/some/new/video/site")
    assert is_http_url("http://127.0.0.1:8080/video")
    assert is_http_url("https://user:secret@example.org/private/video")
    assert not is_http_url("file:///tmp/video.mp4")
    assert not is_http_url("not a URL")


# --- Registry routing equivalence (what the old sniffers accepted) ----------


def test_youtube_watch_and_short_route_to_single_video_download() -> None:
    for url in (
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://m.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://music.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://youtu.be/dQw4w9WgXcQ",
        "https://youtu.be/dQw4w9WgXcQ?t=10",
        "https://www.youtube.com/live/dQw4w9WgXcQ",
        "https://www.youtube.com/embed/dQw4w9WgXcQ?start=10",
    ):
        c = classify_url(url)
        assert c is not None, url
        assert c.kind == "media"
        assert c.provider == "youtube"
        assert c.handler.action_kind == "media.ytdlp_download"

    assert classify_url("https://youtube.com/watchlater?v=dQw4w9WgXcQ").kind == "page"


def test_plugin_matcher_can_opt_into_automatic_ytdlp_routing() -> None:
    from frisket.features.url_classification import (
        UrlMatcher,
        register_matcher,
        unregister_matcher,
    )

    matcher_id = "plugin.example.media"
    register_matcher(
        UrlMatcher(
            matcher_id=matcher_id,
            provider="example",
            registered_domains=("media.example",),
            kind="media",
            path_prefix=("/watch",),
            handler_action_kind="media.ytdlp_download",
            source="plugin",
            origin_plugin_id="example.media",
        )
    )
    try:
        assert is_supported_ytdlp_media_url(
            "https://media.example/watch/123",
            enabled_plugin_ids={"example.media"},
        )
    finally:
        unregister_matcher(matcher_id)


def test_youtube_channel_routes_collection_never_single_video_download() -> None:
    for url in (
        "https://www.youtube.com/@nasa",
        "https://www.youtube.com/channel/UCabcdefghijklmnopqrstuv",
        "https://www.youtube.com/c/somechannel",
        "https://www.youtube.com/playlist?list=PL123",
    ):
        c = classify_url(url)
        assert c is not None, url
        assert c.kind == "collection", url
        # It must NOT be dragged into the single-video download path.
        assert c.handler.action_kind != "media.ytdlp_download", url


def test_tiktok_and_vimeo_route_media_via_registry_rows() -> None:
    tiktok = classify_url("https://www.tiktok.com/@creator/video/7123456789")
    assert tiktok is not None
    assert tiktok.kind == "media"
    assert tiktok.provider == "tiktok"
    assert tiktok.handler.action_kind == "media.ytdlp_download"

    short = classify_url("https://vm.tiktok.com/ZM123abc/")
    assert short is not None
    assert short.kind == "media"
    assert short.provider == "tiktok"
    assert short.handler.action_kind == "media.ytdlp_download"

    alternate_short = classify_url("https://vt.tiktok.com/ZM123abc/")
    assert alternate_short is not None
    assert alternate_short.kind == "media"
    assert alternate_short.provider == "tiktok"
    assert alternate_short.handler.action_kind == "media.ytdlp_download"

    vimeo = classify_url("https://vimeo.com/123456789")
    assert vimeo is not None
    assert vimeo.kind == "media"
    assert vimeo.provider == "vimeo"
    assert vimeo.handler.action_kind == "media.ytdlp_download"
    assert is_http_url("https://vimeo.com/123456789?share=copy")

    assert classify_url("https://www.tiktok.com/live").kind == "page"


def test_kick_vod_and_clips_route_media_but_live_channels_do_not() -> None:
    for url in (
        "https://kick.com/xqc/videos/5c697a87-afce-4256-b01f-3c8fe71ef5cb",
        "https://kick.com/spreen/clips/clip_01J8RGZRKHXHXXKJEHGRM932A5",
        "https://kick.com/mxddy?clip=clip_01GYXVB5Y8PWAPWCWMSBCFB05X",
    ):
        classification = classify_url(url)
        assert classification is not None
        assert classification.kind == "media"
        assert classification.provider == "kick"

    assert classify_url("https://kick.com/xqc").kind == "page"


def test_page_fallback_reaches_fetch_url() -> None:
    for url in (
        "https://example.org/story",
        "https://cdn.example/audio/episode.mp3",
    ):
        c = classify_url(url)
        assert c is not None, url
        assert c.kind == "page"
        assert c.handler.action_kind == "media.fetch_url"


def test_non_http_and_evil_lookalike_do_not_route_to_youtube() -> None:
    assert classify_url("not a youtube watch url") is None
    assert classify_url("ftp://example.com/file") is None
    # Parsed-parts security property: youtube in a query param is not youtube.
    evil = classify_url("https://evil.com/?x=youtube.com/watch?v=abc123")
    assert evil is not None
    assert evil.provider != "youtube"


# --- Affordance projector emits url_classification in catalog ui_hints -------


def test_action_catalog_ui_hints_carry_url_classification() -> None:
    hints = project_action_catalog_launcher_hints(
        {"configured": False, "available": False, "engines": [], "error": None}
    )
    yt = hints["media.ytdlp_download"]["url_classification"]
    matcher_ids = {m["matcher_id"] for m in yt["recommended_for"]}
    assert "firstparty.youtube.video.watch" in matcher_ids
    # The channel/playlist collection matchers must NOT recommend the
    # single-video download action.
    assert not any(mid.startswith("firstparty.youtube.channel") for mid in matcher_ids)

    collection = hints["derive.collection_expand"]["url_classification"]
    collection_ids = {m["matcher_id"] for m in collection["recommended_for"]}
    assert collection_ids == {
        "firstparty.youtube.channel",
        "firstparty.youtube.playlist",
    }

    page = hints["media.fetch_url"]["url_classification"]
    page_ids = {m["matcher_id"] for m in page["recommended_for"]}
    assert "firstparty.page" in page_ids
