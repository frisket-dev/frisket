from __future__ import annotations

from frisket.engine.store.media_download_candidates import (
    compute_media_download_candidates,
)


def _col(id_: int, name: str, type_: str) -> dict:
    return {"id": id_, "name": name, "type": type_}


def test_youtube_watch_urls_classify_as_download_media() -> None:
    columns = [_col(1, "source_url", "link")]
    data = {
        1: {"1": "https://youtube.com/watch?v=abc123"},
        2: {"1": "https://youtube.com/watch?v=def456"},
    }
    result = compute_media_download_candidates(columns, data, [1, 2])
    assert result == {1: "media.ytdlp_download"}


def test_vimeo_url_classifies_as_download_media() -> None:
    columns = [_col(1, "source_url", "link")]
    data = {1: {"1": "https://vimeo.com/76979871"}}
    result = compute_media_download_candidates(columns, data, [1])
    assert result == {1: "media.ytdlp_download"}


def test_tiktok_url_classifies_as_download_media() -> None:
    columns = [_col(1, "source_url", "link")]
    data = {1: {"1": "https://www.tiktok.com/@creator/video/7123456789"}}
    result = compute_media_download_candidates(columns, data, [1])
    assert result == {1: "media.ytdlp_download"}


def test_ordinary_page_urls_are_not_candidates() -> None:
    columns = [_col(1, "source_url", "link")]
    data = {1: {"1": "https://example.com/an-article"}}
    result = compute_media_download_candidates(columns, data, [1])
    assert result == {}


def test_gated_when_sheet_already_has_a_media_column() -> None:
    # Once a download step has produced a real media column, this channel
    # goes quiet — the transcribe hint (SS1/SS3.1) takes over from there.
    columns = [_col(1, "source_url", "link"), _col(2, "clip", "audio")]
    data = {1: {"1": "https://youtube.com/watch?v=abc123", "2": None}}
    result = compute_media_download_candidates(columns, data, [1])
    assert result == {}


def test_non_link_text_columns_are_never_candidates() -> None:
    columns = [_col(1, "note", "number")]
    data = {1: {"1": "https://youtube.com/watch?v=abc123"}}
    result = compute_media_download_candidates(columns, data, [1])
    assert result == {}


def test_empty_and_non_string_values_are_skipped() -> None:
    columns = [_col(1, "source_url", "link")]
    data = {
        1: {"1": None},
        2: {"1": "   "},
        3: {"1": "https://youtube.com/watch?v=abc123"},
    }
    result = compute_media_download_candidates(columns, data, [1, 2, 3])
    assert result == {1: "media.ytdlp_download"}
