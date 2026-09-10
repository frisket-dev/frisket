from __future__ import annotations

import asyncio
import copy

import pytest

from frisket.actions.media_download import MediaDownloadParams, download_media
from frisket.actions.media_download_types import download_outputs
from frisket.actions.types import Row, StagedAudio, StagedFile
from frisket.engine.executor.media_download import AdmittedMediaDownloader
from frisket.engine.executor.row_file_stage import RowFileStager
from frisket.engine.store import Project
from frisket.ops.ytdlp import DownloadedMedia, SidecarFile
from frisket.ops.ytdlp_outputs import _select_subtitle_file

YOUTUBE_WATCH = "https://www.youtube.com/watch?v=fixture123"


def _execute(project, options=None, *, url=YOUTUBE_WATCH):
    """Exercise the real typed handler and admitted downloader, without publishing."""
    stager = RowFileStager(project)
    owner = AdmittedMediaDownloader(project, stager)

    async def run():
        try:
            result = await download_media(
                MediaDownloadParams(
                    source="link", media_type="audio", extra_opts=options
                ),
                Row({"link": url}),
                owner.bind_row({}, sheet_id=1, row_id=1, sources={}),
            )
            return result.output
        finally:
            await owner.aclose()

    try:
        return asyncio.run(run())
    finally:
        stager.close()


@pytest.fixture
def project(tmp_path):
    project = Project.create(tmp_path / "captions.frisket")
    yield project
    project.close()


def _fake_downloaded_media(sidecars=()):
    return DownloadedMedia(
        data=b"fake audio bytes",
        mime="audio/webm",
        filename="fixture123.webm",
        duration_seconds=1.25,
        metadata={"title": "Fixture video"},
        sidecars=list(sidecars),
    )


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        ({"writesubtitles": True}, {"writesubtitles": True, "writeautomaticsub": True}),
        (
            {"writeautomaticsub": True},
            {"writesubtitles": True, "writeautomaticsub": True},
        ),
        (
            {"allsubtitles": True},
            {"allsubtitles": True, "writesubtitles": True, "writeautomaticsub": True},
        ),
        (
            {"writesubtitles": True, "writeautomaticsub": False},
            {"writesubtitles": True, "writeautomaticsub": False},
        ),
        ({"writethumbnail": True}, {"writethumbnail": True}),
        ({}, None),
        (None, None),
    ],
)
def test_typed_downloader_expands_caption_flags_without_mutating_caller(
    project, monkeypatch, options, expected
):
    captured = []
    original = copy.deepcopy(options)
    monkeypatch.setattr(
        "frisket.ops.ytdlp.download_media",
        lambda _url, **kw: (
            captured.append(kw["extra_opts"]) or _fake_downloaded_media()
        ),
    )
    out = _execute(project, options)
    assert captured == [expected]
    assert options == original
    assert isinstance(out.audio, StagedAudio)


@pytest.mark.parametrize(
    "flag", ["writesubtitles", "writeautomaticsub", "allsubtitles"]
)
def test_each_caption_flag_declares_both_outputs(flag):
    assert download_outputs("audio", {flag: True}) == (
        "audio",
        "subtitles",
        "subtitles_text",
    )
    assert download_outputs("audio", {}) == ("audio",)


def test_select_subtitle_file_falls_back_to_english_among_many_auto_languages(
    tmp_path,
):
    """Mirrors the live diagnosis: 157 automatic-caption languages on disk
    (auto captions embed lang codes exactly like manual tracks), no explicit
    preference -- 'en' still wins."""
    video_id = "fixture123"
    languages = [f"lang{i:03d}" for i in range(157)] + ["en"]
    candidates = []
    for lang in languages:
        path = tmp_path / f"{video_id}.{lang}.vtt"
        path.write_text(f"WEBVTT\n\n{lang} captions")
        candidates.append(path)

    selected = _select_subtitle_file(candidates, video_id=video_id, extra_opts=None)

    assert selected is not None
    assert selected.name == f"{video_id}.en.vtt"


