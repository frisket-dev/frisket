"""Physical storage helpers for the rebuildable keyword-search sidecar."""

from __future__ import annotations

from pathlib import Path
import sqlite3
import zlib


RECLAIM_PENDING_KEY = "reclaim_pending"
_DECODE_FUNCTION = "frisket_zlib_decode"

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS fts_state (key TEXT PRIMARY KEY, value TEXT)",
    "CREATE TABLE IF NOT EXISTS search_cells ("
    "id INTEGER PRIMARY KEY,"
    "sheet_id INTEGER NOT NULL,"
    "column_id INTEGER NOT NULL,"
    "row_id INTEGER NOT NULL,"
    "source_hash TEXT NOT NULL,"
    "UNIQUE(column_id,row_id))",
    "CREATE INDEX IF NOT EXISTS search_cells_sheet_column "
    "ON search_cells(sheet_id,column_id,row_id)",
    "CREATE TABLE IF NOT EXISTS search_content ("
    "id INTEGER PRIMARY KEY,"
    "compressed_content BLOB NOT NULL,"
    "column_name TEXT NOT NULL,"
    "FOREIGN KEY(id) REFERENCES search_cells(id) ON DELETE CASCADE)",
    "CREATE VIEW IF NOT EXISTS search_content_view AS "
    "SELECT c.id,frisket_zlib_decode(c.compressed_content) AS content,"
    "s.sheet_id,s.row_id,s.column_id,c.column_name "
    "FROM search_content AS c JOIN search_cells AS s ON s.id=c.id",
    "CREATE VIRTUAL TABLE IF NOT EXISTS cell_fts USING fts5("
    "content,sheet_id UNINDEXED,row_id UNINDEXED,column_id UNINDEXED,"
    "column_name UNINDEXED,content='search_content_view',content_rowid='id')",
    # The content-addressed semantic cache is independent of keyword schema
    # versions and deliberately survives a keyword reset.
    "CREATE TABLE IF NOT EXISTS cell_vec (key TEXT PRIMARY KEY, vec BLOB NOT NULL)",
)


def encode_search_content(value: str) -> bytes:
    return zlib.compress(value.encode("utf-8"), level=6)


def _decode_search_content(value: bytes) -> str:
    return zlib.decompress(value).decode("utf-8")


def configure_search_connection(db: sqlite3.Connection) -> None:
    """Install connection-local functions required by the external-content view."""
    db.create_function(
        _DECODE_FUNCTION,
        1,
        _decode_search_content,
        deterministic=True,
    )


def ensure_search_schema(db: sqlite3.Connection) -> None:
    for statement in _SCHEMA:
        db.execute(statement)


def keyword_schema_is_current(db: sqlite3.Connection) -> bool:
    row = db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='cell_fts'"
    ).fetchone()
    if row is None or row[0] is None:
        return False
    normalized = "".join(str(row[0]).lower().split())
    return "content='search_content_view'" in normalized


def keyword_storage_has_rows(db: sqlite3.Connection) -> bool:
    try:
        if db.execute("SELECT 1 FROM search_cells LIMIT 1").fetchone() is not None:
            return True
        return (
            db.execute("SELECT 1 FROM cell_fts_docsize LIMIT 1").fetchone() is not None
        )
    except sqlite3.DatabaseError:
        return False


def reset_keyword_storage(
    db: sqlite3.Connection, *, content_version: str, reclaim: bool
) -> None:
    """Replace keyword objects transactionally while preserving ``cell_vec``."""
    db.execute("DROP TABLE IF EXISTS cell_fts")
    db.execute("DROP VIEW IF EXISTS search_content_view")
    db.execute("DROP TABLE IF EXISTS search_content")
    db.execute("DROP TABLE IF EXISTS search_cells")
    db.execute("DROP TABLE IF EXISTS fts_state")
    ensure_search_schema(db)
    db.execute(
        "INSERT INTO fts_state(key,value) VALUES ('index_content_version',?)",
        (content_version,),
    )
    if reclaim:
        db.execute(
            "INSERT INTO fts_state(key,value) VALUES (?,?)",
            (RECLAIM_PENDING_KEY, content_version),
        )


def reclaim_is_pending(path: Path) -> bool:
    if not path.is_file():
        return False
    db = None
    try:
        db = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=0)
        configure_search_connection(db)
        return (
            db.execute(
                "SELECT 1 FROM fts_state WHERE key=?", (RECLAIM_PENDING_KEY,)
            ).fetchone()
            is not None
        )
    except sqlite3.DatabaseError as exc:
        if _retryable_reclaim_error(exc):
            # A schema/writer lock can hide the marker briefly.  Treat that as
            # pending so the durable queue checks again instead of losing work.
            return True
        return False
    finally:
        if db is not None:
            db.close()


def _retryable_reclaim_error(exc: sqlite3.Error) -> bool:
    primary = getattr(exc, "sqlite_errorcode", 0) & 0xFF
    return primary in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED, sqlite3.SQLITE_FULL}


def reclaim_search_storage(path: Path) -> bool:
    """Finish a one-time in-place migration reclaim, retrying busy/full later."""
    if not reclaim_is_pending(path):
        return True
    db = sqlite3.connect(path, timeout=0, isolation_level=None)
    configure_search_connection(db)
    try:
        try:
            # If VACUUM succeeded but checkpointing was busy, its retry sees no
            # freelist and does not rewrite the completed index a second time.
            if int(db.execute("PRAGMA freelist_count").fetchone()[0]) > 0:
                db.execute("VACUUM")
            checkpoint = db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if checkpoint is not None and int(checkpoint[0]) != 0:
                return False
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM fts_state WHERE key=?", (RECLAIM_PENDING_KEY,))
            db.commit()
            return True
        except sqlite3.Error as exc:
            if db.in_transaction:
                db.rollback()
            if _retryable_reclaim_error(exc):
                return False
            raise
    finally:
        db.close()
