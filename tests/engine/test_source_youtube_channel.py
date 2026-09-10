from __future__ import annotations

import json
import sys
import types
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from frisket.engine.executor import run_action_spec
from frisket.engine.jobs import (
    SqliteJobQueue,
    Worker,
    default_registry,
    enqueue_due_source_polls,
    register_source_poll_handler,
)
from frisket.server.sources.runtime import (
    get_source_poller,
    register_source_poller,
    unregister_source_poller,
)
from frisket.server.sources.youtube import (
    YOUTUBE_CHANNEL_KIND,
    YouTubeChannelListing,
    YouTubeChannelPoller,
    list_youtube_channel_with_ytdlp,
)
from frisket.engine.store import Project
from frisket.engine.store.sources import SourceStore


PROJECT_ID = "project-source-youtube-channel"
CHANNEL_ID = "UCinvestigative123456"
CHANNEL_HANDLE = "@investigative"


class FakeYouTubeChannelProvider:
    def __init__(self, listings: list[YouTubeChannelListing]) -> None:
        self._listings = list(listings)
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
                "channel_id": channel_id,
                "channel_handle": channel_handle,
                "max_pages": max_pages,
                "item_cap": item_cap,
            }
        )
        if not self._listings:
            raise AssertionError("fake YouTube channel provider exhausted")
        listing = self._listings.pop(0)
        limits = [limit for limit in (item_cap, max_pages and max_pages * 100) if limit]
        provider_limit = min(limits) if limits else None
        truncated = provider_limit is not None and len(listing.entries) > provider_limit
        warnings = list(listing.warnings)
        if truncated:
            warnings.append(
                f"youtube_channel item cap reached; limited to {provider_limit} items"
            )
        return replace(
            listing,
            entries=(
                listing.entries
                if provider_limit is None
                else listing.entries[:provider_limit]
            ),
            provider_facts={**listing.provider_facts, "truncated": truncated},
            warnings=warnings,
        )


@pytest.fixture
def restore_youtube_channel_poller() -> Iterator[None]:
    previous = get_source_poller(YOUTUBE_CHANNEL_KIND)
    try:
        yield
    finally:
        if previous is None:
            unregister_source_poller(YOUTUBE_CHANNEL_KIND)
        else:
            register_source_poller(previous, replace=True)


def _source_poll_action(
    *,
    source_id: int | None = None,
    source: dict[str, Any] | None = None,
    idempotency_key: str = "source_youtube_channel@sha256:first",
) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if source_id is not None:
        params["source"] = source_id
    if source is not None:
        params["source"] = source
    return {
        "action_id": "source.poll",
        "scope": {"kind": "project"},
        "params": params,
        "idempotency_key": idempotency_key,
    }


def _youtube_channel_source(
    config: dict[str, Any] | None = None,
    *,
    url: str | None = f"https://www.youtube.com/{CHANNEL_HANDLE}/videos",
) -> dict[str, Any]:
    return {
        "kind": YOUTUBE_CHANNEL_KIND,
        "name": "Investigative Channel",
        "url": url,
        "config": {
            "schema_version": "frisket.source.youtube.v1",
            "max_pages_per_poll": 2,
            **(config or {}),
        },
    }


def _receipt(project: Project, receipt_id: str) -> dict[str, Any]:
    row = project.db.execute(
        "SELECT body FROM receipts WHERE id=?",
        (receipt_id,),
    ).fetchone()
    assert row is not None
    return json.loads(row["body"])


def _rows(project: Project, sheet_id: int) -> list[dict[str, Any]]:
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


