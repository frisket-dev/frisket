"""Bounded maintenance of the disposable keyword index."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import sqlite3
import threading

from frisket.engine.store import Project
from frisket.engine.store.search_index_work import (
    ack_dirty_scope,
    advance_dirty_scope,
    enqueue_dirty_scope,
    latest_revision,
    read_dirty_scopes,
)


class SearchIndexNotReady(RuntimeError):
    """The index does not describe the caller's complete source snapshot."""

    code = "search_index_not_ready"

    def __init__(self) -> None:
        super().__init__("Search is still indexing; retry when indexing completes.")


@dataclass(frozen=True)
class IndexProgress:
    processed: int
    pending: bool
    complete: bool


def source_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _state(db: sqlite3.Connection, key: str) -> str | None:
    row = db.execute("SELECT value FROM fts_state WHERE key=?", (key,)).fetchone()
    return None if row is None else str(row[0])


def _set_state(db: sqlite3.Connection, key: str, value: object) -> None:
    db.execute(
        "INSERT INTO fts_state(key,value) VALUES (?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


def index_is_complete(db: sqlite3.Connection, project) -> bool:
    from frisket.search import FTS_INDEX_CONTENT_VERSION

    return _state(db, "index_content_version") == FTS_INDEX_CONTENT_VERSION and _state(
        db, "complete_revision"
    ) == str(latest_revision(project.db))


def index_needs_work(project: Project) -> bool:
    """Cheap scheduling hint without creating or modifying the sidecar."""
    path = project.path / "project.search.db"
    if not path.is_file():
        return True
    db = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=0)
    try:
        db.execute("BEGIN")
        return not index_is_complete(db, project)
    except sqlite3.DatabaseError:
        return True
    finally:
        db.close()


def _next_column(source, index, scope, after: int) -> int | None:
    if scope.column_id is not None:
        return scope.column_id if scope.column_id > after else None
    params = [after]
    where = "id>?"
    indexed_where = "column_id>?"
    if scope.sheet_id is not None:
        where += " AND sheet_id=?"
        indexed_where += " AND sheet_id=?"
        params.append(scope.sheet_id)
    live = source.execute(
        f"SELECT id FROM columns WHERE {where} ORDER BY id LIMIT 1", params
    ).fetchone()
    old = index.execute(
        f"SELECT column_id FROM search_cells WHERE {indexed_where} "
        "ORDER BY column_id LIMIT 1",
        params,
    ).fetchone()
    candidates = [int(row[0]) for row in (live, old) if row is not None]
    return min(candidates) if candidates else None


def _column_page(source, index, scope, column: int, after: int, limit: int):
    lower = max(after, (scope.row_id_start or 1) - 1)
    upper = scope.row_id_end or 9_223_372_036_854_775_807
    params = (column, lower, upper, limit)
    live = source.execute(
        "SELECT cc.row_id,cc.value,cc.validity,r.hidden FROM current_cells cc "
        "JOIN rows r ON r.id=cc.row_id "
        "WHERE cc.column_id=? AND cc.row_id>? AND cc.row_id<=? "
        "ORDER BY cc.row_id LIMIT ?",
        params,
    ).fetchall()
    old = index.execute(
        "SELECT row_id FROM search_cells "
        "WHERE column_id=? AND row_id>? AND row_id<=? ORDER BY row_id LIMIT ?",
        params,
    ).fetchall()
    ids = sorted({int(row[0]) for row in live} | {int(row[0]) for row in old})[:limit]
    return ids, {int(row[0]): row for row in live}


def _column_descriptor(source, column: int):
    return source.execute(
        "SELECT c.sheet_id,c.name FROM columns c JOIN sheets s ON s.id=c.sheet_id "
        "WHERE c.id=? AND c.hidden=0 AND s.hidden=0 "
        "AND c.type IN ('text','category','json','link')",
        (column,),
    ).fetchone()


def _replace_cell(index, column: int, row_id: int, cell, descriptor) -> None:
    old = index.execute(
        "SELECT id FROM search_cells WHERE column_id=? AND row_id=?",
        (column, row_id),
    ).fetchone()
    if old is not None:
        index.execute("DELETE FROM cell_fts WHERE rowid=?", (old[0],))
        index.execute("DELETE FROM search_cells WHERE id=?", (old[0],))
    if (
        cell is None
        or cell["validity"] != "valid"
        or cell["value"] is None
        or cell["hidden"]
        or descriptor is None
    ):
        return
    value = json.loads(cell["value"])
    if value is None or not str(value).strip():
        return
    identity = index.execute(
        "INSERT INTO search_cells(sheet_id,column_id,row_id,source_hash) "
        "VALUES (?,?,?,?)",
        (descriptor["sheet_id"], column, row_id, source_hash(str(value))),
    ).lastrowid
    index.execute(
        "INSERT INTO cell_fts(rowid,content,sheet_id,row_id,column_id,column_name) "
        "VALUES (?,?,?,?,?,?)",
        (
            identity,
            str(value),
            descriptor["sheet_id"],
            row_id,
            column,
            descriptor["name"],
        ),
    )


