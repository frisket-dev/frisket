from __future__ import annotations

import sqlite3
import time
from contextlib import closing

import pytest

from frisket.engine.store import Project
from frisket.retrieval.store import (
    SCHEMA_VERSION,
    RetrievalStore,
    UnsupportedRetrievalSchema,
    PROJECT_SIDECAR_META_KEY,
    read_sidecar_id,
)
from frisket.retrieval.recovery import prepare_retrieval_on_open


@pytest.mark.parametrize("kind", ["garbage", "foreign", "old_v1", "old_v2"])
def test_unusable_retrieval_cache_is_preserved_and_rebuilt_on_open(tmp_path, kind):
    from frisket.search import drain_index, search_project

    path = tmp_path / f"recover-{kind}.frisket"
    with closing(Project.create(path)) as project:
        sheet = project.add_sheet("Notes")
        column = project.add_column(sheet, "body")
        project.add_rows(sheet, [{"body": "recoverable needle"}], {"body": column})
    sidecar = path / "project.retrieval.db"
    if kind == "garbage":
        sidecar.write_bytes(b"not a sqlite database")
    else:
        with sqlite3.connect(sidecar) as db:
            db.execute("CREATE TABLE preserved_work(value TEXT)")
            db.execute("INSERT INTO preserved_work VALUES ('paid vector remains')")
            if kind.startswith("old_v"):
                db.execute(
                    "CREATE TABLE retrieval_meta(key TEXT PRIMARY KEY,value TEXT)"
                )
                db.execute(
                    "INSERT INTO retrieval_meta VALUES ('schema_version',?)",
                    (kind.removeprefix("old_v"),),
                )
    with closing(Project(path)) as project:
        archives = list(path.glob("retrieval-unusable-*/project.retrieval.db"))
        assert len(archives) == 1
        if kind == "garbage":
            assert archives[0].read_bytes() == b"not a sqlite database"
        else:
            with sqlite3.connect(archives[0]) as db:
                assert db.execute("SELECT value FROM preserved_work").fetchone() == (
                    "paid vector remains",
                )
        assert project.db.execute("SELECT 1 FROM search_dirty_scopes").fetchone()
        drain_index(project)
        assert search_project(project, "needle")
    with closing(Project(path)):
        assert len(list(path.glob("retrieval-unusable-*"))) == 1


def test_open_defers_repair_while_initial_sidecar_creation_is_locked(tmp_path) -> None:
    project = Project.create(tmp_path / "sidecar-creation-race.frisket")
    writer: sqlite3.Connection | None = None
    try:
        sheet = project.add_sheet("Notes")
        column = project.add_column(sheet, "body")
        project.add_rows(sheet, [{"body": "repair later"}], {"body": column})
        project.db.execute("DELETE FROM search_dirty_scopes")
        project.db.commit()

        path = project.path / "project.retrieval.db"
        writer = sqlite3.connect(path, isolation_level=None)
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        writer.execute("BEGIN IMMEDIATE")
        writer.execute(
            "CREATE TABLE retrieval_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)"
        )
        writer.execute(
            "INSERT INTO retrieval_meta(key,value) VALUES ('schema_version',?)",
            (str(SCHEMA_VERSION),),
        )

        started = time.monotonic()  # realtime: bound actual SQLite lock waiting
        prepare_retrieval_on_open(project)
        elapsed = time.monotonic() - started  # realtime: assert nonblocking open

        assert elapsed < 1
        assert project.db.execute(
            "SELECT 1 FROM search_dirty_scopes WHERE sheet_id IS NULL "
            "AND scan_cursor='[0,0]'"
        ).fetchone()

        writer.rollback()
        writer.close()
        writer = None
        prepare_retrieval_on_open(project)
        assert read_sidecar_id(project) is not None
    finally:
        if writer is not None:
            writer.rollback()
            writer.close()
        project.close()


