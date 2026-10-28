"""Lazy playlist pages must be consumed while their yt-dlp client is open."""

import sys
from types import SimpleNamespace

import pytest

from frisket.server.sources.youtube import (
    YouTubeProviderError,
    list_youtube_channel_with_ytdlp,
    list_youtube_playlist_with_ytdlp,
)


@pytest.mark.parametrize("kind", ["channel", "playlist"])
@pytest.mark.parametrize("page_fails", [False, True])
def test_lazy_listing_lifecycle_and_error_redaction(monkeypatch, kind, page_fails):
    consumed = []

    class YoutubeDL:
        def __init__(self, options):
            assert options["lazy_playlist"] is True
            self.open = False

        def __enter__(self):
            self.open = True
            return self

        def __exit__(self, *_args):
            self.open = False

        def extract_info(self, _url, *, download):
            assert download is False

            def entries():
                assert self.open, "lazy pages read after closing yt-dlp"
                consumed.append(True)
                if page_fails:
                    raise RuntimeError("provider failure api_key=secret-value")
                yield None
                yield {"id": "video-1", "title": "Video"}

            return {"id": "collection", "entries": entries()}

    monkeypatch.setitem(sys.modules, "yt_dlp", SimpleNamespace(YoutubeDL=YoutubeDL))
    provider = (
        list_youtube_channel_with_ytdlp
        if kind == "channel"
        else list_youtube_playlist_with_ytdlp
    )
    identity = (
        {"channel_id": None, "channel_handle": "@example"}
        if kind == "channel"
        else {"playlist_id": "PLexample"}
    )
    kwargs = {
        **identity,
        "max_pages": None,
        "item_cap": 20,
        "on_progress": lambda *_: None,
    }
    if page_fails:
        with pytest.raises(
            YouTubeProviderError, match=f"{kind} listing failed"
        ) as error:
            provider("https://www.youtube.com/example", **kwargs)
        assert "secret-value" not in str(error.value)
        assert "<redacted>" in str(error.value)
    else:
        result = provider("https://www.youtube.com/example", **kwargs)
        assert result.entries == [{"id": "video-1", "title": "Video"}]
    assert consumed == [True]