def index_batch(
    project: Project,
    *,
    batch_size: int = 500,
    cancel_event: threading.Event | None = None,
) -> IndexProgress:
    """Reconcile a bounded page; acknowledge only after durable index commit."""
    from frisket.search import FTS_INDEX_CONTENT_VERSION, _raise_if_cancelled, _sidecar

    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    if project.db.in_transaction:
        raise RuntimeError("index maintenance requires its own source transaction")
    index = _sidecar(project)
    snapshot = None
    try:
        index.execute("BEGIN IMMEDIATE")
        _raise_if_cancelled(cancel_event)
        if _state(index, "index_content_version") != FTS_INDEX_CONTENT_VERSION:
            # This is an explicit maintenance operation, never a reader side effect.
            project.db.execute("BEGIN IMMEDIATE")
            try:
                enqueue_dirty_scope(project.db)
                project.db.commit()
            except BaseException:
                project.db.rollback()
                raise
            index.execute("DELETE FROM cell_fts")
            index.execute("DELETE FROM search_cells")
            index.execute("DELETE FROM fts_state")
            _set_state(index, "index_content_version", FTS_INDEX_CONTENT_VERSION)
        # Acquire the source snapshot AFTER the sidecar writer lock. A second
        # indexer may replay a page, but cannot publish an older snapshot over it.
        snapshot = project.read_snapshot()
        revision = latest_revision(snapshot.db)
        scopes = read_dirty_scopes(snapshot.db, limit=2)
        processed = 0
        exhausted = True
        scope = scopes[0] if scopes else None
        cursor = None
        if scope is not None:
            index.execute("DELETE FROM fts_state WHERE key='complete_revision'")
            column, row = json.loads(scope.scan_cursor)
            if column == 0:
                column = _next_column(snapshot.db, index, scope, 0)
            while column is not None and processed < batch_size:
                _raise_if_cancelled(cancel_event)
                ids, cells = _column_page(
                    snapshot.db, index, scope, column, row, batch_size - processed
                )
                descriptor = _column_descriptor(snapshot.db, column)
                for row_id in ids:
                    _raise_if_cancelled(cancel_event)
                    _replace_cell(index, column, row_id, cells.get(row_id), descriptor)
                    processed += 1
                    row = row_id
                if ids:
                    cursor = json.dumps([column, row])
                    exhausted = False
                else:
                    # Empty/deleted columns also consume bounded maintenance
                    # work, so a schema with many empty columns cannot monopolize a job.
                    processed += 1
                    column = _next_column(snapshot.db, index, scope, column)
                    row = 0
                    exhausted = column is None
                    if column is not None:
                        cursor = json.dumps([column, 0])
            if column is None:
                exhausted = True
        complete = exhausted and len(scopes) <= 1
        if complete:
            _set_state(index, "complete_revision", revision)
            _set_state(index, "indexed_at_op", snapshot.op_cursor)
        index.commit()
        snapshot.close()
        snapshot = None
        if scope is not None:
            project.db.execute("BEGIN IMMEDIATE")
            try:
                if exhausted:
                    ack_dirty_scope(
                        project.db, scope_id=scope.id, expected_cursor=scope.scan_cursor
                    )
                else:
                    advance_dirty_scope(
                        project.db,
                        scope_id=scope.id,
                        expected_cursor=scope.scan_cursor,
                        scan_cursor=cursor,
                    )
                project.db.commit()
            except BaseException:
                project.db.rollback()
                raise
        complete = complete and latest_revision(project.db) == revision
        return IndexProgress(processed, not complete, complete)
    except BaseException:
        index.rollback()
        raise
    finally:
        if snapshot is not None:
            snapshot.close()
        index.close()


def drain_index(
    project: Project,
    *,
    batch_size: int = 500,
    cancel_event: threading.Event | None = None,
) -> int:
    """Explicit synchronous maintenance for CLI/library use without a worker."""
    while not index_batch(
        project, batch_size=batch_size, cancel_event=cancel_event
    ).complete:
        pass
    from frisket.search import _sidecar

    db = _sidecar(project)
    try:
        return int(db.execute("SELECT COUNT(*) FROM search_cells").fetchone()[0])
    finally:
        db.close()
