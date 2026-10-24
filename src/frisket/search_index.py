"""Bounded maintenance of the unified retrieval sidecar."""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass

from frisket.engine.store import Project
from frisket.engine.store.search_index_work import (
    ack_dirty_scope,
    advance_dirty_scope,
    enqueue_dirty_scope,
    latest_revision,
    read_dirty_scopes,
)
from frisket.retrieval.preparation import (
    PreparationPolicy,
    dispatch_resolved_blob_source,
    dispatch_resolved_evidence_source,
    dispatch_resolved_text_source,
    retire_cell_binding,
)
from frisket.retrieval.preparation_policy import read_preparation_policy_db
from frisket.retrieval.source_adapter import (
    BLOB_SOURCE_COLUMN_TYPES,
    RETRIEVAL_SOURCE_COLUMN_TYPES,
    TEXT_SOURCE_COLUMN_TYPES,
    ResolvedCellBlobSource,
    ResolvedCellOcrTextSource,
    ResolvedCellTextSource,
    ResolvedCellTranscriptSource,
    resolve_cell_blob_source,
    resolve_cell_ocr_text_source,
    resolve_cell_text_sources,
    resolve_cell_transcript_source,
)
from frisket.retrieval.sources import cell_source_binding_id
from frisket.retrieval.store import (
    PROJECT_SIDECAR_META_KEY as RETRIEVAL_SIDECAR_META_KEY,
    RETRIEVAL_DB_NAME,
    RetrievalStore,
    read_sidecar_id,
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


def _raise_if_cancelled(cancel_event: threading.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise InterruptedError("search was stopped")


def index_needs_work(project: Project) -> bool:
    """Return a read-only scheduling hint for lexical source maintenance."""

    path = project.path / RETRIEVAL_DB_NAME
    # Fresh bundles carry a bootstrap dirty scope, but have nothing to index.
    # An existing sidecar still needs maintenance after the last cell is removed.
    if (
        not path.exists()
        and project.db.execute("SELECT 1 FROM current_cells LIMIT 1").fetchone() is None
    ):
        return False
    if read_dirty_scopes(project.db, limit=1):
        return True
    sidecar_id = read_sidecar_id(path)
    if sidecar_id is None or project.get_meta(RETRIEVAL_SIDECAR_META_KEY) != sidecar_id:
        return True
    db = None
    try:
        db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=0)
        return (
            db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sources'"
            ).fetchone()
            is None
        )
    except sqlite3.DatabaseError:
        return True
    finally:
        if db is not None:
            db.close()


def analyze_retrieval_store(project: Project) -> None:
    """Refresh bounded planner statistics after a complete maintenance pass."""

    with RetrievalStore(project).open() as retrieval:
        # SQLite 3.45's PRAGMA optimize does not populate enough sidecar
        # statistics after a fresh bulk build. The bounded sample teaches
        # semantic joins to start from scoped bindings instead of the whole
        # vector space without a full-table analysis.
        retrieval.db.execute("PRAGMA analysis_limit=1000")
        retrieval.db.execute("ANALYZE")


def bind_retrieval_sidecar(project: Project) -> str:
    """Bind the project to one sidecar incarnation and schedule its rebuild."""

    with RetrievalStore(project).open() as retrieval:
        sidecar_id = retrieval.sidecar_id()
    with project.transaction_scope():
        if project.get_meta(RETRIEVAL_SIDECAR_META_KEY) != sidecar_id:
            enqueue_dirty_scope(project.db)
            project.set_meta(RETRIEVAL_SIDECAR_META_KEY, sidecar_id, commit=False)
    return sidecar_id


def _next_column(source, retrieval: RetrievalStore, scope, after: int) -> int | None:
    if scope.column_id is not None:
        return scope.column_id if scope.column_id > after else None
    live_params: list[int | str] = [after, *RETRIEVAL_SOURCE_COLUMN_TYPES]
    indexed_params: list[int] = [after]
    type_placeholders = ",".join("?" for _ in RETRIEVAL_SOURCE_COLUMN_TYPES)
    where = f"c.id>? AND c.hidden=0 AND s.hidden=0 AND c.type IN ({type_placeholders})"
    indexed_where = "column_id>?"
    if scope.sheet_id is not None:
        where += " AND c.sheet_id=?"
        indexed_where += " AND sheet_id=?"
        live_params.append(scope.sheet_id)
        indexed_params.append(scope.sheet_id)
    live = source.execute(
        "SELECT c.id FROM columns c JOIN sheets s ON s.id=c.sheet_id "
        f"WHERE {where} ORDER BY c.id LIMIT 1",
        live_params,
    ).fetchone()
    old = retrieval.db.execute(
        f"SELECT column_id FROM bindings WHERE {indexed_where} "
        "ORDER BY column_id LIMIT 1",
        indexed_params,
    ).fetchone()
    candidates = [int(row[0]) for row in (live, old) if row is not None]
    return min(candidates) if candidates else None


