from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from frisket.engine.executor import run_action_spec
from frisket.engine.jobs import (
    SOURCE_POLL_KIND,
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
    YOUTUBE_PLAYLIST_KIND,
    YouTubePlaylistListing,
    YouTubePlaylistPoller,
)
from frisket.engine.store import Project
from frisket.engine.store.sources import SourceStore


PROJECT_ID = "project-source-youtube"
PLAYLIST_ID = "PLinvestigative123"


class FakeYouTubeProvider:
    def __init__(self, listings: list[YouTubePlaylistListing]) -> None:
        self._listings = list(listings)
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        playlist_url: str,
        *,
        playlist_id: str,
        max_pages: int | None,
        item_cap: int | None,
    ) -> YouTubePlaylistListing:
        self.calls.append(
            {
                "playlist_url": playlist_url,
                "playlist_id": playlist_id,
                "max_pages": max_pages,
                "item_cap": item_cap,
            }
        )
        if not self._listings:
            raise AssertionError("fake YouTube provider exhausted")
        listing = self._listings.pop(0)
        limits = [limit for limit in (item_cap, max_pages and max_pages * 100) if limit]
        provider_limit = min(limits) if limits else None
        truncated = provider_limit is not None and len(listing.entries) > provider_limit
        warnings = list(listing.warnings)
        if truncated:
            warnings.append(
                f"youtube_playlist item cap reached; limited to {provider_limit} items"
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
def restore_youtube_poller() -> Iterator[None]:
    previous = get_source_poller(YOUTUBE_PLAYLIST_KIND)
    try:
        yield
    finally:
        if previous is None:
            unregister_source_poller(YOUTUBE_PLAYLIST_KIND)
        else:
            register_source_poller(previous, replace=True)


def _source_poll_action(
    *,
    source_id: int | None = None,
    source: dict[str, Any] | None = None,
    idempotency_key: str = "source_youtube@sha256:first",
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


def _youtube_source(config: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "kind": YOUTUBE_PLAYLIST_KIND,
        "name": "Investigative Playlist",
        "url": f"https://www.youtube.com/playlist?list={PLAYLIST_ID}",
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
    playlist_title: str | None = "Investigative Playlist",
    channel_title: str | None = "Investigative Channel",
    **facts: Any,
) -> YouTubePlaylistListing:
    return YouTubePlaylistListing(
        playlist_id=PLAYLIST_ID,
        entries=entries,
        playlist_title=playlist_title,
        channel_title=channel_title,
        provider_facts={"service": "fake-yt-dlp", **facts},
    )


def test_youtube_playlist_solo_poll_materializes_legacy_cap_plus_one(
    tmp_path: Path,
    restore_youtube_poller: None,
) -> None:
    entries = [_video(f"bulk{index:07d}", f"Video {index}") for index in range(501)]
    provider = FakeYouTubeProvider(
        [_listing(entries, pages_fetched=6, playlist_count=501)]
    )
    register_source_poller(YouTubePlaylistPoller(provider=provider), replace=True)
    source = _youtube_source()
    source["config"].pop("max_pages_per_poll")

    project = Project.create(tmp_path / "youtube-unbounded.frisket", name="YouTube")
    try:
        result = run_action_spec(
            project,
            _source_poll_action(
                source=source,
                idempotency_key="source_youtube@sha256:legacy-cap-plus-one",
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


def test_default_youtube_playlist_name_resolves_to_channel_and_playlist(
    tmp_path: Path,
    restore_youtube_poller: None,
) -> None:
    url = f"https://www.youtube.com/playlist?list={PLAYLIST_ID}"
    default_name = f"YouTube playlist: {url.removeprefix('https://www.')[:72]}"
    provider = FakeYouTubeProvider(
        [
            _listing(
                [_video("named00123A", "Named upload")],
                playlist_title="Weekly Accountability Roundup",
                channel_title="Investigative Channel",
            )
        ]
    )
    register_source_poller(YouTubePlaylistPoller(provider=provider), replace=True)
    project = Project.create(tmp_path / "youtube-playlist-name.frisket", name="YouTube")
    try:
        result = run_action_spec(
            project,
            _source_poll_action(
                source={**_youtube_source(), "name": default_name},
                idempotency_key="source_youtube@sha256:resolved-name",
            ),
            project_id=PROJECT_ID,
        )
        assert result.status == "completed"
        source = SourceStore(project).sources()[0]
        assert source["name"] == "Investigative Channel: Weekly Accountability Roundup"
        assert project.sheets()[0]["name"] == source["name"]
    finally:
        project.close()


def _video(
    video_id: str,
    title: str,
    *,
    channel_id: str = "UCinvestigative",
    channel: str = "Investigative Channel",
    upload_date: str = "20260622",
    description: str = "Source video description",
) -> dict[str, Any]:
    return {
        "id": video_id,
        "title": title,
        "webpage_url": f"https://www.youtube.com/watch?v={video_id}",
        "channel_id": channel_id,
        "channel": channel,
        "upload_date": upload_date,
        "description": description,
        "thumbnail": f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg",
    }


def test_youtube_playlist_source_poll_materializes_metadata_rows_and_dedupes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    restore_youtube_poller: None,
) -> None:
    from frisket.ops import ytdlp

    def unexpected_download(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("source poll must not download YouTube media")

    monkeypatch.setattr(ytdlp, "download_media", unexpected_download)
    first_entries = [
        _video("alpha00123A", "Alpha hearing clip", upload_date="20260620"),
        {"title": "[Private video]", "availability": "private"},
        _video("beta001234B", "Beta press conference", upload_date="20260621"),
    ]
    second_entries = list(first_entries)
    third_entries = [
        _video("alpha00123A", "Alpha hearing clip", upload_date="20260620"),
        _video(
            "beta001234B",
            "Beta press conference revised",
            upload_date="20260621",
        ),
        _video("gamma00123C", "Gamma upload", upload_date="20260622"),
    ]
    provider = FakeYouTubeProvider(
        [
            _listing(first_entries, pages_fetched=1),
            _listing(second_entries, pages_fetched=1),
            _listing(third_entries, pages_fetched=1),
        ]
    )
    register_source_poller(YouTubePlaylistPoller(provider=provider), replace=True)

    project = Project.create(tmp_path / "youtube-source.frisket", name="YouTube")
    try:
        first = run_action_spec(
            project,
            _source_poll_action(source=_youtube_source()),
            project_id=PROJECT_ID,
        )
        assert first.status == "completed"
        assert first.receipt_id is not None
        assert provider.calls == [
            {
                "playlist_url": (
                    f"https://www.youtube.com/playlist?list={PLAYLIST_ID}"
                ),
                "playlist_id": PLAYLIST_ID,
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
        assert [row["video_id"] for row in rows] == ["alpha00123A", "beta001234B"]
        assert rows[0]["source_id"] == source_id
        assert rows[0]["source_run_id"] == source_run_id
        assert rows[0]["source_item_id"] == "youtube:video:alpha00123A"
        assert rows[0]["url"] == "https://www.youtube.com/watch?v=alpha00123A"
        assert rows[0]["source_url"] == rows[0]["url"]
        assert rows[0]["channel_id"] == "UCinvestigative"
        assert rows[0]["channel_title"] == "Investigative Channel"
        assert rows[0]["playlist_id"] == PLAYLIST_ID
        assert rows[0]["published_at"] == "2026-06-20T00:00:00Z"
        assert rows[0]["description"] == "Source video description"
        assert rows[0]["thumbnail_url"].endswith("/alpha00123A/hqdefault.jpg")
        assert rows[0]["raw"]["id"] == "alpha00123A"
        assert rows[0]["source_raw"]["id"] == "alpha00123A"
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
        assert first_cursor["playlist_id"] == PLAYLIST_ID
        assert first_cursor["items_seen"] == 2
        assert first_cursor["newest_published_at"] == "2026-06-21T00:00:00Z"
        assert json.loads(first_run["summary_json"])["summary"]["pages_fetched"] == 1

        receipt = _receipt(project, first.receipt_id)
        assert receipt["action_kind"] == "source.poll"
        assert receipt["status"] == "completed"
        assert receipt["warnings"] == [
            "youtube_playlist skipped unavailable video: [Private video]"
        ]
        assert receipt["provider_use"][0]["provider"] == "youtube"
        assert receipt["provider_use"][0]["service"] == "fake-yt-dlp"
        assert receipt["provider_use"][0]["download"] is False
        refs = [
            item["ref"]
            for item in [*receipt["outputs"], *receipt["evidence"]]
            if isinstance(item, dict)
        ]
        assert {"source_poll_run", "source_poll_rows", "source_poll_cursor"} <= {
            ref["kind"] for ref in refs
        }
        assert _table_count(project, "source_items") == 2
        assert _table_count(project, "blobs") == 0

        second = run_action_spec(
            project,
            _source_poll_action(
                source_id=source_id,
                idempotency_key="source_youtube@sha256:second",
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
            "alpha00123A",
            "beta001234B",
        ]
        assert _table_count(project, "source_items") == 2

        third = run_action_spec(
            project,
            _source_poll_action(
                source_id=source_id,
                idempotency_key="source_youtube@sha256:third",
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
            "Alpha hearing clip",
            "Beta press conference",
            "Beta press conference revised",
            "Gamma upload",
        ]
        assert rows[2]["_revises"] == "youtube:video:beta001234B"
        assert rows[2]["_revision"] == 1
        beta_item = project.db.execute(
            "SELECT revision FROM source_items WHERE source_id=? AND dedupe_key=?",
            (source_id, "youtube:video:beta001234B"),
        ).fetchone()
        assert beta_item is not None
        assert beta_item["revision"] == 1
        assert _table_count(project, "source_items") == 3
        assert _table_count(project, "blobs") == 0
    finally:
        project.close()


def test_youtube_playlist_validation_caps_and_provider_failure_redaction(
    tmp_path: Path,
    restore_youtube_poller: None,
) -> None:
    invalid_project = Project.create(tmp_path / "invalid.frisket", name="Invalid")
    try:
        invalid = run_action_spec(
            invalid_project,
            _source_poll_action(
                source={
                    "kind": YOUTUBE_PLAYLIST_KIND,
                    "name": "Channel",
                    "url": "https://www.youtube.com/@example",
                    "config": {},
                },
                idempotency_key="source_youtube@sha256:invalid",
            ),
            project_id=PROJECT_ID,
        )
        assert invalid.status == "failed"
        assert invalid.errors[0].code == "unsupported_source_config"
        assert "channel" in invalid.errors[0].message
    finally:
        invalid_project.close()

    capped_provider = FakeYouTubeProvider(
        [
            _listing(
                [
                    _video("cap0000001", "One"),
                    _video("cap0000002", "Two"),
                    _video("cap0000003", "Three"),
                ],
                pages_fetched=3,
            )
        ]
    )
    register_source_poller(
        YouTubePlaylistPoller(provider=capped_provider), replace=True
    )
    capped_project = Project.create(tmp_path / "capped.frisket", name="Capped")
    try:
        capped = run_action_spec(
            capped_project,
            _source_poll_action(
                source=_youtube_source(
                    {"max_pages_per_poll": 1, "max_items_per_poll": 2}
                ),
                idempotency_key="source_youtube@sha256:capped",
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
            "youtube_playlist item cap reached; limited to 2 items"
        ]
    finally:
        capped_project.close()

    def failing_provider(*_args: Any, **_kwargs: Any) -> YouTubePlaylistListing:
        raise RuntimeError(
            "quota failed api_key=SECRET token=SECRET authorization: Bearer abc123"
        )

    register_source_poller(
        YouTubePlaylistPoller(provider=failing_provider), replace=True
    )
    failed_project = Project.create(tmp_path / "failed.frisket", name="Failed")
    try:
        source_id = SourceStore(failed_project).add_source(
            name="Failing Playlist",
            kind=YOUTUBE_PLAYLIST_KIND,
            url=f"https://www.youtube.com/playlist?list={PLAYLIST_ID}",
            config={"playlist_id": PLAYLIST_ID},
        )
        failed = run_action_spec(
            failed_project,
            _source_poll_action(
                source_id=source_id,
                idempotency_key="source_youtube@sha256:provider-failed",
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


def test_youtube_playlist_scheduled_source_dispatches_generic_source_poll(
    tmp_path: Path,
    restore_youtube_poller: None,
) -> None:
    provider = FakeYouTubeProvider(
        [_listing([_video("sched0012A", "Scheduled upload")], pages_fetched=1)]
    )
    register_source_poller(YouTubePlaylistPoller(provider=provider), replace=True)

    workspace = tmp_path / "ws"
    workspace.mkdir()
    project = Project.create(workspace / "news.frisket", name="news")
    try:
        youtube_source_id = SourceStore(project).add_source(
            name="YouTube",
            kind=YOUTUBE_PLAYLIST_KIND,
            url=f"https://www.youtube.com/playlist?list={PLAYLIST_ID}",
            schedule="@hourly",
            config={"playlist_id": PLAYLIST_ID, "max_pages_per_poll": 1},
        )
        SourceStore(project).add_source(
            name="Unsupported API",
            kind="api",
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
            raise AssertionError("youtube_playlist must not use RSS fetch")

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
        assert job.result["embedding_refresh_jobs"] == 0
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
            assert receipt["provider_use"][0]["provider"] == "youtube"
        finally:
            reopened.close()
    finally:
        queue.close()


def test_scheduled_source_poll_idempotency_is_stable_across_attempts(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    project = Project.create(workspace / "news.frisket", name="news")
    try:
        source_id = SourceStore(project).add_source(
            name="Feed",
            kind="rss",
            url="https://example.com/feed.xml",
            schedule="@hourly",
        )
    finally:
        project.close()

    queue = SqliteJobQueue(workspace / ".queue.db")
    try:
        job_id = queue.enqueue(
            SOURCE_POLL_KIND,
            {
                "project_id": "news",
                "source_id": source_id,
                "poll_id": "news:1:2026-06-23T12:00:00.000000+00:00",
                "workspace_root": str(workspace),
            },
            max_attempts=2,
        )
        calls = 0

        def fetch(_url: str) -> str:
            nonlocal calls
            calls += 1
            raise RuntimeError("feed down")

        worker = Worker(
            queue,
            default_registry(),
            retry_base_seconds=0,
        )
        register_source_poll_handler(
            worker.registry,
            workspace_root=workspace,
            queue=queue,
            fetch=fetch,
        )
        assert worker.run_once()
        assert worker.run_once()

        job = queue.get(job_id)
        assert job.status == "failed"
        assert job.attempts == 2
        assert calls == 1
        reopened = Project(workspace / "news.frisket")
        try:
            receipts = [
                dict(row)
                for row in reopened.db.execute(
                    "SELECT idempotency_key, status FROM receipts "
                    "WHERE action_kind='source.poll'"
                ).fetchall()
            ]
            assert len(receipts) == 1
            assert receipts[0]["status"] == "failed"
            runs = [dict(row) for row in SourceStore(reopened).source_runs(source_id)]
            assert len(runs) == 1
            assert runs[0]["status"] == "error"
        finally:
            reopened.close()
    finally:
        queue.close()
