"""Transactional maintenance of the rebuildable visible-cell projection.

All current writers use one ordered authority. Historical schema migrations
use the separate frozen historical_current_cells module.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Collection, Iterator

from frisket.authoring import column_types
from frisket.engine.store.search_index_work import enqueue_dirty_scope, latest_revision
from frisket.engine.store.value_codec import decode_stored_value

_SQLITE_BIND_LIMIT = 900
_MAX_SEARCH_ROW_RANGES = 128
INLINE_TEXT_BYTE_LIMIT = 512


def register_current_cell_functions(db: sqlite3.Connection) -> None:
    """Register descriptor validation on every project-reading connection."""
    db.create_function("frisket_cell_validity", 3, cell_validity, deterministic=True)


def stamp_retrieval_revision(
    db: sqlite3.Connection,
    revision: int,
    *,
    pairs: Collection[tuple[int, int]] | None = None,
) -> None:
    """Stamp source changes on exact live cells, or all cells during migration."""

    _require_transaction(db)

    columns = (
        [int(row[0]) for row in db.execute("SELECT id FROM columns")]
        if pairs is None
        else sorted({int(column_id) for _, column_id in pairs})
    )
    db.executemany(
        "INSERT INTO cell_authority_revisions(column_id,revision) VALUES (?,?) "
        "ON CONFLICT(column_id) DO UPDATE SET revision=excluded.revision",
        [(column_id, revision) for column_id in columns],
    )
    if pairs is None:
        db.execute("UPDATE current_cells SET retrieval_revision=?", (revision,))
        return
    targets = list(pairs)
    chunk_size = (_SQLITE_BIND_LIMIT - 1) // 2
    for start in range(0, len(targets), chunk_size):
        chunk = targets[start : start + chunk_size]
        values = ",".join("(?,?)" for _ in chunk)
        params = [value for pair in chunk for value in pair]
        db.execute(
            f"WITH requested(row_id,column_id) AS (VALUES {values}) "
            "UPDATE current_cells AS head SET retrieval_revision=? "
            "FROM requested target WHERE head.column_id=target.column_id "
            "AND head.row_id=target.row_id",
            [*params, revision],
        )


def inline_scalar_eligibility_sql(
    *, descriptor_alias: str, value_kind_sql: str, value_sql: str
) -> str:
    """Return the bounded scalar-copy policy for the current projection.

    Native null, integer, real, and boolean values are always compact. Big integers
    stay bounded text, while text/category/date strings share a small byte ceiling.
    Longer text and every structured value remain authority-table references.
    """

    return (
        f"({value_kind_sql} IN ('null','integer','real','boolean') "
        f"OR ({value_kind_sql}='bigint' "
        f"AND {descriptor_alias}.type IN ('integer','number') "
        f"AND length(CAST({value_sql} AS BLOB))<={INLINE_TEXT_BYTE_LIMIT}) "
        f"OR ({value_kind_sql}='text' "
        f"AND {descriptor_alias}.type IN ('text','category','date') "
        f"AND length(CAST({value_sql} AS BLOB))<={INLINE_TEXT_BYTE_LIMIT}))"
    )


def decoded_cell_validity(column_type: str, value: object) -> str:
    """Classify one decoded value against its column descriptor."""

    if value is None:
        return "missing"
    return "valid" if column_types.validate_value(column_type, value) else "invalid"


def cell_validity(
    column_type: str, value_kind: str | None, stored_value: object
) -> str:
    """Classify one native stored value against its column descriptor."""

    # Prepared-content references are physically positive integer ids but are
    # logically text.  Projection refresh cannot query through the same SQLite
    # connection from this UDF, so validate the storage shape and the only
    # supported logical column type without resolving the body here.
    if value_kind == "prepared_content_ref":
        return (
            "valid"
            if column_type == "text"
            and isinstance(stored_value, int)
            and not isinstance(stored_value, bool)
            and stored_value > 0
            else "invalid"
        )
    try:
        value = decode_stored_value(value_kind, stored_value)
    except (TypeError, ValueError, RecursionError):
        return "invalid"
    return decoded_cell_validity(column_type, value)


def _chunks(values: list[int], size: int) -> Iterator[list[int]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _ordered_ids(values: Collection[int]) -> list[int]:
    return sorted({int(value) for value in values})


def _search_row_ranges(rows: list[int]) -> list[tuple[int, int]] | None:
    """Keep sparse edits exact, or use one column scope for fragmented bulk work.

    Input is sorted and unique. Capping the range count avoids creating a
    worklist row per cell for large alternating-row edits.
    """
    ranges: list[tuple[int, int]] = []
    start = end = rows[0]
    for row in rows[1:]:
        if row != end + 1:
            ranges.append((start, end))
            if len(ranges) == _MAX_SEARCH_ROW_RANGES:
                return None
            start = row
        end = row
    ranges.append((start, end))
    return ranges


def _require_transaction(db: sqlite3.Connection) -> None:
    if not db.in_transaction:
        raise RuntimeError(
            "current-cell projection writes require a caller transaction"
        )


def _temp_key_table(name: str) -> str:
    if re.fullmatch(r"temp_result_keys_[0-9a-f]+", name) is None:
        raise ValueError("invalid temporary result-key table name")
    return name


def _enqueue_column_work(
    db: sqlite3.Connection,
    *,
    sheet_id: int,
    column_id: int,
    ranges: list[tuple[int, int]] | None,
) -> None:
    from .document_text import document_text_related_column_ids

    for related in document_text_related_column_ids(db, column_id=column_id):
        if ranges is None:
            enqueue_dirty_scope(db, sheet_id=sheet_id, column_id=related)
        else:
            for start, end in ranges:
                enqueue_dirty_scope(
                    db,
                    sheet_id=sheet_id,
                    column_id=related,
                    row_id_start=start,
                    row_id_end=end,
                )


def _enqueue_region(
    db: sqlite3.Connection, *, column_ids: list[int], row_ids: list[int] | None
) -> int:
    ranges = None if row_ids is None else _search_row_ranges(row_ids)
    for column_id in column_ids:
        sheet = db.execute(
            "SELECT sheet_id FROM columns WHERE id=?", (column_id,)
        ).fetchone()
        if sheet is None:
            continue
        sheet_id = int(sheet[0])
        _enqueue_column_work(db, sheet_id=sheet_id, column_id=column_id, ranges=ranges)
    return latest_revision(db)


def refresh_current_cells_from_key_table(db: sqlite3.Connection, key_table: str) -> int:
    """Refresh exact keys from a caller-owned temporary SQL relation."""

    _require_transaction(db)
    table = _temp_key_table(key_table)
    from .cell_projection import refresh_ordered_cells

    total = 0
    for column in db.execute(f"SELECT DISTINCT column_id FROM {table}").fetchall():
        column_id = int(column[0])
        # A complete column invalidation is compact even for a million
        # sparse historical keys; canonical reads preserve untouched cells.
        revision = _enqueue_region(db, column_ids=[column_id], row_ids=None)
        total += refresh_ordered_cells(
            db, [column_id], None, retrieval_revision=revision
        )
    return total


def refresh_current_cells(
    db: sqlite3.Connection,
    *,
    column_ids: Collection[int],
    row_ids: Collection[int] | None = None,
    source_values_changed: bool = False,
) -> int:
    """Refresh one column/row region inside the caller's transaction.

    ``row_ids=None`` means every row in the selected columns. Empty explicit
    collections are no-ops. ``source_values_changed`` is the narrow signal for
    a canonical writer that replaced base-cell values under the same producer;
    it forces revisions only where the final visible head is that source cell.
    The return value is the number of projected rows now present in the region.
    """

    _require_transaction(db)
    columns = _ordered_ids(column_ids)
    if not columns:
        return 0
    rows = None if row_ids is None else _ordered_ids(row_ids)
    if rows == []:
        return 0

    retrieval_revision = _enqueue_region(db, column_ids=columns, row_ids=rows)
    from .cell_projection import refresh_ordered_cells

    return refresh_ordered_cells(
        db, columns, rows, retrieval_revision=retrieval_revision
    )


def refresh_current_cell_pairs(
    db: sqlite3.Connection, pairs: Collection[tuple[int, int]]
) -> int:
    """Refresh exact ``(row_id, column_id)`` pairs without an N-squared region.

    Columns sharing the same row set remain one dense projector call; sparse
    columns stay separate instead of acquiring every other column's rows.
    """

    _require_transaction(db)
    rows_by_column: dict[int, set[int]] = {}
    for row_id, column_id in pairs:
        rows_by_column.setdefault(int(column_id), set()).add(int(row_id))
    columns_by_rows: dict[tuple[int, ...], list[int]] = {}
    for column_id, row_ids in rows_by_column.items():
        columns_by_rows.setdefault(tuple(sorted(row_ids)), []).append(column_id)
    groups = [
        (row_ids, sorted(column_ids))
        for row_ids, column_ids in sorted(columns_by_rows.items())
    ]
    total = sum(
        refresh_current_cells(db, column_ids=column_ids, row_ids=row_ids)
        for row_ids, column_ids in groups
    )
    return total


def rebuild_current_cells(
    db: sqlite3.Connection, *, invalidate_search: bool = True
) -> int:
    """Rebuild the complete projection in bounded column batches.

    Historical migrations that predate the search worklist explicitly suppress
    invalidation; their later search-schema migration seeds a full repair.
    """

    _require_transaction(db)

    return refresh_current_cells(
        db, column_ids=[int(row[0]) for row in db.execute("SELECT id FROM columns")]
    )
