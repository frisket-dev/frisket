"""One unreadable project bundle must not stop a scheduler scan of its root.

Each worker sweep scans every bundle under a root; a bundle that cannot be
opened is logged and skipped, and the good bundles after it are still
enqueued.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path

import pytest

import frisket.cli as cli
from frisket.engine.executor import run_action_spec
from frisket.engine.jobs import SqliteJobQueue
from frisket.engine.store import Project
from frisket.engine.store.feeds import FeedStore
from tests.alert_helpers import add_alert
from frisket.engine.store.transactions import write_transaction

GOOD = ("a-good", "c-good")
UNREADABLE = "b-unreadable"


def _due_source(project: Project) -> None:
    with write_transaction(project.db):
        FeedStore(project).add_feed(
            name="News",
            kind="rss",
            url="https://example.com/feed.xml",
            schedule="@hourly",
        )


def _due_embedding_index(project: Project) -> None:
    sheet = project.add_sheet("feed")
    cols = {"headline": project.add_column(sheet, "headline")}
    project.add_rows(sheet, [{"headline": "story"}], cols)
    result = run_action_spec(
        project,
        {
            "action_id": "embedding.index_create",
            "scope": {"kind": "project"},
            "params": {
                "sheet_id": sheet,
                "source_columns": ["headline"],
                "modality": "text",
                "provider": "fastembed",
                "source_policy": {"kind": "text_cell"},
                "maintenance_policy": {"mode": "scheduled", "schedule": "@hourly"},
                "provider_policy": {"allow_remote": False},
            },
            "idempotency_key": "emb_create@isolation",
        },
        project_id=project.path.stem,
    )
    assert result.status == "completed", result.errors


def _due_alert(project: Project) -> None:
    sheet = project.add_sheet("Records")
    project.add_column(sheet, "text")
    add_alert(
        project,
        "Monitor",
        sheet_id=sheet,
        query={"kind": "filter", "sheet_id": sheet, "filter": {}},
    )


def _due_digest(project: Project) -> None:
    channel = project.create_notification_channel(
        kind="email",
        name="Digest email",
        config={"to": "alerts@example.com"},
        secret_ref="env:RESEND_API_KEY",
    )
    project.create_notification_route(
        name="Daily email",
        channel_id=channel["id"],
        source_kind="plugin.acme",
        delivery_mode="digest",
        digest_cadence="daily",
        digest_timezone="UTC",
        digest_anchor_time="00:00",
    )


SWEEPS: dict[str, tuple[cli._Sweep, Callable[[Project], None], str]] = {
    "source": (cli._FEED_POLLS, _due_source, "source_project_schedule_failed"),
    "embedding": (
        cli._SCHEDULED_EMBEDDINGS,
        _due_embedding_index,
        "embedding_project_schedule_failed",
    ),
    "watch": (cli._ALERTS, _due_alert, "alert_project_schedule_failed"),
    "digest": (
        cli._NOTIFICATION_DIGESTS,
        _due_digest,
        "notification_digest_project_schedule_failed",
    ),
}


def _workspace(root: Path, make_due: Callable[[Project], None]) -> None:
    for slug in GOOD:
        project = Project.create(root / f"{slug}.frisket", name=slug)
        try:
            make_due(project)
        finally:
            project.close()
    unreadable = root / f"{UNREADABLE}.frisket"
    unreadable.mkdir()
    (unreadable / "manifest.json").write_text(json.dumps({"name": UNREADABLE}))
    (unreadable / "project.db").write_bytes(b"not a sqlite database" * 64)


@pytest.mark.parametrize("scoped", [False, True], ids=["direct", "opener"])
@pytest.mark.parametrize("kind", sorted(SWEEPS))
def test_unreadable_bundle_does_not_stop_the_scan(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, kind: str, scoped: bool
) -> None:
    sweep, make_due, failure_event = SWEEPS[kind]
    root = tmp_path / "ws"
    root.mkdir()
    _workspace(root, make_due)
    opened: list[str] = []

    def opener(key, path: Path) -> Project:
        assert key.storage_org_id == 23
        opened.append(key.project_slug)
        return Project(path)

    queue = SqliteJobQueue(tmp_path / "queue.db", cloud=scoped)
    try:
        with caplog.at_level(logging.WARNING):
            enqueued = cli._sweep(
                sweep,
                queue,
                [(root, 23 if scoped else None)],
                project_opener=opener if scoped else None,
            )
        queued = queue.list_jobs(status="queued")
    finally:
        queue.close()

    assert enqueued == 2
    assert sorted(job.payload["project_id"] for job in queued) == list(GOOD)
    if scoped:
        assert opened == ["a-good", UNREADABLE, "c-good"]
    failures = [r for r in caplog.records if r.message == failure_event]
    assert [r.project_id for r in failures] == [UNREADABLE]
    assert failures[0].event == failure_event
    assert failures[0].project_path == str(root / f"{UNREADABLE}.frisket")
    assert failures[0].exc_info is not None
    assert not [r for r in caplog.records if r.message == sweep.skipped_event]