def _table_count(project: Project, table: str) -> int:
    return int(project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _listing(
    entries: list[dict[str, Any]],
    *,
    warnings: list[str] | None = None,
    **facts: Any,
) -> YouTubeChannelListing:
    return YouTubeChannelListing(
        entries=entries,
        channel_id=CHANNEL_ID,
        channel_handle=CHANNEL_HANDLE,
        channel_title="Investigative Channel",
        provider_facts={"service": "fake-yt-dlp", **facts},
        warnings=warnings or [],
    )


def _video(
    video_id: str,
    title: str,
    *,
    channel_id: str | None = CHANNEL_ID,
    channel: str | None = "Investigative Channel",
    upload_date: str = "20260622",
    description: str = "Source video description",
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": video_id,
        "title": title,
        "webpage_url": f"https://www.youtube.com/watch?v={video_id}",
        "upload_date": upload_date,
        "description": description,
        "thumbnail": f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg",
    }
    if channel_id is not None:
        row["channel_id"] = channel_id
    if channel is not None:
        row["channel"] = channel
    return row


def test_youtube_channel_solo_poll_materializes_legacy_cap_plus_one(
    tmp_path: Path,
    restore_youtube_channel_poller: None,
) -> None:
    entries = [_video(f"chan{index:07d}", f"Video {index}") for index in range(501)]
    provider = FakeYouTubeChannelProvider(
        [_listing(entries, pages_fetched=6, playlist_count=501)]
    )
    register_source_poller(YouTubeChannelPoller(provider=provider), replace=True)
    source = _youtube_channel_source()
    source["config"].pop("max_pages_per_poll")

    project = Project.create(
        tmp_path / "youtube-channel-unbounded.frisket", name="YouTube"
    )
    try:
        result = run_action_spec(
            project,
            _source_poll_action(
                source=source,
                idempotency_key="source_youtube_channel@sha256:legacy-cap-plus-one",
            ),
            project_id=PROJECT_ID,
        )

        assert result.status == "completed", result.errors
        item_cap = provider.calls[0]["item_cap"]
        assert item_cap is None or int(item_cap) >= 501
        max_pages = provider.calls[0]["max_pages"]
        assert max_pages is None or int(max_pages) >= 6
        sheet_id = next(
            output.sheet_id for output in result.outputs if output.kind == "sheet"
        )
        assert sheet_id is not None
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM rows WHERE sheet_id=?", (sheet_id,)
            ).fetchone()[0]
            == 501
        )
        run_ref = next(
            output.ref for output in result.outputs if output.kind == "source_run"
        )
        source_run = project.db.execute(
            "SELECT cursor_after FROM source_runs WHERE id=?",
            (run_ref["source_run_id"],),
        ).fetchone()
        assert source_run is not None
        cursor = json.loads(source_run["cursor_after"])
        assert cursor["items_seen"] == 501
        assert cursor["truncated"] is False
    finally:
        project.close()


def test_default_youtube_channel_name_resolves_to_channel_title(
    tmp_path: Path,
    restore_youtube_channel_poller: None,
) -> None:
    url = f"https://www.youtube.com/{CHANNEL_HANDLE}/videos"
    default_name = f"YouTube channel: {url.removeprefix('https://www.')[:72]}"
    provider = FakeYouTubeChannelProvider(
        [_listing([_video("namedchan1", "Named upload")])]
    )
    register_source_poller(YouTubeChannelPoller(provider=provider), replace=True)
    project = Project.create(tmp_path / "youtube-channel-name.frisket", name="YouTube")
    try:
        result = run_action_spec(
            project,
            _source_poll_action(
                source={**_youtube_channel_source(), "name": default_name},
                idempotency_key="source_youtube_channel@sha256:resolved-name",
            ),
            project_id=PROJECT_ID,
        )
        assert result.status == "completed"
        source = SourceStore(project).sources()[0]
        assert source["name"] == "Investigative Channel"
        assert project.sheets()[0]["name"] == "Investigative Channel"
    finally:
        project.close()


def test_ytdlp_channel_provider_resolves_metadata_without_downloading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    class FakeYoutubeDL:
        def __init__(self, opts: dict[str, Any]) -> None:
            seen["opts"] = opts

        def __enter__(self) -> FakeYoutubeDL:
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

        def extract_info(self, url: str, *, download: bool) -> dict[str, Any]:
            seen["url"] = url
            seen["download"] = download
            return {
                "id": CHANNEL_ID,
                "title": "Investigative Channel",
                "channel_id": CHANNEL_ID,
                "entries": [
                    None,
                    "unparseable entry",
                    _video(
                        "fullmeta1",
                        "Metadata-rich upload",
                        upload_date="20260712",
                        description="Resolved by yt-dlp",
                    ),
                ],
            }

    monkeypatch.setitem(
        sys.modules,
        "yt_dlp",
        types.SimpleNamespace(YoutubeDL=FakeYoutubeDL),
    )
    listing = list_youtube_channel_with_ytdlp(
        f"https://www.youtube.com/{CHANNEL_HANDLE}/videos",
        channel_id=None,
        channel_handle=CHANNEL_HANDLE,
        max_pages=1,
        item_cap=10,
    )

    assert seen["download"] is False
    assert seen["opts"]["skip_download"] is True
    assert seen["opts"]["extract_flat"] is False
    assert seen["opts"]["playlistend"] == 10
    assert listing.entries[0]["upload_date"] == "20260712"
    assert listing.entries[0]["description"] == "Resolved by yt-dlp"
    assert len(listing.entries) == 1


