from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from frisket.ops.enclosures import ENCLOSURE_DOWNLOAD_KIND, mark_enclosure_queued
from frisket.engine.jobs import (
    SOURCE_POLL_KIND,
    SqliteJobQueue,
    enqueue_due_source_polls,
    enqueue_enclosure_downloads,
)
from frisket.engine.jobs.sources import _source_poll_job_identity
from frisket.engine.store import Project
from frisket.engine.store.sources import SourceStore


ROOT = Path(__file__).resolve().parents[2]
QUEUE = ROOT / "src/frisket/engine/jobs/queue.py"
SOURCES = ROOT / "src/frisket/engine/jobs/sources.py"


def _project(workspace: Path, project_id: str = "news") -> Project:
    workspace.mkdir(parents=True, exist_ok=True)
    return Project.create(workspace / f"{project_id}.frisket", name=project_id)


def _add_due_rss_source(project: Project) -> int:
    return SourceStore(project).add_source(
        name="News",
        kind="rss",
        url="https://example.com/feed.xml",
        schedule="@hourly",
    )


def _insert_newer_active_jobs(queue: SqliteJobQueue, *, count: int) -> None:
    for idx in range(count):
        queue.enqueue(
            "project.run",
            {
                "project_id": f"unrelated-{idx}",
                "run_id": idx + 1000,
                "workspace_root": f"/tmp/unrelated/{idx}",
            },
        )


def _insert_newer_running_jobs(queue: SqliteJobQueue, *, count: int) -> None:
    for idx in range(count):
        jid = queue.enqueue(
            SOURCE_POLL_KIND,
            {
                "project_id": f"unrelated-{idx}",
                "source_id": idx + 1000,
                "workspace_root": f"/tmp/unrelated/{idx}",
            },
        )
        claimed = queue.claim(f"worker-{idx}")
        assert claimed is not None and claimed.id == jid


def _row_with_remote_enclosure(project: Project) -> tuple[int, int]:
    sheet_id = project.add_sheet("feed")
    columns = {
        "guid": project.add_column(sheet_id, "guid"),
        "link": project.add_column(sheet_id, "link", type="link"),
        "enclosure_url": project.add_column(
            sheet_id,
            "enclosure_url",
            type="link",
        ),
        "enclosure_mime": project.add_column(sheet_id, "enclosure_mime"),
        "media": project.add_column(sheet_id, "media", type="audio"),
        "media_status": project.add_column(sheet_id, "media_status", type="category"),
    }
    row_id = project.add_rows(
        sheet_id,
        [
            {
                "guid": "ep1",
                "link": "https://pod.example/ep1",
                "enclosure_url": "https://cdn.example/ep1.mp3",
                "enclosure_mime": "audio/mpeg",
                "media_status": "remote",
            }
        ],
        columns,
    )[0]
    return sheet_id, row_id


def test_indexed_ref_lookup_rejects_under_scoped_workspace_queries(
    tmp_path: Path,
) -> None:
    queue = SqliteJobQueue(tmp_path / ".queue.db")
    try:
        with pytest.raises(ValueError, match="workspace_root"):
            queue.find_job_by_refs(
                SOURCE_POLL_KIND,
                project_id="news",
                source_id=1,
            )
        with pytest.raises(ValueError, match="project_id"):
            queue.find_job_by_refs(
                ENCLOSURE_DOWNLOAD_KIND,
                sheet_id=1,
                row_id=2,
                workspace_root=str(tmp_path),
            )
        with pytest.raises(ValueError, match="at least one status"):
            queue.find_job_by_refs(
                SOURCE_POLL_KIND,
                statuses=(),
                project_id="news",
                source_id=1,
                workspace_root=str(tmp_path),
            )
    finally:
        queue.close()