def _column_page(
    source,
    retrieval: RetrievalStore,
    scope,
    column: int,
    after: int,
    limit: int,
    *,
    searchable: bool,
):
    lower = max(after, (scope.row_id_start or 1) - 1)
    upper = scope.row_id_end or 9_223_372_036_854_775_807
    params = (column, lower, upper, limit)
    live = (
        source.execute(
            "SELECT cc.row_id FROM current_cells cc "
            "INDEXED BY idx_current_cells_column_row "
            "WHERE cc.column_id=? AND cc.row_id>? AND cc.row_id<=? "
            "ORDER BY cc.row_id LIMIT ?",
            params,
        ).fetchall()
        if searchable
        else []
    )
    old = retrieval.db.execute(
        "SELECT row_id,min(sheet_id) AS sheet_id FROM bindings "
        "WHERE column_id=? AND row_id>? AND row_id<=? "
        "GROUP BY row_id ORDER BY row_id LIMIT ?",
        params,
    ).fetchall()
    ids = sorted(
        {int(row["row_id"]) for row in live} | {int(row["row_id"]) for row in old}
    )[:limit]
    return (
        ids,
        {int(row["row_id"]) for row in live},
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
                size = len(str(bool(cell["boolean_value"])))
            elif kind == "real":
                size = len(str(cell["real_value"]).encode("utf-8"))
            else:
                size = int(cell["stored_bytes"])
            sizes[int(cell["row_id"])] = size
    return sizes


def _column_descriptor(source, column: int):
    placeholders = ",".join("?" for _ in RETRIEVAL_SOURCE_COLUMN_TYPES)
    return source.execute(
        "SELECT c.sheet_id,c.name,c.type FROM columns c "
        "JOIN sheets s ON s.id=c.sheet_id "
        "WHERE c.id=? AND c.hidden=0 AND s.hidden=0 "
        f"AND c.type IN ({placeholders})",
        (column, *RETRIEVAL_SOURCE_COLUMN_TYPES),
    ).fetchone()


def _replace_cell(
    column: int,
    row_id: int,
    resolved: tuple[
        ResolvedCellTextSource
        | ResolvedCellTranscriptSource
        | ResolvedCellOcrTextSource
        | ResolvedCellBlobSource,
        ...,
    ],
    descriptor,
    old,
    retrieval: RetrievalStore,
    policy: PreparationPolicy,
) -> None:
    if descriptor is not None and resolved:
        for source in resolved:
            if isinstance(source, ResolvedCellTextSource):
                dispatch_resolved_text_source(
                    retrieval,
                    source,
                    recipe=policy.text_recipe,
                )
            elif isinstance(
                source, ResolvedCellTranscriptSource | ResolvedCellOcrTextSource
            ):
                dispatch_resolved_evidence_source(
                    retrieval,
                    source,
                    policy=policy,
                )
            else:
                dispatch_resolved_blob_source(
                    retrieval,
                    source,
                    policy=policy,
                )
        sheet_id = int(descriptor["sheet_id"])
        retrieval.remove_cell_bindings_except(
            sheet_id=sheet_id,
            row_id=row_id,
            column_id=column,
            keep_binding_ids=tuple(
                cell_source_binding_id(
                    sheet_id=sheet_id,
                    row_id=row_id,
                    column_id=column,
                    source_id=source.source.source_id,
                )
                for source in resolved
            ),
        )
        return
    sheet_id = (
        int(descriptor["sheet_id"])
        if descriptor is not None
        else (int(old["sheet_id"]) if old is not None else None)
    )
    if sheet_id is not None:
        retire_cell_binding(
            retrieval,
            sheet_id=sheet_id,
            row_id=row_id,
            column_id=column,
        )


def index_batch(
    project: Project,
    *,
    batch_size: int = 500,
    max_bytes: int = 4 * 1024 * 1024,
    cancel_event: threading.Event | None = None,
) -> IndexProgress:
    """Reconcile one bounded dirty page and acknowledge only durable publication."""

    if batch_size < 1 or max_bytes < 1:
        raise ValueError("batch_size and max_bytes must be positive")
    if project.db.in_transaction:
        raise RuntimeError("index maintenance requires its own source transaction")
    bind_retrieval_sidecar(project)
    snapshot = None
    retrieval = None
    try:
        _raise_if_cancelled(cancel_event)
        snapshot = project.read_snapshot()
        policy = read_preparation_policy_db(snapshot.db)
        revision = latest_revision(snapshot.db)
        scopes = read_dirty_scopes(snapshot.db, limit=batch_size + 1)
        processed = processed_bytes = 0
        checkpoints = []
        if scopes:
            retrieval = RetrievalStore(project).open()
        for scope in scopes:
            if processed >= batch_size or processed_bytes >= max_bytes:
                break
            column, row = json.loads(scope.scan_cursor)
            if column == 0:
                column = _next_column(snapshot.db, retrieval, scope, 0)
            before_scope = processed
            byte_limit_reached = False
            while column is not None and processed < batch_size:
                _raise_if_cancelled(cancel_event)
                descriptor = _column_descriptor(snapshot.db, column)
                remaining = batch_size - processed
                ids, live_ids, old_by_row = _column_page(
                    snapshot.db,
                    retrieval,
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
                    if processed_bytes and processed_bytes + size > max_bytes:
                        byte_limit_reached = True
                        break
                    admitted.append((row_id, size))
                    processed_bytes += size
                admitted_live = [
                    row_id for row_id, _size in admitted if row_id in live_ids
                ]
                column_type = str(descriptor["type"]) if descriptor is not None else ""
                text_sources = (
                    resolve_cell_text_sources(
                        snapshot,
                        sheet_id=int(descriptor["sheet_id"]),
                        column_id=column,
                        row_ids=admitted_live,
                    )
                    if admitted_live and column_type in TEXT_SOURCE_COLUMN_TYPES
                    else {}
                )
                resolved_sources = {}
                for row_id in admitted_live:
                    sheet_id = int(descriptor["sheet_id"])
                    sources = []
                    transcript = resolve_cell_transcript_source(
                        snapshot,
                        sheet_id=sheet_id,
                        row_id=row_id,
                        column_id=column,
                    )
                    if transcript is not None:
                        sources.append(transcript)
                    ocr = resolve_cell_ocr_text_source(
                        snapshot,
                        sheet_id=sheet_id,
                        row_id=row_id,
                        column_id=column,
                    )
                    if ocr is not None:
                        sources.append(ocr)
                    if column_type in TEXT_SOURCE_COLUMN_TYPES:
                        if text_source := text_sources.get(row_id):
                            sources.append(text_source)
                    elif column_type in BLOB_SOURCE_COLUMN_TYPES:
                        blob = resolve_cell_blob_source(
                            snapshot,
                            sheet_id=sheet_id,
                            row_id=row_id,
                            column_id=column,
                        )
                        if blob is not None:
                            sources.append(blob)
                    resolved_sources[row_id] = tuple(sources)
                with retrieval.publication_batch():
                    for row_id, _size in admitted:
                        _raise_if_cancelled(cancel_event)
                        _replace_cell(
                            column,
                            row_id,
                            resolved_sources.get(row_id, ()),
                            descriptor,
                            old_by_row.get(row_id),
                            retrieval,
                            policy,
                        )
                        processed += 1
                        row = row_id
                del resolved_sources, text_sources
                if processed_bytes >= max_bytes:
                    byte_limit_reached = True
                if byte_limit_reached:
                    break
                if len(ids) < remaining:
                    if not ids:
                        processed += 1
                    column = _next_column(snapshot.db, retrieval, scope, column)
                    row = 0
            if processed == before_scope and column is None:
                processed += 1
            cursor = None if column is None else json.dumps([column, row])
            checkpoints.append((scope, cursor))
            if cursor is not None:
                break

        complete = len(checkpoints) == len(scopes) and all(
            cursor is None for _, cursor in checkpoints
        )
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
    finally:
        if snapshot is not None:
            snapshot.close()
        if retrieval is not None:
            retrieval.close()


def drain_index(
    project: Project,
    *,
    batch_size: int = 500,
    cancel_event: threading.Event | None = None,
) -> int:
    """Synchronously publish lexical sources without starting an encoder."""

    while not index_batch(
        project, batch_size=batch_size, cancel_event=cancel_event
    ).complete:
        pass
    path = project.path / RETRIEVAL_DB_NAME
    if not path.is_file():
        return 0
    with RetrievalStore(project).open() as store:
        return int(
            store.db.execute(
                "SELECT count(DISTINCT binding.binding_id) FROM bindings binding "
                "JOIN lexical_sources lexical "
                "ON lexical.source_key=binding.source_key"
            ).fetchone()[0]
        )


__all__ = [
    "IndexProgress",
    "SearchIndexNotReady",
    "drain_index",
    "index_batch",
    "index_needs_work",
]
