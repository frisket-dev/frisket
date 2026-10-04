"""Contentless FTS storage and authoritative snippet hydration."""

from __future__ import annotations

import hashlib
import sqlite3
import zlib

import pytest

import frisket.search as search_mod
from frisket.engine.store import Project
from frisket.engine.store.search_index_work import latest_revision
from frisket.search import (
    _sidecar,
    drain_index,
    fresh_sidecar,
    index_batch,
    search_cells_scoped,
    search_project,
)
from frisket.search_storage import (
    SearchStorageUnsupported,
    RECLAIM_PENDING_KEY,
    ensure_search_runtime,
    reclaim_is_pending,
    reclaim_search_storage,
    reset_keyword_storage,
)


LEGACY_SCHEMA = """
CREATE VIRTUAL TABLE cell_fts USING fts5(
  content, sheet_id UNINDEXED, row_id UNINDEXED, column_id UNINDEXED,
  column_name UNINDEXED
);
CREATE TABLE fts_state (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE search_cells (
  id INTEGER PRIMARY KEY,
  sheet_id INTEGER NOT NULL,
  column_id INTEGER NOT NULL,
  row_id INTEGER NOT NULL,
  source_hash TEXT NOT NULL,
  UNIQUE(column_id,row_id)
);
CREATE INDEX search_cells_sheet_column
  ON search_cells(sheet_id,column_id,row_id);
CREATE TABLE cell_vec (key TEXT PRIMARY KEY, vec BLOB NOT NULL);
"""

COMPRESSED_EXTERNAL_SCHEMA = """
CREATE TABLE fts_state (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE search_cells (
  id INTEGER PRIMARY KEY,
  sheet_id INTEGER NOT NULL,
  column_id INTEGER NOT NULL,
  row_id INTEGER NOT NULL,
  source_hash TEXT NOT NULL,
  UNIQUE(column_id,row_id)
);
CREATE INDEX search_cells_sheet_column
  ON search_cells(sheet_id,column_id,row_id);
CREATE TABLE search_content (
  id INTEGER PRIMARY KEY,
  compressed_content BLOB NOT NULL,
  column_name TEXT NOT NULL,
  FOREIGN KEY(id) REFERENCES search_cells(id) ON DELETE CASCADE
);
CREATE VIEW search_content_view AS
SELECT c.id,frisket_zlib_decode(c.compressed_content) AS content,
       s.sheet_id,s.row_id,s.column_id,c.column_name
FROM search_content c JOIN search_cells s ON s.id=c.id;
CREATE VIRTUAL TABLE cell_fts USING fts5(
  content,sheet_id UNINDEXED,row_id UNINDEXED,column_id UNINDEXED,
  column_name UNINDEXED,content='search_content_view',content_rowid='id'
);
CREATE TABLE cell_vec (key TEXT PRIMARY KEY, vec BLOB NOT NULL);
"""


def test_search_runtime_refuses_sqlite_before_contentless_delete(monkeypatch):
    monkeypatch.setattr(sqlite3, "sqlite_version_info", (3, 42, 0))
    monkeypatch.setattr(sqlite3, "sqlite_version", "3.42.0")

    with pytest.raises(SearchStorageUnsupported, match=r"3\.43.*3\.42\.0"):
        ensure_search_runtime()


def test_unsupported_runtime_refuses_before_dropping_legacy_index(monkeypatch):
    db = sqlite3.connect(":memory:")
    try:
        db.executescript(LEGACY_SCHEMA)
        monkeypatch.setattr(sqlite3, "sqlite_version_info", (3, 42, 0))
        monkeypatch.setattr(sqlite3, "sqlite_version", "3.42.0")

        with pytest.raises(SearchStorageUnsupported):
            reset_keyword_storage(db, content_version="next", reclaim=True)

        assert db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='cell_fts'"
        ).fetchone()
    finally:
        db.close()