def test_due_source_poll_dedupe_uses_indexed_refs_beyond_recent_queue_window(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "ws"
    project = _project(workspace)
    source_id = _add_due_rss_source(project)
    project.close()
    queue = SqliteJobQueue(workspace / ".queue.db")
    try:
        old_job_id = queue.enqueue(
            SOURCE_POLL_KIND,
            {
                "project_id": "news",
                "source_id": source_id,
                "poll_id": "old-poll",
                "workspace_root": str(workspace),
            },
        )
        _insert_newer_active_jobs(queue, count=1001)

        jobs = enqueue_due_source_polls(
            workspace_root=workspace,
            queue=queue,
            now=datetime(2026, 6, 19, 12, 0, tzinfo=UTC),
        )

        assert jobs == []
        matching = [
            job
            for job in queue.list_jobs(status="queued", limit=2000)
            if job.kind == SOURCE_POLL_KIND
            and job.project_id == "news"
            and job.source_id == source_id
            and job.workspace_root == str(workspace)
        ]
        assert [job.id for job in matching] == [old_job_id]
    finally:
        queue.close()


def test_due_source_poll_dedupe_is_scoped_by_workspace_root(
    tmp_path: Path,
) -> None:
    other_workspace = tmp_path / "other" / "ws"
    workspace = tmp_path / "current" / "ws"
    project = _project(workspace)
    source_id = _add_due_rss_source(project)
    project.close()
    queue = SqliteJobQueue(tmp_path / ".queue.db")
    try:
        queue.enqueue(
            SOURCE_POLL_KIND,
            {
                "project_id": "news",
                "source_id": source_id,
                "poll_id": "other-workspace-poll",
                "workspace_root": str(other_workspace),
            },
        )
        queue.enqueue(
            SOURCE_POLL_KIND,
            {
                "project_id": "news",
                "source_id": source_id,
                "poll_id": "legacy-rootless-poll",
            },
        )

        jobs = enqueue_due_source_polls(
            workspace_root=workspace,
            queue=queue,
            now=datetime(2026, 6, 19, 12, 0, tzinfo=UTC),
        )

        assert len(jobs) == 1
        job = queue.get(int(jobs[0]["job_id"]))
        assert job is not None
        assert job.kind == SOURCE_POLL_KIND
        assert job.project_id == "news"
        assert job.source_id == source_id
        assert job.workspace_root == str(workspace)
    finally:
        queue.close()


def test_running_source_poll_identity_uses_indexed_refs_beyond_recent_window(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "ws"
    queue = SqliteJobQueue(workspace / ".queue.db")
    payload = {
        "project_id": "news",
        "source_id": 7,
        "poll_id": "old-running-poll",
        "workspace_root": str(workspace),
    }
    try:
        old_job_id = queue.enqueue(SOURCE_POLL_KIND, payload)
        claimed = queue.claim("target-worker")
        assert claimed is not None and claimed.id == old_job_id
        _insert_newer_running_jobs(queue, count=1001)

        identity = _source_poll_job_identity(queue, payload)

        assert identity == "old-running-poll:attempt:1"
    finally:
        queue.close()


def test_running_source_poll_identity_ignores_rootless_or_other_workspace_jobs(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "current" / "ws"
    other_workspace = tmp_path / "other" / "ws"
    queue = SqliteJobQueue(tmp_path / ".queue.db")
    payload = {
        "project_id": "news",
        "source_id": 7,
        "poll_id": "current-poll",
        "workspace_root": str(workspace),
    }
    try:
        rootless_job_id = queue.enqueue(
            SOURCE_POLL_KIND,
            {
                "project_id": "news",
                "source_id": 7,
                "poll_id": "legacy-rootless-poll",
            },
        )
        claimed = queue.claim("rootless-worker")
        assert claimed is not None and claimed.id == rootless_job_id
        other_job_id = queue.enqueue(
            SOURCE_POLL_KIND,
            {
                "project_id": "news",
                "source_id": 7,
                "poll_id": "other-workspace-poll",
                "workspace_root": str(other_workspace),
            },
        )
        claimed = queue.claim("other-worker")
        assert claimed is not None and claimed.id == other_job_id

        identity = _source_poll_job_identity(queue, payload)

        assert identity == "current-poll"
    finally:
        queue.close()


def test_enclosure_download_dedupe_uses_indexed_refs_beyond_recent_queue_window(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "ws"
    project = _project(workspace)
    sheet_id, row_id = _row_with_remote_enclosure(project)
    queue = SqliteJobQueue(workspace / ".queue.db")
    try:
        old_job_id = queue.enqueue(
            ENCLOSURE_DOWNLOAD_KIND,
            {
                "project_id": "news",
                "sheet_id": sheet_id,
                "row_id": row_id,
                "workspace_root": str(workspace),
            },
        )
        _insert_newer_active_jobs(queue, count=1001)

        jobs = enqueue_enclosure_downloads(
            queue,
            project=project,
            project_id="news",
            workspace_root=workspace,
            source={"config": {"download_enclosures": "queued"}},
            sheet_id=sheet_id,
            row_ids=[row_id],
        )

        assert jobs == []
        matching = [
            job
            for job in queue.list_jobs(status="queued", limit=2000)
            if job.kind == ENCLOSURE_DOWNLOAD_KIND
            and job.project_id == "news"
            and job.sheet_id == sheet_id
            and job.row_id == row_id
            and job.workspace_root == str(workspace)
        ]
        assert [job.id for job in matching] == [old_job_id]
        status = project.db.execute(
            "SELECT cells.value FROM cells "
            "JOIN columns ON columns.id = cells.column_id "
            "WHERE columns.sheet_id=? AND columns.name='media_status' "
            "AND cells.row_id=?",
            (sheet_id, row_id),
        ).fetchone()
        assert json.loads(status["value"]) == "remote"
    finally:
        queue.close()
        project.close()


def test_enclosure_download_dedupe_is_scoped_by_workspace_root(
    tmp_path: Path,
) -> None:
    other_workspace = tmp_path / "other" / "ws"
    workspace = tmp_path / "current" / "ws"
    project = _project(workspace)
    sheet_id, row_id = _row_with_remote_enclosure(project)
    queue = SqliteJobQueue(tmp_path / ".queue.db")
    try:
        queue.enqueue(
            ENCLOSURE_DOWNLOAD_KIND,
            {
                "project_id": "news",
                "sheet_id": sheet_id,
                "row_id": row_id,
                "workspace_root": str(other_workspace),
            },
        )
        queue.enqueue(
            ENCLOSURE_DOWNLOAD_KIND,
            {
                "project_id": "news",
                "sheet_id": sheet_id,
                "row_id": row_id,
            },
        )

        jobs = enqueue_enclosure_downloads(
            queue,
            project=project,
            project_id="news",
            workspace_root=workspace,
            source={"config": {"download_enclosures": "queued"}},
            sheet_id=sheet_id,
            row_ids=[row_id],
        )

        assert len(jobs) == 1
        job = queue.get(jobs[0])
        assert job is not None
        assert job.kind == ENCLOSURE_DOWNLOAD_KIND
        assert job.project_id == "news"
        assert job.sheet_id == sheet_id
        assert job.row_id == row_id
        assert job.workspace_root == str(workspace)
    finally:
        queue.close()
        project.close()


def test_enclosure_download_dedupe_respects_applied_queued_status_edit(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "ws"
    project = _project(workspace)
    sheet_id, row_id = _row_with_remote_enclosure(project)
    queue = SqliteJobQueue(workspace / ".queue.db")
    try:
        mark_enclosure_queued(project, sheet_id=sheet_id, row_id=row_id)

        jobs = enqueue_enclosure_downloads(
            queue,
            project=project,
            project_id="news",
            workspace_root=workspace,
            source={"config": {"download_enclosures": "queued"}},
            sheet_id=sheet_id,
            row_ids=[row_id],
        )

        assert jobs == []
        assert queue.list_jobs(status="queued", limit=10) == []
    finally:
        queue.close()
        project.close()
