from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from frisket.engine.executor import actions as executor_actions
from frisket.server.sources.youtube import (
    YouTubePlaylistListing,
    collection_display_name,
    enumerate_youtube_collection,
    list_youtube_playlist_with_ytdlp,
)
from frisket.engine.store import Project

PROJECT_ID = "project-youtube-playlist-title"
PLAYLIST_ID = "PLinvestigative123"
PLAYLIST_URL = f"https://www.youtube.com/playlist?list={PLAYLIST_ID}"


def _entries(count: int) -> list[dict[str, Any]]:
    return [
        {
            "id": f"vid{index:07d}",
            "title": f"Video {index}",
            "url": f"https://www.youtube.com/watch?v=vid{index:07d}",
        }
        for index in range(count)
    ]


# ---------------------------------------------------------------------------
# Naming logic (offline, pure function)
# ---------------------------------------------------------------------------


def test_collection_display_name_uses_playlist_title_when_present():
    name = collection_display_name(
        "youtube_playlist",
        {"playlist_title": "Weekly Accountability Roundup", "playlist_id": PLAYLIST_ID},
        PLAYLIST_URL,
    )

    assert name == "Weekly Accountability Roundup"


def test_collection_display_name_falls_back_to_url_form_when_title_absent():
    name = collection_display_name(
        "youtube_playlist",
        {"playlist_title": None, "playlist_id": PLAYLIST_ID},
        PLAYLIST_URL,
    )

    assert name == f"YouTube playlist: {PLAYLIST_URL}"


def test_collection_display_name_falls_back_when_title_is_blank_or_whitespace():
    name = collection_display_name(
        "youtube_playlist",
        {"playlist_title": "   ", "playlist_id": PLAYLIST_ID},
        PLAYLIST_URL,
    )

    assert name == f"YouTube playlist: {PLAYLIST_URL}"


def test_collection_display_name_uses_channel_title_for_channel_kind():
    name = collection_display_name(
        "youtube_channel",
        {"channel_title": "Investigative", "channel_id": "UC123"},
        "https://www.youtube.com/@investigative",
    )

    assert name == "Investigative"


def test_collection_display_name_channel_fallback_labels_correctly():
    name = collection_display_name(
        "youtube_channel",
        {"channel_title": None, "channel_id": "UC123"},
        "https://www.youtube.com/@investigative",
    )

    assert name == "YouTube channel: https://www.youtube.com/@investigative"


# ---------------------------------------------------------------------------
# Enumerator: yt-dlp extract_info -> playlist_title (offline fixture)
# ---------------------------------------------------------------------------


class _FakeYoutubeDL:
    def __init__(self, opts: dict[str, Any], *, info: dict[str, Any]) -> None:
        self.opts = opts
        self._info = info

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):  # noqa: ANN001
        return False

    def extract_info(self, url, *, download):  # noqa: ANN001, ARG002
        return self._info


def test_list_youtube_playlist_with_ytdlp_extracts_playlist_title(monkeypatch):
    import sys
    import types

    info = {
        "id": PLAYLIST_ID,
        "title": "Weekly Accountability Roundup",
        "extractor_key": "YoutubeTab",
        "entries": [None, *_entries(3), "unparseable entry"],
    }
    fake_module = types.SimpleNamespace(
        YoutubeDL=lambda opts: _FakeYoutubeDL(opts, info=info),
    )
    monkeypatch.setitem(sys.modules, "yt_dlp", fake_module)

    listing = list_youtube_playlist_with_ytdlp(
        PLAYLIST_URL, playlist_id=PLAYLIST_ID, max_pages=5, item_cap=100
    )

    assert listing.playlist_title == "Weekly Accountability Roundup"
    assert len(listing.entries) == 3


def test_list_youtube_playlist_with_ytdlp_title_absent_is_none(monkeypatch):
    import sys
    import types

    info = {
        "id": PLAYLIST_ID,
        "extractor_key": "YoutubeTab",
        "entries": _entries(2),
    }
    fake_module = types.SimpleNamespace(
        YoutubeDL=lambda opts: _FakeYoutubeDL(opts, info=info),
    )
    monkeypatch.setitem(sys.modules, "yt_dlp", fake_module)

    listing = list_youtube_playlist_with_ytdlp(
        PLAYLIST_URL, playlist_id=PLAYLIST_ID, max_pages=5, item_cap=100
    )

    assert listing.playlist_title is None


# ---------------------------------------------------------------------------
# enumerate_youtube_collection: identity carries playlist_title
# ---------------------------------------------------------------------------


def _fake_playlist_provider(*, title: str | None, entry_count: int):
    def provider(
        playlist_url: str,
        *,
        playlist_id: str,
        max_pages: int,
        item_cap: int,
    ) -> YouTubePlaylistListing:
        return YouTubePlaylistListing(
            playlist_id=playlist_id,
            entries=_entries(entry_count)[:item_cap],
            playlist_title=title,
            provider_facts={"service": "yt-dlp", "playlist_count": entry_count},
        )

    return provider


def test_enumerate_youtube_collection_identity_carries_playlist_title():
    enumeration = enumerate_youtube_collection(
        "youtube_playlist",
        PLAYLIST_URL,
        item_cap=10,
        provider=_fake_playlist_provider(
            title="Weekly Accountability Roundup", entry_count=3
        ),
    )

    assert enumeration.identity["playlist_title"] == "Weekly Accountability Roundup"
    assert enumeration.identity["playlist_id"] == PLAYLIST_ID


