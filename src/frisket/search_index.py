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


_SQLITE_ROW_ID_CHUNK = 900


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
        db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=0)
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
        "SELECT id,row_id,sheet_id,column_name,source_hash FROM search_cells "
        "WHERE column_id=? AND row_id>? AND row_id<=? ORDER BY row_id LIMIT ?",
        params,
    ).fetchall()
    ids = sorted({int(row[0]) for row in live} | {int(row["row_id"]) for row in old})[
        :limit
    ]
    return (
        ids,
        {int(row[0]) for row in live},
        {int(row["row_id"]): row for row in old},
    )


def _cell_sizes(source, column: int, rows: list[int]) -> dict[int, int]:
    sizes: dict[int, int] = {}
    for start in range(0, len(rows), _SQLITE_ROW_ID_CHUNK):
        chunk = rows[start : start + _SQLITE_ROW_ID_CHUNK]
        placeholders = ",".join("?" for _ in chunk)
        found = source.execute(
            "SELECT row_id,value_kind,"
            "COALESCE(length(CAST(value AS BLOB)),0) AS stored_bytes,"
            "CASE WHEN value_kind='boolean' THEN value END AS boolean_value,"
            "CASE WHEN value_kind='real' THEN value END AS real_value "
            "FROM current_cell_values WHERE column_id=? "
            f"AND row_id IN ({placeholders})",
            (column, *chunk),
        ).fetchall()
        for cell in found:
            kind = cell["value_kind"]
            if kind == "null":
                size = 0
            elif kind == "boolean":
                # SQLite stores booleans as 0/1, while searchable_text receives
                # native False/True.
                size = len(str(bool(cell["boolean_value"])))
            elif kind == "real":
                size = len(str(cell["real_value"]).encode("utf-8"))
            else:
                # Stored text length is its UTF-8 byte length. JSON/link text
                # remains a conservative bound when decoding removes escapes.
                size = int(cell["stored_bytes"])
            sizes[int(cell["row_id"])] = size
    return sizes


def _column_descriptor(source, column: int):
    return source.execute(
        "SELECT c.sheet_id,c.name FROM columns c JOIN sheets s ON s.id=c.sheet_id "
        "WHERE c.id=? AND c.hidden=0 AND s.hidden=0 "
        "AND c.type IN ('text','category','json','link')",
        (column,),
    ).fetchone()


def _replace_cell(index, column: int, row_id: int, value, descriptor, old) -> None:
    text = searchable_text(value) if descriptor is not None else None
    digest = source_hash(text) if text is not None else None
    if (
        old is not None
        and descriptor is not None
        and digest is not None
        and int(old["sheet_id"]) == int(descriptor["sheet_id"])
        and old["column_name"] == descriptor["name"]
        and old["source_hash"] == digest
    ):
        return
    if old is not None:
        # contentless-delete removes postings without retaining or replaying text.
        index.execute("DELETE FROM cell_fts WHERE rowid=?", (old["id"],))
        index.execute("DELETE FROM search_cells WHERE id=?", (old["id"],))
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
            digest,
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
                ids, live_ids, old_by_row = _column_page(
                    snapshot.db,
                    index,
                    scope,
                    column,
                    row,
                    remaining,
                    searchable=descriptor is not None,
                )
                live_page = [row_id for row_id in ids if row_id in live_ids]
                sizes = _cell_sizes(snapshot.db, column, live_page)
                admitted = []
                for row_id in ids:
                    _raise_if_cancelled(cancel_event)
                    size = sizes.get(row_id, 0)
                    # One oversized cell can always make progress. Never load
                    # a second cell that would exceed the quantum byte budget.
                    if processed_bytes and processed_bytes + size > max_bytes:
                        byte_limit_reached = True
                        break
                    admitted.append((row_id, size))
                    processed_bytes += size
                admitted_live = [
                    row_id for row_id, _size in admitted if row_id in live_ids
                ]
                values = (
                    snapshot.get_values(
                        int(descriptor["sheet_id"]), column, row_ids=admitted_live
                    )
                    if admitted_live
                    else {}
                )
                for row_id, _size in admitted:
                    _raise_if_cancelled(cancel_event)
                    _replace_cell(
                        index,
                        column,
                        row_id,
                        values.get(row_id) if row_id in live_ids else None,
                        descriptor,
                        old_by_row.get(row_id),
                    )
                    processed += 1
                    row = row_id
                del values
                if processed_bytes >= max_bytes:
                    byte_limit_reached = True
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
