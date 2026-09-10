from __future__ import annotations

from datetime import UTC, datetime, timedelta
from importlib.metadata import version
from types import SimpleNamespace

import sqlalchemy as sa

from frisket.engine.jobs.queue import open_queue
from frisket.team.readiness import (
    CONTROL_DATABASE_UNREACHABLE,
    IDENTITY_SCHEMA_VERSION,
    NO_CURRENT_WORKER,
    RUN_QUEUE_UNREACHABLE,
    STANDALONE_WORKER_EXITED,
    deployment_readiness,
)


def _engine(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'control.sqlite3'}")
    with engine.begin() as cx:
        cx.execute(sa.text("CREATE TABLE ready_probe (id INTEGER PRIMARY KEY)"))
    return engine


def test_readiness_requires_fresh_matching_worker_even_when_queue_is_empty(tmp_path):
    engine = _engine(tmp_path)
    queue = open_queue(database_url=f"sqlite:///{tmp_path / 'queue.sqlite3'}")
    now = datetime.now(UTC)

    missing = deployment_readiness(
        engine=engine,
        queue=queue,
        target_code_version="release-a",
        now=now,
    )
    assert missing["schema_version"] == "frisket.server_readiness.v1"
    assert missing["ok"] is False
    assert missing["failures"] == [NO_CURRENT_WORKER]
    assert missing["identity"] == {
        "schema_version": IDENTITY_SCHEMA_VERSION,
        "package_version": version("frisket"),
        "code_version": "release-a",
    }

    queue.record_worker_heartbeat(
        "old-worker",
        worker_version="release-a",
        now=now - timedelta(seconds=61),
    )
    queue.record_worker_heartbeat(
        "wrong-worker",
        worker_version="release-b",
        now=now,
    )
    assert deployment_readiness(
        engine=engine,
        queue=queue,
        target_code_version="release-a",
        now=now,
    )["failures"] == [NO_CURRENT_WORKER]

    queue.record_worker_heartbeat("current-worker", worker_version="release-a", now=now)
    assert (
        deployment_readiness(
            engine=engine,
            queue=queue,
            target_code_version="release-a",
            now=now,
        )["ok"]
        is True
    )


def test_readiness_sanitizes_backend_and_runtime_failures(tmp_path):
    class BrokenEngine:
        def connect(self):
            raise RuntimeError("postgresql://user:secret@db/control")

    class BrokenQueue:
        def counts(self):
            raise RuntimeError("postgresql://user:secret@db/queue")

        def list_worker_heartbeats(self):
            raise AssertionError("unreachable")

    report = deployment_readiness(
        engine=BrokenEngine(),
        queue=BrokenQueue(),
        runtime_state=SimpleNamespace(worker_exited_unexpectedly=True),
        target_code_version="release-a",
    )
    assert report["ok"] is False
    assert report["failures"] == [
        CONTROL_DATABASE_UNREACHABLE,
        RUN_QUEUE_UNREACHABLE,
        STANDALONE_WORKER_EXITED,
    ]
    assert "secret" not in str(report)
    assert report["identity"]["code_version"] == "release-a"