def _legacy_sidecar(
    project: Project,
    sheet: int,
    column: int,
    rows: list[int],
    values: list[str],
) -> int:
    path = project.path / "project.search.db"
    db = sqlite3.connect(path)
    try:
        db.executescript(LEGACY_SCHEMA)
        for index_id, (row_id, value) in enumerate(
            zip(rows, values, strict=True), start=1
        ):
            db.execute(
                "INSERT INTO search_cells"
                "(id,sheet_id,column_id,row_id,source_hash) VALUES (?,?,?,?,?)",
                (
                    index_id,
                    sheet,
                    column,
                    row_id,
                    hashlib.sha256(value.encode()).hexdigest(),
                ),
            )
            db.execute(
                "INSERT INTO cell_fts"
                "(rowid,content,sheet_id,row_id,column_id,column_name) "
                "VALUES (?,?,?,?,?,?)",
                (index_id, value, sheet, row_id, column, "body"),
            )
        db.executemany(
            "INSERT INTO fts_state(key,value) VALUES (?,?)",
            (
                ("index_content_version", "3"),
                ("complete_revision", str(latest_revision(project.db))),
                ("indexed_at_op", str(project.op_cursor)),
            ),
        )
        db.execute("INSERT INTO cell_vec(key,vec) VALUES ('kept',X'01020304')")
        db.commit()
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        db.close()
    return path.stat().st_size


def _query_signature(db: sqlite3.Connection, query: str) -> list[tuple]:
    return [
        (int(row[0]), float(row[1]))
        for row in db.execute(
            "SELECT sc.row_id,rank FROM cell_fts "
            "JOIN search_cells sc ON sc.id=cell_fts.rowid "
            "WHERE cell_fts MATCH ? ORDER BY rank,cell_fts.rowid LIMIT 50",
            (query,),
        )
    ]


def test_current_compressed_sidecar_resets_without_loading_legacy_view(tmp_path):
    project = Project.create(tmp_path / "compressed-v4.frisket", name="migration")
    try:
        sheet = project.add_sheet("Documents")
        column = project.add_column(sheet, "body")
        [row] = project.add_rows(
            sheet, [{"body": "compressedneedle"}], {"body": column}
        )
        path = project.path / "project.search.db"
        db = sqlite3.connect(path)
        try:
            db.executescript(COMPRESSED_EXTERNAL_SCHEMA)
            db.execute(
                "INSERT INTO search_cells VALUES (1,?,?,?,?)",
                (
                    sheet,
                    column,
                    row,
                    hashlib.sha256(b"compressedneedle").hexdigest(),
                ),
            )
            db.execute(
                "INSERT INTO search_content VALUES (1,?,?)",
                (zlib.compress(b"compressedneedle"), "body"),
            )
            db.execute(
                "INSERT INTO cell_fts"
                "(rowid,content,sheet_id,row_id,column_id,column_name) "
                "VALUES (1,?,?,?,?,?)",
                ("compressedneedle", sheet, row, column, "body"),
            )
            db.executemany(
                "INSERT INTO fts_state VALUES (?,?)",
                (
                    ("index_content_version", "4"),
                    ("complete_revision", str(latest_revision(project.db))),
                ),
            )
            db.commit()
        finally:
            db.close()

        drain_index(project)

        assert search_project(project, "compressedneedle", rerank="off")[0][
            "row_id"
        ] == row
        rebuilt = sqlite3.connect(path)
        try:
            assert (
                rebuilt.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='search_content'"
                ).fetchone()
                is None
            )
        finally:
            rebuilt.close()
    finally:
        project.close()


