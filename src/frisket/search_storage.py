"""Physical storage helpers for the rebuildable keyword-search sidecar."""

from __future__ import annotations

from pathlib import Path
import sqlite3


RECLAIM_PENDING_KEY = "reclaim_pending"
MIN_CONTENTLESS_DELETE_SQLITE = (3, 43, 0)


class SearchStorageUnsupported(RuntimeError):
    """The linked SQLite cannot maintain Frisket's contentless FTS index."""


_contentless_delete_probed = False

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS fts_state (key TEXT PRIMARY KEY, value TEXT)",
    "CREATE TABLE IF NOT EXISTS search_cells ("
    "id INTEGER PRIMARY KEY,"
    "sheet_id INTEGER NOT NULL,"
    "column_id INTEGER NOT NULL,"
    "row_id INTEGER NOT NULL,"
    "column_name TEXT NOT NULL,"
    "source_hash TEXT NOT NULL,"
    "UNIQUE(column_id,row_id))",
    "CREATE INDEX IF NOT EXISTS search_cells_sheet_column "
    "ON search_cells(sheet_id,column_id,row_id)",
    "CREATE VIRTUAL TABLE IF NOT EXISTS cell_fts USING fts5("
    "content,content='',contentless_delete=1)",
    # The content-addressed semantic cache is independent of keyword schema
    # versions and deliberately survives a keyword reset.
    "CREATE TABLE IF NOT EXISTS cell_vec (key TEXT PRIMARY KEY, vec BLOB NOT NULL)",
)


def configure_search_connection(db: sqlite3.Connection) -> None:
    """Verify the linked SQLite can maintain contentless-delete FTS5."""
    del db
    ensure_search_runtime()


def ensure_search_runtime() -> None:
    """Probe the process SQLite without opening or modifying a project index."""

    global _contentless_delete_probed
    if sqlite3.sqlite_version_info < MIN_CONTENTLESS_DELETE_SQLITE:
        required = ".".join(str(part) for part in MIN_CONTENTLESS_DELETE_SQLITE)
        raise SearchStorageUnsupported(
            "Search requires SQLite "
            f"{required} or newer for FTS5 contentless-delete indexes; "
            f"this Python runtime links SQLite {sqlite3.sqlite_version}."
        )
    if _contentless_delete_probed:
        return
    db = sqlite3.connect(":memory:")
    try:
        db.execute(
            "CREATE VIRTUAL TABLE temp.frisket_contentless_delete_probe "
            "USING fts5(content,content='',contentless_delete=1)"
        )
        db.execute(
            "INSERT INTO temp.frisket_contentless_delete_probe(rowid,content) "
            "VALUES (1,'probe')"
        )
        db.execute("DELETE FROM temp.frisket_contentless_delete_probe WHERE rowid=1")
        if db.execute(
            "SELECT count(*) FROM temp.frisket_contentless_delete_probe "
            "WHERE frisket_contentless_delete_probe MATCH 'probe'"
        ).fetchone()[0]:
            raise sqlite3.OperationalError(
                "contentless-delete probe left deleted postings searchable"
            )
        db.execute("DROP TABLE temp.frisket_contentless_delete_probe")
    except sqlite3.DatabaseError as exc:
        try:
            db.execute("DROP TABLE IF EXISTS temp.frisket_contentless_delete_probe")
        except sqlite3.DatabaseError:
            pass
        raise SearchStorageUnsupported(
            "Search requires SQLite with FTS5 contentless-delete support "
            f"(SQLite 3.43 or newer); capability probe failed on "
            f"SQLite {sqlite3.sqlite_version}: {exc}"
        ) from exc
    finally:
        db.close()
    _contentless_delete_probed = True


def require_contentless_delete(db: sqlite3.Connection) -> None:
    """Compatibility wrapper for schema helpers with an existing connection."""

    del db
    ensure_search_runtime()


def ensure_search_schema(db: sqlite3.Connection) -> None:
    require_contentless_delete(db)
    for statement in _SCHEMA:
        db.execute(statement)


def keyword_schema_is_current(db: sqlite3.Connection) -> bool:
    row = db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='cell_fts'"
    ).fetchone()
    if row is None or row[0] is None:
        return False
    normalized = "".join(str(row[0]).lower().split())
    return "content=''" in normalized and "contentless_delete=1" in normalized


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
    # Probe before destructive DDL so an older runtime leaves a legacy index intact.
    require_contentless_delete(db)
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
        db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=0)
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
