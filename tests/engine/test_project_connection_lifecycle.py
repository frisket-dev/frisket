from __future__ import annotations

import gc
import sqlite3
import threading
from pathlib import Path

import pytest

from frisket.engine.store import Project
from frisket.engine.store.blob_backend import FilesystemProjectBlobStore


def test_retired_threads_release_their_project_connections(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "thread-churn.frisket")
    errors: list[BaseException] = []
    retired_connections: list[sqlite3.Connection] = []

    def query_project() -> None:
        try:
            connection = project.db
            assert connection.execute("SELECT 1").fetchone()[0] == 1
            # Keep the raw connection reachable after the thread exits so the
            # assertion below proves the holder closed it, not merely that the
            # connection object happened to be garbage-collected too.
            retired_connections.append(connection)
        except BaseException as exc:
            errors.append(exc)

    try:
        # A server with a 1024-descriptor limit used to exhaust it after roughly
        # this many connections because SQLite holds both DB and WAL files open.
        for _ in range(600):
            # realtime: threads ARE the subject here — connections are
            # thread-owned, so retirement cannot be exercised without one.
            # Started and joined immediately; no clock is raced.
            thread = threading.Thread(target=query_project)  # realtime: see above
            thread.start()
            thread.join()

        gc.collect()

        assert errors == []
        assert len(project._connections) == 1
        for connection in retired_connections:
            with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
                connection.execute("SELECT 1")
    finally:
        project.close()


def test_live_threads_keep_distinct_project_connections(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "live-threads.frisket")
    release = threading.Event()
    ready = threading.Semaphore(0)
    connection_ids: list[int] = []
    errors: list[BaseException] = []

    def hold_connection() -> None:
        try:
            connection_ids.append(id(project.db))
            ready.release()
            release.wait()
        except BaseException as exc:
            errors.append(exc)

    # realtime: one connection per live thread is the property under test.
    # The threads rendezvous on a semaphore, so ordering is deterministic
    # rather than timing-dependent.
    threads = [  # realtime: see above
        threading.Thread(target=hold_connection) for _ in range(4)
    ]
    try:
        for thread in threads:
            thread.start()
            assert ready.acquire(timeout=5)

        assert errors == []
        assert len(set(connection_ids)) == len(threads)
        assert len(project._connections) == len(threads) + 1
    finally:
        release.set()
        for thread in threads:
            thread.join(timeout=5)
        project.close()


def test_project_close_closes_live_connections_and_allows_reopen(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "close.frisket")
    old_connection = project.db

    project.close()

    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        old_connection.execute("SELECT 1")
    assert project.db.execute("SELECT 1").fetchone()[0] == 1
    project.close()


def test_closed_project_refuses_blob_writes_before_backend_effect(
    tmp_path: Path,
) -> None:
    blob_root = tmp_path / "canonical-blobs"
    project = Project.create(
        tmp_path / "closed-blob-writes.frisket",
        blob_store=FilesystemProjectBlobStore(root=blob_root),
    )
    source = tmp_path / "late-result.bin"
    source.write_bytes(b"late provider result")

    project.close()

    for write in (
        lambda: project.add_blob(b"late in-memory result"),
        lambda: project.add_blob_from_path(source),
    ):
        with pytest.raises(RuntimeError, match="closed.*blob write"):
            write()

    assert not blob_root.exists() or not any(blob_root.rglob("*"))
    # Keep the pre-existing connection-lifecycle contract narrow: ordinary DB
    # access may reopen, but the retired object can no longer publish bytes.
    assert project.db.execute("SELECT 1").fetchone()[0] == 1
    project.close()