def test_legacy_sidecar_migrates_to_contentless_and_reclaims_file(tmp_path):
    project = Project.create(tmp_path / "migration.frisket", name="migration")
    try:
        sheet = project.add_sheet("Documents")
        column = project.add_column(sheet, "body")
        values = [
            "alpha beta investigation "
            + ("ordinary filler " * 2_000)
            + f" uniquetail{i}"
            for i in range(40)
        ]
        rows = project.add_rows(
            sheet, [{"body": value} for value in values], {"body": column}
        )
        old_bytes = _legacy_sidecar(project, sheet, column, rows, values)
        old = sqlite3.connect(project.path / "project.search.db")
        try:
            before = {
                query: _query_signature(old, query)
                for query in ('"alpha beta"', "investigat*", "NEAR(alpha beta, 1)")
            }
        finally:
            old.close()

        assert drain_index(project, batch_size=11) == len(rows)
        path = project.path / "project.search.db"
        assert reclaim_is_pending(path)
        assert search_project(project, '"alpha beta"', rerank="off")
        assert reclaim_search_storage(path)

        db = fresh_sidecar(project)
        try:
            after = {
                query: _query_signature(db, query)
                for query in ('"alpha beta"', "investigat*", "NEAR(alpha beta, 1)")
            }
            assert after == before
            assert db.execute("SELECT vec FROM cell_vec WHERE key='kept'").fetchone()[
                0
            ] == bytes.fromhex("01020304")
            assert (
                db.execute(
                    "SELECT 1 FROM fts_state WHERE key=?", (RECLAIM_PENDING_KEY,)
                ).fetchone()
                is None
            )
            schema = db.execute(
                "SELECT sql FROM sqlite_master WHERE name='cell_fts'"
            ).fetchone()[0]
            assert "contentless_delete=1" in schema.replace(" ", "")
            assert (
                db.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='search_content'"
                ).fetchone()
                is None
            )
            assert (
                db.execute("SELECT content FROM cell_fts LIMIT 1").fetchone()[0] is None
            )
        finally:
            db.close()

        assert path.stat().st_size < old_bytes
        assert [
            hit["row_id"]
            for hit in search_project(project, '"alpha beta"', rerank="off")
        ] == rows
        scoped = search_cells_scoped(
            project, sheet, "uniquetail39", [], {(rows[-1], column)}, limit=10
        )
        assert [hit["row_id"] for hit in scoped] == [rows[-1]]
        assert "uniquetail39" in scoped[0]["snip"]
    finally:
        project.close()


def test_cancelled_schema_migration_rolls_back_keyword_reset(tmp_path, monkeypatch):
    project = Project.create(tmp_path / "cancel-migration.frisket", name="migration")
    try:
        sheet = project.add_sheet("Documents")
        column = project.add_column(sheet, "body")
        values = ["migration needle"]
        rows = project.add_rows(sheet, [{"body": values[0]}], {"body": column})
        _legacy_sidecar(project, sheet, column, rows, values)
        original = search_mod._raise_if_cancelled
        checks = 0

        def cancel_after_reset(_event):
            nonlocal checks
            checks += 1
            if checks == 2:
                raise InterruptedError("search was stopped")

        monkeypatch.setattr(search_mod, "_raise_if_cancelled", cancel_after_reset)
        with pytest.raises(InterruptedError, match="search was stopped"):
            index_batch(project, batch_size=1)
        raw = sqlite3.connect(project.path / "project.search.db")
        try:
            schema = raw.execute(
                "SELECT sql FROM sqlite_master WHERE name='cell_fts'"
            ).fetchone()[0]
            assert "contentless_delete" not in schema
            assert raw.execute("SELECT vec FROM cell_vec WHERE key='kept'").fetchone()[
                0
            ] == bytes.fromhex("01020304")
        finally:
            raw.close()

        monkeypatch.setattr(search_mod, "_raise_if_cancelled", original)
        drain_index(project)
        assert search_project(project, "needle", rerank="off")
    finally:
        project.close()


def test_busy_reclaim_leaves_completed_index_readable_and_retries(tmp_path):
    project = Project.create(tmp_path / "busy-reclaim.frisket", name="reclaim")
    try:
        sheet = project.add_sheet("Documents")
        column = project.add_column(sheet, "body")
        project.add_rows(sheet, [{"body": "searchable needle"}], {"body": column})
        drain_index(project)
        path = project.path / "project.search.db"
        writer = _sidecar(project)
        try:
            writer.execute("CREATE TABLE reclaim_scratch(value BLOB)")
            writer.execute("INSERT INTO reclaim_scratch VALUES (zeroblob(262144))")
            writer.execute("DROP TABLE reclaim_scratch")
            writer.execute(
                "INSERT INTO fts_state(key,value) VALUES (?,?)",
                (RECLAIM_PENDING_KEY, "test"),
            )
            writer.commit()
            assert writer.execute("PRAGMA freelist_count").fetchone()[0] > 0
            writer.execute("BEGIN IMMEDIATE")

            assert not reclaim_search_storage(path)
            assert reclaim_is_pending(path)
            assert search_project(project, "needle", rerank="off")
        finally:
            writer.rollback()
            writer.close()

        assert reclaim_search_storage(path)
        assert not reclaim_is_pending(path)
        assert search_project(project, "needle", rerank="off")
    finally:
        project.close()
