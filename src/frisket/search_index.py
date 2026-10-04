"""Bounded maintenance of the disposable keyword index."""

from __future__ import annotations

from dataclasses import dataclass
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
from frisket.search_hydration import searchable_text, source_hash
from frisket.search_storage import (
    keyword_schema_is_current,
    keyword_storage_has_rows,
    reset_keyword_storage,
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
    processed_bytes: int = 0


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
    db = None
    try:
        db = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=0)
        db.execute("BEGIN")
        return not index_is_complete(db, project)
    except sqlite3.DatabaseError:
        return True
    finally:
        if db is not None:
            db.close()


def _next_column(source, index, scope, after: int) -> int | None:
    if scope.column_id is not None:
        return scope.column_id if scope.column_id > after else None
    params = [after]
    where = (
        "c.id>? AND c.hidden=0 AND s.hidden=0 "
        "AND c.type IN ('text','category','json','link')"
    )
    indexed_where = "column_id>?"
    if scope.sheet_id is not None:
        where += " AND c.sheet_id=?"
        indexed_where += " AND sheet_id=?"
        params.append(scope.sheet_id)
    live = source.execute(
        "SELECT c.id FROM columns c JOIN sheets s ON s.id=c.sheet_id "
        f"WHERE {where} ORDER BY c.id LIMIT 1",
        params,
    ).fetchone()
    old = index.execute(
        f"SELECT column_id FROM search_cells WHERE {indexed_where} "
        "ORDER BY column_id LIMIT 1",
        params,
    ).fetchone()
    candidates = [int(row[0]) for row in (live, old) if row is not None]
    return min(candidates) if candidates else None


def _column_page(
    source, index, scope, column: int, after: int, limit: int, *, searchable: bool
):
    lower = max(after, (scope.row_id_start or 1) - 1)
    upper = scope.row_id_end or 9_223_372_036_854_775_807
    params = (column, lower, upper, limit)
    live = (
        source.execute(
            # This column-first index covers the identity fields without loading
            # potentially large values merely to discover the next candidate IDs.
            "SELECT cc.row_id FROM current_cells cc INDEXED BY idx_current_cells_column_row "
            "WHERE cc.column_id=? AND cc.row_id>? AND cc.row_id<=? "
            "ORDER BY cc.row_id LIMIT ?",
            params,
        ).fetchall()
        if searchable
        else []
    )
    old = index.execute(
        "SELECT row_id FROM search_cells "
        "WHERE column_id=? AND row_id>? AND row_id<=? ORDER BY row_id LIMIT ?",
        params,
    ).fetchall()
    ids = sorted({int(row[0]) for row in live} | {int(row[0]) for row in old})[:limit]
    return ids, {int(row[0]) for row in live}


def _current_value_relation(source) -> str:
    view = source.execute(
        "SELECT 1 FROM sqlite_master WHERE type='view' AND name='current_cell_values'"
    ).fetchone()
    return "current_cell_values" if view is not None else "current_cells"


def _cell_size(source, relation: str, column: int, row: int) -> int:
    # ``relation`` is selected from the two constants above, never caller input.
    found = source.execute(
        f"SELECT length(CAST(value AS BLOB)) FROM {relation} "
        "WHERE column_id=? AND row_id=?",
        (column, row),
    ).fetchone()
    return int(found[0] or 0) if found is not None else 0


def _read_cell(snapshot, sheet_id: int, column: int, row: int):
    return snapshot.get_values(sheet_id, column, row_ids=[row]).get(row)


def _column_descriptor(source, column: int):
    return source.execute(
        "SELECT c.sheet_id,c.name FROM columns c JOIN sheets s ON s.id=c.sheet_id "
        "WHERE c.id=? AND c.hidden=0 AND s.hidden=0 "
        "AND c.type IN ('text','category','json','link')",
        (column,),
    ).fetchone()


def _replace_cell(index, column: int, row_id: int, value, descriptor) -> None:
    old = index.execute(
        "SELECT id FROM search_cells WHERE column_id=? AND row_id=?",
        (column, row_id),
    ).fetchone()
    if old is not None:
        # contentless-delete removes postings without retaining or replaying text.
        index.execute("DELETE FROM cell_fts WHERE rowid=?", (old[0],))
        index.execute("DELETE FROM search_cells WHERE id=?", (old[0],))
    if descriptor is None:
        return
    text = searchable_text(value)
    if text is None:
        return
    identity = index.execute(
        "INSERT INTO search_cells"
        "(sheet_id,column_id,row_id,column_name,source_hash) VALUES (?,?,?,?,?)",
        (
            descriptor["sheet_id"],
            column,
            row_id,
            descriptor["name"],
            source_hash(text),
        ),
    ).lastrowid
    index.execute(
        "INSERT INTO cell_fts(rowid,content) VALUES (?,?)",
        (identity, text),
    )