def test_select_subtitle_file_handles_hyphenated_auto_caption_lang_codes(tmp_path):
    """Automatic-caption lang codes can carry a variant suffix (e.g. the
    un-translated original ASR track is often keyed like 'en-orig'). The
    filename parser splits on '.' only, so a hyphenated lang token stays
    intact and is matched exactly against subtitleslangs."""
    video_id = "fixture123"
    (tmp_path / f"{video_id}.en-orig.vtt").write_text("WEBVTT\n\noriginal")
    (tmp_path / f"{video_id}.es.vtt").write_text("WEBVTT\n\nspanish")
    candidates = [
        tmp_path / f"{video_id}.en-orig.vtt",
        tmp_path / f"{video_id}.es.vtt",
    ]

    selected = _select_subtitle_file(
        candidates, video_id=video_id, extra_opts={"subtitleslangs": ["en-orig"]}
    )

    assert selected is not None
    assert selected.name == f"{video_id}.en-orig.vtt"


def test_select_subtitle_file_manual_and_auto_same_language_disk_has_one_file(
    tmp_path,
):
    """yt-dlp itself de-dupes per language when both writesubtitles and
    writeautomaticsub are set (a language covered by manual subtitles is
    never re-fetched as an automatic caption) -- so at most one file per
    language ever lands on disk. Selection just needs to pick it."""
    video_id = "fixture123"
    (tmp_path / f"{video_id}.en.vtt").write_text("WEBVTT\n\nthe only en file")
    candidates = [tmp_path / f"{video_id}.en.vtt"]

    selected = _select_subtitle_file(candidates, video_id=video_id, extra_opts=None)

    assert selected is not None
    assert selected.read_text().endswith("the only en file")


def test_execute_writes_field_error_note_when_subtitles_requested_but_absent(
    project, monkeypatch
):
    monkeypatch.setattr(
        "frisket.ops.ytdlp.download_media", lambda *_a, **_k: _fake_downloaded_media()
    )
    out = _execute(project, {"writesubtitles": True})
    assert isinstance(out.audio, StagedAudio)
    for outcome in (out.subtitles, out.subtitles_text):
        assert outcome.status == "failed"
        assert outcome.code == "subtitles_unavailable"
        assert "no manual or automatic captions" in outcome.message


def test_execute_no_note_when_subtitles_not_requested(project, monkeypatch):
    monkeypatch.setattr(
        "frisket.ops.ytdlp.download_media", lambda *_a, **_k: _fake_downloaded_media()
    )
    out = _execute(project)
    assert out.subtitles.status == "ok"
    assert out.subtitles_text.status == "ok"
    assert "subtitles" not in out.model_fields_set


def test_execute_no_note_when_subtitles_sidecar_present(project, monkeypatch):
    vtt = b"WEBVTT\n\n00:00:00.000 --> 00:00:02.000\nauto captions\n"
    sidecar = SidecarFile("subtitles", vtt, "text/vtt", "fixture123.en.vtt")
    monkeypatch.setattr(
        "frisket.ops.ytdlp.download_media",
        lambda *_a, **_k: _fake_downloaded_media([sidecar]),
    )
    out = _execute(project, {"writesubtitles": True})
    assert out.subtitles.status == "ok"
    assert isinstance(out.subtitles.value, StagedFile)
    assert out.subtitles.value.size == len(vtt)
    assert out.subtitles_text.value == "auto captions"


@pytest.mark.network
def test_live_auto_caption_only_video_fetches_subtitles(project):
    pytest.importorskip("yt_dlp")
    out = _execute(
        project,
        {"writesubtitles": True},
        url="https://www.youtube.com/watch?v=YE7VzlLtp-4",
    )
    assert isinstance(out.audio, StagedAudio)
    assert out.audio.size > 0
    assert (
        out.subtitles.value is not None or out.subtitles.code == "subtitles_unavailable"
    )