def test_enumerate_youtube_collection_identity_title_absent_is_none():
    enumeration = enumerate_youtube_collection(
        "youtube_playlist",
        PLAYLIST_URL,
        item_cap=10,
        provider=_fake_playlist_provider(title=None, entry_count=2),
    )

    assert enumeration.identity["playlist_title"] is None


# ---------------------------------------------------------------------------
# derive.collection_expand: the naming decision surfaces end to end
# ---------------------------------------------------------------------------


def _seed_source_cell(project_path: Path, url: str = PLAYLIST_URL) -> dict[str, int]:
    project = Project.create(project_path, name="Playlists")
    try:
        sheet_id = project.add_sheet("Playlists")
        col_id = project.add_column(sheet_id, "playlist_url", type="link")
        row_ids = project.add_rows(
            sheet_id, [{"playlist_url": url}], {"playlist_url": col_id}
        )
        return {
            "sheet_id": sheet_id,
            "column_id": col_id,
            "row_id": int(row_ids[0]) if isinstance(row_ids, list) else int(row_ids),
        }
    finally:
        project.close()


def _expand_action(
    *,
    source_sheet_id: int,
    source_column_id: int,
    source_row_id: int,
    target_sheet_name: str,
    idempotency_key: str,
) -> dict[str, Any]:
    return {
        "action_id": "derive.collection_expand",
        "scope": {"kind": "project"},
        "sheet_name": target_sheet_name,
        "params": {
            "source_sheet_id": source_sheet_id,
            "source_column_id": source_column_id,
            "source_row_id": source_row_id,
        },
        "idempotency_key": idempotency_key,
    }


@pytest.fixture
def inject_playlist_provider(monkeypatch: pytest.MonkeyPatch):
    from frisket.engine.executor import collection_read as family

    def _inject(*, title: str | None, entry_count: int) -> None:
        monkeypatch.setattr(
            family,
            "_ENUMERATOR_PROVIDER",
            _fake_playlist_provider(title=title, entry_count=entry_count),
            raising=False,
        )

    return _inject


def test_derive_collection_expand_receipt_carries_suggested_target_name(
    tmp_path: Path, inject_playlist_provider
) -> None:
    inject_playlist_provider(title="Weekly Accountability Roundup", entry_count=2)
    seed = _seed_source_cell(tmp_path / "p.frisket")
    project = Project(tmp_path / "p.frisket")
    try:
        result = executor_actions.run_action_spec(
            project,
            _expand_action(
                source_sheet_id=seed["sheet_id"],
                source_column_id=seed["column_id"],
                source_row_id=seed["row_id"],
                # The caller still names the sheet explicitly (target_sheet_name
                # stays required app-wide) -- this pins that the SUGGESTION the
                # backend now computes and surfaces is independent of whatever
                # name the caller actually chose.
                target_sheet_name="My Custom Name",
                idempotency_key="collection_expand@sha256:title-evidence",
            ),
            project_id=PROJECT_ID,
        )
        assert result.status == "completed", result.errors

        from frisket.engine.store.receipts import ReceiptStore

        receipt = ReceiptStore(project).parsed_by_id(result.receipt_id)
        classification_refs = [
            item.ref
            for item in (*receipt.inputs, *receipt.evidence)
            if item.ref["kind"] == "collection_expand_classification"
        ]
        assert len(classification_refs) == 1
        assert (
            classification_refs[0]["suggested_target_name"]
            == "Weekly Accountability Roundup"
        )
        assert (
            classification_refs[0]["identity"]["playlist_title"]
            == "Weekly Accountability Roundup"
        )
    finally:
        project.close()


def test_derive_collection_expand_confirmation_gate_carries_suggested_target_name(
    tmp_path: Path, inject_playlist_provider
) -> None:
    """The default-cap 402 gate fires before a name is ever written -- the
    suggested name must still be visible in the gate's details so a caller
    can prefill the confirm-resubmit form correctly."""
    inject_playlist_provider(title=None, entry_count=250)
    seed = _seed_source_cell(tmp_path / "p.frisket")
    project = Project(tmp_path / "p.frisket")
    try:
        result = executor_actions.run_action_spec(
            project,
            _expand_action(
                source_sheet_id=seed["sheet_id"],
                source_column_id=seed["column_id"],
                source_row_id=seed["row_id"],
                target_sheet_name="Placeholder",
                idempotency_key="collection_expand@sha256:title-gate",
            ),
            project_id=PROJECT_ID,
        )
        assert result.status == "needs_confirmation"
        assert result.errors[0].code == "collection_expand_requires_confirmation"
        assert result.errors[0].details["preview_count"] == 250
        assert len(str(result.errors[0].details["promise_set_hash"])) == 64
        assert (
            result.errors[0].details["suggested_target_name"]
            == f"YouTube playlist: {PLAYLIST_URL}"
        )
    finally:
        project.close()


# ---------------------------------------------------------------------------
# Live (network) test -- skipped by default, run with -m network.
# ---------------------------------------------------------------------------


@pytest.mark.network
def test_live_playlist_title_extracted_from_real_yt_dlp():
    """A real, long-lived public YouTube playlist has a non-empty title that
    is not the raw playlist URL."""
    pytest.importorskip("yt_dlp")

    # Google Developers' well-known, long-lived public playlist.
    url = "https://www.youtube.com/playlist?list=PLOU2XLYxmsIKpaV8h0AGE05so0fAwwfTw"

    listing = list_youtube_playlist_with_ytdlp(
        url, playlist_id="PLOU2XLYxmsIKpaV8h0AGE05so0fAwwfTw", max_pages=1, item_cap=5
    )

    assert listing.playlist_title
    assert "youtube.com" not in listing.playlist_title