def test_youtube_channel_source_poll_materializes_metadata_rows_and_dedupes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    restore_youtube_channel_poller: None,
) -> None:
    from frisket.ops import ytdlp

    def unexpected_download(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("source poll must not download YouTube media")

    monkeypatch.setattr(ytdlp, "download_media", unexpected_download)
    first_entries = [
        _video("chanalpha1", "Alpha channel upload", upload_date="20260620"),
        {"title": "[Deleted video]", "availability": "deleted"},
        _video(
            "chanbeta22",
            "Beta channel upload",
            upload_date="20260621",
            channel_id=None,
            channel=None,
        ),
    ]
    second_entries = list(first_entries)
    third_entries = [
        _video("chanalpha1", "Alpha channel upload", upload_date="20260620"),
        _video(
            "chanbeta22",
            "Beta channel upload revised",
            upload_date="20260621",
        ),
        _video("changamma3", "Gamma channel upload", upload_date="20260622"),
    ]
    provider = FakeYouTubeChannelProvider(
        [
            _listing(first_entries, pages_fetched=1),
            _listing(second_entries, pages_fetched=1),
            _listing(third_entries, pages_fetched=1),
        ]
    )
    register_source_poller(YouTubeChannelPoller(provider=provider), replace=True)

    project = Project.create(tmp_path / "youtube-channel.frisket", name="YouTube")
    try:
        first = run_action_spec(
            project,
            _source_poll_action(source=_youtube_channel_source()),
            project_id=PROJECT_ID,
        )
        assert first.status == "completed"
        assert first.receipt_id is not None
        assert provider.calls == [
            {
                "channel_url": (f"https://www.youtube.com/{CHANNEL_HANDLE}/videos"),
                "channel_id": None,
                "channel_handle": CHANNEL_HANDLE,
                "max_pages": 2,
                "item_cap": 200,
            }
        ]
        source_id = next(
            output.ref["source_id"]
            for output in first.outputs
            if output.kind == "source"
        )
        source_run_id = next(
            output.ref["source_run_id"]
            for output in first.outputs
            if output.kind == "source_run"
        )
        sheet_id = next(
            output.sheet_id for output in first.outputs if output.kind == "sheet"
        )
        assert sheet_id is not None

        rows = _rows(project, sheet_id)
        assert [row["video_id"] for row in rows] == ["chanalpha1", "chanbeta22"]
        assert rows[0]["source_id"] == source_id
        assert rows[0]["source_run_id"] == source_run_id
        assert rows[0]["source_item_id"] == "youtube:video:chanalpha1"
        assert rows[0]["url"] == "https://www.youtube.com/watch?v=chanalpha1"
        assert rows[0]["source_url"] == rows[0]["url"]
        assert rows[0]["channel_id"] == CHANNEL_ID
        assert rows[0]["channel_title"] == "Investigative Channel"
        assert "playlist_id" not in rows[0]
        assert rows[0]["published_at"] == "2026-06-20T00:00:00Z"
        assert rows[0]["description"] == "Source video description"
        assert rows[0]["thumbnail_url"].endswith("/chanalpha1/hqdefault.jpg")
        assert rows[0]["raw"]["id"] == "chanalpha1"
        assert rows[0]["source_raw"]["id"] == "chanalpha1"
        assert rows[1]["channel_id"] == CHANNEL_ID
        assert rows[1]["channel_title"] == "Investigative Channel"
        assert "media" not in rows[0]
        column_visibility = {
            str(column["name"]): bool(column["default_hidden"])
            for column in project.columns(sheet_id)
        }
        assert {
            name
            for name, default_hidden in column_visibility.items()
            if not default_hidden
        } == {"title", "source_url", "published_at", "channel_title", "description"}
        assert column_visibility["url"] is True
        assert column_visibility["source_raw"] is True

        first_run = project.db.execute(
            "SELECT * FROM source_runs WHERE id=?",
            (source_run_id,),
        ).fetchone()
        assert first_run is not None
        assert first_run["status"] == "ok"
        assert first_run["new_rows"] == 2
        assert first_run["warning_count"] == 1
        first_cursor = json.loads(first_run["cursor_after"])
        assert first_cursor["schema_version"] == "frisket.youtube_cursor.v1"
        assert first_cursor["source_kind"] == YOUTUBE_CHANNEL_KIND
        assert first_cursor["channel_id"] == CHANNEL_ID
        assert first_cursor["channel_handle"] == CHANNEL_HANDLE
        assert first_cursor["items_seen"] == 2
        assert first_cursor["newest_published_at"] == "2026-06-21T00:00:00Z"
        assert json.loads(first_run["summary_json"])["summary"]["pages_fetched"] == 1

        receipt = _receipt(project, first.receipt_id)
        assert receipt["action_kind"] == "source.poll"
        assert receipt["warnings"] == [
            "youtube_channel skipped unavailable video: [Deleted video]"
        ]
        assert receipt["provider_use"][0]["provider"] == "youtube"
        assert receipt["provider_use"][0]["service"] == "fake-yt-dlp"
        assert receipt["provider_use"][0]["source_kind"] == YOUTUBE_CHANNEL_KIND
        assert receipt["provider_use"][0]["download"] is False
        assert _table_count(project, "source_items") == 2
        assert _table_count(project, "blobs") == 0

        second = run_action_spec(
            project,
            _source_poll_action(
                source_id=source_id,
                idempotency_key="source_youtube_channel@sha256:second",
            ),
            project_id=PROJECT_ID,
        )
        assert second.status == "completed"
        second_run_id = next(
            output.ref["source_run_id"]
            for output in second.outputs
            if output.kind == "source_run"
        )
        second_run = project.db.execute(
            "SELECT * FROM source_runs WHERE id=?",
            (second_run_id,),
        ).fetchone()
        assert second_run["new_rows"] == 0
        assert second_run["skipped_rows"] == 2
        assert second_run["changed_rows"] == 0
        assert second_run["revisions"] == 0
        assert [row["video_id"] for row in _rows(project, sheet_id)] == [
            "chanalpha1",
            "chanbeta22",
        ]

        third = run_action_spec(
            project,
            _source_poll_action(
                source_id=source_id,
                idempotency_key="source_youtube_channel@sha256:third",
            ),
            project_id=PROJECT_ID,
        )
        assert third.status == "completed"
        third_run_id = next(
            output.ref["source_run_id"]
            for output in third.outputs
            if output.kind == "source_run"
        )
        third_run = project.db.execute(
            "SELECT * FROM source_runs WHERE id=?",
            (third_run_id,),
        ).fetchone()
        assert third_run["new_rows"] == 1
        assert third_run["skipped_rows"] == 1
        assert third_run["changed_rows"] == 1
        assert third_run["revisions"] == 1
        rows = _rows(project, sheet_id)
        assert [row["title"] for row in rows] == [
            "Alpha channel upload",
            "Beta channel upload",
            "Beta channel upload revised",
            "Gamma channel upload",
        ]
        assert rows[2]["_revises"] == "youtube:video:chanbeta22"
        assert rows[2]["_revision"] == 1
        beta_item = project.db.execute(
            "SELECT revision FROM source_items WHERE source_id=? AND dedupe_key=?",
            (source_id, "youtube:video:chanbeta22"),
        ).fetchone()
        assert beta_item is not None
        assert beta_item["revision"] == 1
        assert _table_count(project, "source_items") == 3
        assert _table_count(project, "blobs") == 0
    finally:
        project.close()


def test_youtube_channel_accepts_channel_url_id_and_handle_configs(
    tmp_path: Path,
    restore_youtube_channel_poller: None,
) -> None:
    cases = [
        (
            _youtube_channel_source(
                url=f"https://www.youtube.com/channel/{CHANNEL_ID}"
            ),
            {
                "channel_url": f"https://www.youtube.com/channel/{CHANNEL_ID}/videos",
                "channel_id": CHANNEL_ID,
                "channel_handle": None,
                "max_pages": 2,
                "item_cap": 200,
            },
        ),
        (
            _youtube_channel_source(
                {"channel_id": CHANNEL_ID},
                url=None,
            ),
            {
                "channel_url": f"https://www.youtube.com/channel/{CHANNEL_ID}/videos",
                "channel_id": CHANNEL_ID,
                "channel_handle": None,
                "max_pages": 2,
                "item_cap": 200,
            },
        ),
        (
            _youtube_channel_source(
                {"channel_handle": CHANNEL_HANDLE},
                url=None,
            ),
            {
                "channel_url": f"https://www.youtube.com/{CHANNEL_HANDLE}/videos",
                "channel_id": None,
                "channel_handle": CHANNEL_HANDLE,
                "max_pages": 2,
                "item_cap": 200,
            },
        ),
    ]

    for index, (source, expected_call) in enumerate(cases):
        provider = FakeYouTubeChannelProvider(
            [
                _listing(
                    [
                        _video(
                            f"case{index:02d}vid",
                            f"Case {index} upload",
                            upload_date="20260623",
                        )
                    ],
                    pages_fetched=1,
                )
            ]
        )
        register_source_poller(YouTubeChannelPoller(provider=provider), replace=True)
        project = Project.create(
            tmp_path / f"youtube-channel-case-{index}.frisket",
            name=f"Case {index}",
        )
        try:
            result = run_action_spec(
                project,
                _source_poll_action(
                    source=source,
                    idempotency_key=f"source_youtube_channel@sha256:case-{index}",
                ),
                project_id=PROJECT_ID,
            )
            assert result.status == "completed"
            assert provider.calls == [expected_call]
            sheet_id = next(
                output.sheet_id for output in result.outputs if output.kind == "sheet"
            )
            assert sheet_id is not None
            rows = _rows(project, sheet_id)
            assert rows[0]["video_id"] == f"case{index:02d}vid"
            assert rows[0]["channel_id"] == CHANNEL_ID
            assert rows[0]["channel_title"] == "Investigative Channel"
        finally:
            project.close()


def test_youtube_channel_validation_caps_and_provider_failure_redaction(
    tmp_path: Path,
    restore_youtube_channel_poller: None,
) -> None:
    invalid_project = Project.create(tmp_path / "invalid.frisket", name="Invalid")
    try:
        invalid = run_action_spec(
            invalid_project,
            _source_poll_action(
                source={
                    "kind": YOUTUBE_CHANNEL_KIND,
                    "name": "Playlist",
                    "url": "https://www.youtube.com/playlist?list=PLinvalid",
                    "config": {},
                },
                idempotency_key="source_youtube_channel@sha256:invalid",
            ),
            project_id=PROJECT_ID,
        )
        assert invalid.status == "failed"
        assert invalid.errors[0].code == "unsupported_source_config"
        assert "playlist" in invalid.errors[0].message
    finally:
        invalid_project.close()

    capped_provider = FakeYouTubeChannelProvider(
        [
            _listing(
                [
                    _video("capchan001", "One"),
                    _video("capchan002", "Two"),
                    _video("capchan003", "Three"),
                ],
                pages_fetched=3,
            )
        ]
    )
    register_source_poller(YouTubeChannelPoller(provider=capped_provider), replace=True)
    capped_project = Project.create(tmp_path / "capped.frisket", name="Capped")
    try:
        capped = run_action_spec(
            capped_project,
            _source_poll_action(
                source=_youtube_channel_source(
                    {"max_pages_per_poll": 1, "max_items_per_poll": 2}
                ),
                idempotency_key="source_youtube_channel@sha256:capped",
            ),
            project_id=PROJECT_ID,
        )
        assert capped.status == "completed"
        assert capped_provider.calls[0]["max_pages"] == 1
        assert capped_provider.calls[0]["item_cap"] == 2
        run_ref = next(
            output.ref for output in capped.outputs if output.kind == "source_run"
        )
        assert run_ref["new_rows"] == 2
        assert run_ref["warning_count"] == 1
        source_run = capped_project.db.execute(
            "SELECT * FROM source_runs WHERE id=?",
            (run_ref["source_run_id"],),
        ).fetchone()
        cursor = json.loads(source_run["cursor_after"])
        assert cursor["item_cap"] == 2
        assert cursor["max_pages_per_poll"] == 1
        assert cursor["truncated"] is True
        assert _receipt(capped_project, capped.receipt_id)["warnings"] == [
            "youtube_channel item cap reached; limited to 2 items"
        ]
    finally:
        capped_project.close()

    def failing_provider(*_args: Any, **_kwargs: Any) -> YouTubeChannelListing:
        raise RuntimeError(
            "quota failed api_key=SECRET token=SECRET authorization: Bearer abc123"
        )

    register_source_poller(
        YouTubeChannelPoller(provider=failing_provider), replace=True
    )
    failed_project = Project.create(tmp_path / "failed.frisket", name="Failed")
    try:
        source_id = SourceStore(failed_project).add_source(
            name="Failing Channel",
            kind=YOUTUBE_CHANNEL_KIND,
            url=f"https://www.youtube.com/{CHANNEL_HANDLE}/videos",
            config={"channel_handle": CHANNEL_HANDLE},
        )
        failed = run_action_spec(
            failed_project,
            _source_poll_action(
                source_id=source_id,
                idempotency_key="source_youtube_channel@sha256:provider-failed",
            ),
            project_id=PROJECT_ID,
        )
        assert failed.status == "failed"
        assert failed.receipt_id is not None
        assert failed.errors[0].code == "source_poll_failed"
        assert "SECRET" not in failed.errors[0].message
        assert "abc123" not in failed.errors[0].message
        source_run = SourceStore(failed_project).source_runs(source_id, limit=1)[0]
        assert source_run["status"] == "error"
        assert "SECRET" not in source_run["error"]
        assert "abc123" not in source_run["error"]
        receipt = _receipt(failed_project, failed.receipt_id)
        assert receipt["status"] == "failed"
        assert "SECRET" not in receipt["errors"][0]["message"]
        assert "abc123" not in receipt["errors"][0]["message"]
    finally:
        failed_project.close()


def test_youtube_channel_scheduled_source_dispatches_generic_source_poll(
    tmp_path: Path,
    restore_youtube_channel_poller: None,
) -> None:
    provider = FakeYouTubeChannelProvider(
        [_listing([_video("schedchan1", "Scheduled upload")], pages_fetched=1)]
    )
    register_source_poller(YouTubeChannelPoller(provider=provider), replace=True)

    workspace = tmp_path / "ws"
    workspace.mkdir()
    project = Project.create(workspace / "news.frisket", name="news")
    try:
        youtube_source_id = SourceStore(project).add_source(
            name="YouTube Channel",
            kind=YOUTUBE_CHANNEL_KIND,
            url=f"https://www.youtube.com/{CHANNEL_HANDLE}/videos",
            schedule="@hourly",
            config={"channel_handle": CHANNEL_HANDLE, "max_pages_per_poll": 1},
        )
        SourceStore(project).add_source(
            name="Unsupported API",
            kind="unsupported_api",
            url="https://example.com/api",
            schedule="@hourly",
        )
    finally:
        project.close()

    queue = SqliteJobQueue(workspace / ".queue.db")
    try:
        jobs = enqueue_due_source_polls(
            workspace_root=workspace,
            queue=queue,
        )
        assert len(jobs) == 1
        assert jobs[0]["source_id"] == youtube_source_id
        job_id = int(jobs[0]["job_id"])

        def unexpected_rss_fetch(_url: str) -> str:
            raise AssertionError("youtube_channel must not use RSS fetch")

        registry = default_registry()
        register_source_poll_handler(
            registry,
            workspace_root=workspace,
            queue=queue,
            fetch=unexpected_rss_fetch,
        )
        assert Worker(queue, registry).run_once()
        job = queue.get(job_id)
        assert job.status == "done"
        assert job.result["action_kind"] == "source.poll"
        assert job.result["source_id"] == youtube_source_id
        assert job.result["new_rows"] == 1
        assert provider.calls[0]["max_pages"] == 1

        reopened = Project(workspace / "news.frisket")
        try:
            runs = [
                dict(row)
                for row in SourceStore(reopened).source_runs(youtube_source_id)
            ]
            assert len(runs) == 1
            assert runs[0]["status"] == "ok"
            receipt = _receipt(reopened, job.result["receipt_id"])
            assert receipt["action_kind"] == "source.poll"
            assert receipt["provider_use"][0]["source_kind"] == YOUTUBE_CHANNEL_KIND
        finally:
            reopened.close()
    finally:
        queue.close()