def index_batch(
    project: Project,
    *,
    batch_size: int = 500,
    max_bytes: int = 4 * 1024 * 1024,
    cancel_event: threading.Event | None = None,
) -> IndexProgress:
    """Reconcile a bounded page; acknowledge only after durable index commit."""
    from frisket.search import FTS_INDEX_CONTENT_VERSION, _raise_if_cancelled, _sidecar

    if batch_size < 1 or max_bytes < 1:
        raise ValueError("batch_size and max_bytes must be positive")
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
            reclaim = keyword_storage_has_rows(index)
            if not keyword_schema_is_current(index) or reclaim:
                reset_keyword_storage(
                    index,
                    content_version=FTS_INDEX_CONTENT_VERSION,
                    reclaim=reclaim,
                )
            else:
                _set_state(index, "index_content_version", FTS_INDEX_CONTENT_VERSION)
        # Acquire the source snapshot AFTER the sidecar writer lock. A second
        # indexer may replay a page, but cannot publish an older snapshot over it.
        snapshot = project.read_snapshot()
        value_relation = _current_value_relation(snapshot.db)
        revision = latest_revision(snapshot.db)
        # Every scope consumes at least one unit. The extra record proves
        # whether the completed prefix really exhausted the worklist.
        scopes = read_dirty_scopes(snapshot.db, limit=batch_size + 1)
        processed = processed_bytes = 0
        checkpoints = []
        if scopes:
            index.execute("DELETE FROM fts_state WHERE key='complete_revision'")
        for scope in scopes:
            if processed >= batch_size or processed_bytes >= max_bytes:
                break
            column, row = json.loads(scope.scan_cursor)
            if column == 0:
                column = _next_column(snapshot.db, index, scope, 0)
            before_scope = processed
            byte_limit_reached = False
            while column is not None and processed < batch_size:
                _raise_if_cancelled(cancel_event)
                descriptor = _column_descriptor(snapshot.db, column)
                remaining = batch_size - processed
                ids, live_ids = _column_page(
                    snapshot.db,
                    index,
                    scope,
                    column,
                    row,
                    remaining,
                    searchable=descriptor is not None,
                )
                for row_id in ids:
                    _raise_if_cancelled(cancel_event)
                    size = (
                        _cell_size(snapshot.db, value_relation, column, row_id)
                        if row_id in live_ids
                        else 0
                    )
                    # One oversized cell can always make progress. Never load
                    # a second cell that would exceed the quantum byte budget.
                    if processed_bytes and processed_bytes + size > max_bytes:
                        byte_limit_reached = True
                        break
                    value = (
                        _read_cell(
                            snapshot, int(descriptor["sheet_id"]), column, row_id
                        )
                        if row_id in live_ids
                        else None
                    )
                    _replace_cell(index, column, row_id, value, descriptor)
                    del value
                    processed += 1
                    processed_bytes += size
                    row = row_id
                    if processed_bytes >= max_bytes:
                        byte_limit_reached = True
                        break
                if byte_limit_reached:
                    break
                if len(ids) < remaining:
                    # Both identity scans are exhausted for this column.
                    if not ids:
                        processed += 1
                    column = _next_column(snapshot.db, index, scope, column)
                    row = 0
            if processed == before_scope and column is None:
                processed += 1
            exhausted = column is None
            cursor = None if exhausted else json.dumps([column, row])
            checkpoints.append((scope, cursor))
            if not exhausted:
                break
        complete = len(checkpoints) == len(scopes) and all(
            cursor is None for _, cursor in checkpoints
        )
        if complete:
            _set_state(index, "complete_revision", revision)
            _set_state(index, "indexed_at_op", snapshot.op_cursor)
        index.commit()
        snapshot.close()
        snapshot = None
        if checkpoints:
            project.db.execute("BEGIN IMMEDIATE")
            try:
                for scope, cursor in checkpoints:
                    if cursor is None:
                        ack_dirty_scope(
                            project.db,
                            scope_id=scope.id,
                            expected_cursor=scope.scan_cursor,
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
        return IndexProgress(processed, not complete, complete, processed_bytes)
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