def test_project_open_does_not_wait_for_current_sidecar_writer(tmp_path) -> None:
    from frisket.search_index import bind_retrieval_sidecar

    path = tmp_path / "sidecar-writer.frisket"
    with closing(Project.create(path)) as project:
        sidecar_id = bind_retrieval_sidecar(project)

    writer = sqlite3.connect(path / "project.retrieval.db", isolation_level=None)
    try:
        writer.execute("BEGIN IMMEDIATE")
        started = time.monotonic()  # realtime: bound actual SQLite lock waiting
        with closing(Project(path)) as reopened:
            assert read_sidecar_id(reopened) == sidecar_id
        elapsed = time.monotonic() - started  # realtime: assert nonblocking open
        assert elapsed < 1
    finally:
        writer.rollback()
        writer.close()


def test_project_database_failure_does_not_quarantine_healthy_sidecar(
    tmp_path, monkeypatch
) -> None:
    from frisket import search_index

    project = Project.create(tmp_path / "project-db-failure.frisket")
    try:
        with RetrievalStore(project).open():
            pass
        project.set_meta(PROJECT_SIDECAR_META_KEY, "stale-sidecar")
        failure = sqlite3.DatabaseError("project database write failed")
        failure.sqlite_errorcode = sqlite3.SQLITE_CORRUPT

        def fail_project_binding(_project) -> str:
            raise failure

        monkeypatch.setattr(
            search_index, "bind_retrieval_sidecar", fail_project_binding
        )
        with pytest.raises(
            sqlite3.DatabaseError, match="project database write failed"
        ):
            prepare_retrieval_on_open(project)

        assert (project.path / "project.retrieval.db").is_file()
        assert not list(project.path.glob("retrieval-unusable-*"))
    finally:
        project.close()


@pytest.mark.parametrize("version", ["1", "999"])
def test_unsupported_sidecar_schema_is_rejected_before_ddl(
    tmp_path, version: str
) -> None:
    path = tmp_path / "unknown-sidecar"
    path.mkdir()
    sidecar = path / "project.retrieval.db"
    db = sqlite3.connect(sidecar)
    db.execute("CREATE TABLE retrieval_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
    db.execute(
        "INSERT INTO retrieval_meta(key,value) VALUES ('schema_version',?)",
        (version,),
    )
    db.commit()
    db.close()

    store = RetrievalStore(path)
    with pytest.raises(
        UnsupportedRetrievalSchema, match="unsupported retrieval schema version"
    ):
        store.open()
    with pytest.raises(RuntimeError, match="retrieval store is not open"):
        _ = store.db

    db = sqlite3.connect(sidecar)
    try:
        assert {
            row[0]
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        } == {"retrieval_meta"}
    finally:
        db.close()


def test_sidecar_without_version_raises_typed_schema_error(tmp_path) -> None:
    path = tmp_path / "foreign-sidecar"
    path.mkdir()
    sidecar = path / "project.retrieval.db"
    db = sqlite3.connect(sidecar)
    db.execute("CREATE TABLE foreign_data(value TEXT)")
    db.commit()
    db.close()

    with pytest.raises(UnsupportedRetrievalSchema, match="schema version is missing"):
        RetrievalStore(path).open()


@pytest.mark.parametrize("filename", ["project.search.db", "project.embeddings.db"])
def test_retired_embedding_cache_is_ignored_and_search_rebuilds(tmp_path, filename):
    from frisket.search import drain_index, search_project

    path = tmp_path / "old-cache.frisket"
    with closing(Project.create(path)) as project:
        sheet = project.add_sheet("Notes")
        column = project.add_column(sheet, "body")
        project.add_rows(sheet, [{"body": "durable evidence"}], {"body": column})
    # A retired cache does not even need to be readable. Authoritative content
    # remains available and normal indexing builds the replacement on demand.
    (path / filename).write_bytes(b"obsolete cache")
    with closing(Project(path)) as project:
        assert project.get_values(sheet, column) == {1: "durable evidence"}
        drain_index(project)
        assert search_project(project, "evidence")
        assert (path / filename).read_bytes() == b"obsolete cache"


def test_empty_project_open_does_not_queue_search_rebuild(tmp_path):
    from frisket.search import drain_index

    path = tmp_path / "empty.frisket"
    with closing(Project.create(path)) as project:
        drain_index(project)
    (path / "project.retrieval.db").unlink()
    with closing(Project(path)) as project:
        assert not project.db.execute("SELECT 1 FROM search_dirty_scopes").fetchone()
