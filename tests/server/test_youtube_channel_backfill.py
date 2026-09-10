"""youtube-channel-backfill-v1: channel columns fill from download metadata
+ playlist-owner fallback (yt-dlp's flat playlist listing returns null
channel/uploader/channel_id/uploader_id PER ENTRY -- confirmed against
yt-dlp's own extractor source, yt_dlp/extractor/youtube/_tab.py's
`_extract_metadata_from_tabs`: it populates channel/channel_id/uploader/
uploader_id ONLY on the top-level playlist `info` dict, from the playlist
page's owner/byline renderer -- never per-entry; only `title` survives on a
flat entry).

Two-part fix, tested independently:

(a) POLL-TIME fallback (sources/youtube.py): the playlist-level owner
    (info.channel_id/channel at playlist scope) fills an entry's
    channel_id/channel_title when the entry's own are absent. An entry that
    DOES carry its own channel data keeps it (the fallback never overrides).
    Tested below.

(b) DOWNLOAD-TIME backfill (ops/youtube.py): media.ytdlp_download's
    full-metadata info (a single-video FULL yt-dlp extraction, not
    extract_flat, always carries channel/uploader) backfills the row's
    channel_id/channel_title cells when they are EMPTY. A non-empty cell is
    never overwritten, however it got its value. Tested below.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from frisket.engine.executor import run_action_spec
import frisket.ops.ytdlp as youtube_ops
from frisket.ai.llm import ModelRouter
from frisket.server.sources.runtime import (
    get_source_poller,
    register_source_poller,
    unregister_source_poller,
)
from frisket.server.sources.youtube import (
    YOUTUBE_PLAYLIST_KIND,
    YouTubePlaylistListing,
    YouTubePlaylistPoller,
)
from frisket.engine.store import Project
from frisket.ops.ytdlp import DownloadedMedia


PROJECT_ID = "project-youtube-channel-backfill"
PLAYLIST_ID = "PLchannelbackfill1"
# Deliberately use an embeddable YouTube URL that the routing matcher does not
# recognize. The completed download's extractor/provider metadata, not the
# suggestion classifier, is authoritative for YouTube-only channel backfill.
WATCH_URL_1 = "https://www.youtube.com/embed/dQw4w9WgXcQ"
WATCH_URL_2 = "https://www.youtube.com/watch?v=oHg5SJYRHA0"


# ---------------------------------------------------------------------------
# (a) poll-time playlist-owner fallback
# ---------------------------------------------------------------------------


class _FakePlaylistProvider:
    def __init__(self, listing: YouTubePlaylistListing) -> None:
        self._listing = listing
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        playlist_url: str,
        *,
        playlist_id: str,
        max_pages: int,
        item_cap: int,
    ) -> YouTubePlaylistListing:
        self.calls.append({"playlist_url": playlist_url, "playlist_id": playlist_id})
        return self._listing


@pytest.fixture
def restore_youtube_playlist_poller():
    previous = get_source_poller(YOUTUBE_PLAYLIST_KIND)
    try:
        yield
    finally:
        if previous is None:
            unregister_source_poller(YOUTUBE_PLAYLIST_KIND)
        else:
            register_source_poller(previous, replace=True)


def _source_poll_action(source: dict[str, Any]) -> dict[str, Any]:
    return {
        "action_id": "source.poll",
        "scope": {"kind": "project"},
        "params": {"source": source},
        "idempotency_key": "source_poll@sha256:channel-backfill",
    }


def _playlist_source() -> dict[str, Any]:
    return {
        "kind": YOUTUBE_PLAYLIST_KIND,
        "name": "Channel Backfill Playlist",
        "url": f"https://www.youtube.com/playlist?list={PLAYLIST_ID}",
        "config": {
            "schema_version": "frisket.source.youtube.v1",
            "max_pages_per_poll": 2,
        },
    }


def _rows(project: Project, sheet_id: int) -> list[dict[str, Any]]:
    import json

    columns = {
        int(column["id"]): column["name"]
        for column in project.columns(sheet_id, include_hidden=True)
    }
    out: list[dict[str, Any]] = []
    for row in project.db.execute(
        "SELECT id FROM rows WHERE sheet_id=? ORDER BY position",
        (sheet_id,),
    ).fetchall():
        values = project.db.execute(
            "SELECT column_id, value FROM cells WHERE row_id=?",
            (row["id"],),
        ).fetchall()
        out.append(
            {
                columns[int(value["column_id"])]: json.loads(value["value"])
                for value in values
            }
        )
    return out


def test_playlist_poll_fills_null_entry_channel_from_playlist_owner(
    tmp_path: Path,
    restore_youtube_playlist_poller: None,
) -> None:
    # extract_flat entries: title survives, channel/uploader/channel_id/
    # uploader_id are ALL absent, exactly as yt-dlp's flat playlist listing
    # returns them.
    flat_entry = {
        "id": "flatvid0001",
        "title": "Flat-listed video",
        "upload_date": "20260620",
    }
    # A heterogeneous entry that DOES carry its own channel data -- the
    # playlist-owner fallback must never override an entry's own value.
    owned_entry = {
        "id": "ownedvid002",
        "title": "Entry with its own channel",
        "upload_date": "20260621",
        "channel_id": "UCentryowned00000001",
        "channel": "Entry's Own Channel",
    }
    listing = YouTubePlaylistListing(
        playlist_id=PLAYLIST_ID,
        entries=[flat_entry, owned_entry],
        provider_facts={"service": "fake-yt-dlp"},
        # The playlist-LEVEL owner -- yt-dlp's top-level info.channel_id/
        # channel/uploader_id/uploader at playlist scope.
        channel_id="UCplaylistowner000001",
        channel_title="Playlist Owner Channel",
    )
    provider = _FakePlaylistProvider(listing)
    register_source_poller(YouTubePlaylistPoller(provider=provider), replace=True)

    project = Project.create(tmp_path / "channel-backfill-poll.frisket", name="YT")
    result = run_action_spec(
        project,
        _source_poll_action(_playlist_source()),
        project_id=PROJECT_ID,
    )
    assert result.status == "completed", result.errors
    sheet_id = next(
        output.sheet_id for output in result.outputs if output.kind == "sheet"
    )
    rows = _rows(project, sheet_id)
    by_video = {row["video_id"]: row for row in rows}

    # (a) RED case: the flat-null entry is filled from the playlist owner.
    flat_row = by_video["flatvid0001"]
    assert flat_row["channel_id"] == "UCplaylistowner000001"
    assert flat_row["channel_title"] == "Playlist Owner Channel"

    # The entry that carried its own channel data keeps it -- the fallback
    # never overrides a present entry-level value.
    owned_row = by_video["ownedvid002"]
    assert owned_row["channel_id"] == "UCentryowned00000001"
    assert owned_row["channel_title"] == "Entry's Own Channel"


# ---------------------------------------------------------------------------
# (b) download-time backfill of empty channel cells
# ---------------------------------------------------------------------------


def _seed_channel_backfill_project(tmp_path: Path) -> tuple[Project, int, list[int]]:
    """A sheet shaped like a YouTube playlist source-poll output (url +
    channel_id + channel_title columns), with row 1's channel cells EMPTY
    (the flat-null case a poll-time fallback couldn't fill -- e.g. a
    heterogeneous-owner playlist) and row 2's channel cells already
    populated (must never be clobbered)."""
    project = Project.create(
        tmp_path / "channel-backfill-download.frisket", name="YT Download"
    )
    sheet_id = project.add_sheet("Videos")
    columns = {
        "url": project.add_column(sheet_id, "url", type="link"),
        "channel_id": project.add_column(sheet_id, "channel_id", type="text"),
        "channel_title": project.add_column(sheet_id, "channel_title", type="text"),
    }
    row_ids = project.add_rows(
        sheet_id,
        [
            {"url": WATCH_URL_1, "channel_id": None, "channel_title": None},
            {
                "url": WATCH_URL_2,
                "channel_id": "UCexistingpreset00001",
                "channel_title": "Existing Preset Channel",
            },
        ],
        columns,
    )
    return project, sheet_id, row_ids


def _youtube_download_action(*, sheet_id: int, row_ids: list[int]) -> dict[str, Any]:
    return {
        "action_id": "media.ytdlp_download",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id, "row_ids": row_ids},
        "output_names": {"audio": "media"},
        "params": {
            "source": "url",
            "media_type": "audio",
        },
        "idempotency_key": "media_ytdlp_download@sha256:channel-backfill",
    }


def test_download_backfills_empty_channel_cells_and_never_clobbers_non_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, sheet_id, row_ids = _seed_channel_backfill_project(tmp_path)
    empty_row_id, preset_row_id = row_ids

    calls: list[str] = []

    def fake_download(url: str, **_kwargs: Any) -> DownloadedMedia:
        calls.append(url)
        return DownloadedMedia(
            data=f"fake bytes {len(calls)}".encode("utf-8"),
            mime="audio/wav",
            filename=f"clip-{len(calls)}.wav",
            duration_seconds=1.0,
            # A single video's FULL yt-dlp extraction -- always carries
            # channel/uploader when the video has one.
            metadata={
                "provider": "youtube",
                "title": f"Downloaded clip {len(calls)}",
                "channel_id": "UCfromdownload000001",
                "channel_title": "Download-Time Channel",
            },
        )

    monkeypatch.setattr(youtube_ops, "download_media", fake_download)

    action = _youtube_download_action(sheet_id=sheet_id, row_ids=row_ids)
    router = ModelRouter()
    challenge = run_action_spec(
        project,
        action,
        project_id=PROJECT_ID,
        router=router,
    )
    assert challenge.status == "needs_confirmation", challenge.errors
    promise_set_hash = challenge.errors[0].details["promise_set_hash"]
    action["confirmation"] = promise_set_hash
    result = run_action_spec(
        project,
        action,
        project_id=PROJECT_ID,
        router=router,
    )
    assert result.status == "completed", result.errors
    assert len(calls) == 2

    columns = {
        str(column["name"]): int(column["id"])
        for column in project.db.execute(
            "SELECT id, name FROM columns WHERE sheet_id=?", (sheet_id,)
        ).fetchall()
    }
    channel_id_values = project.get_values(sheet_id, columns["channel_id"])
    channel_title_values = project.get_values(sheet_id, columns["channel_title"])

    # (b) RED case: the empty row's channel cells are backfilled from the
    # download's full-metadata info.
    assert channel_id_values[empty_row_id] == "UCfromdownload000001"
    assert channel_title_values[empty_row_id] == "Download-Time Channel"

    # Never-clobber: the preset row's non-empty channel cells are untouched,
    # even though the (fake) download metadata carries a different channel.
    assert channel_id_values[preset_row_id] == "UCexistingpreset00001"
    assert channel_title_values[preset_row_id] == "Existing Preset Channel"
