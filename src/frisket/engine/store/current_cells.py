"""Transactional maintenance of the rebuildable visible-cell projection.

The authoritative layers remain ``cells``, generation-managed result heads,
and operation-owned ``edits``.  This module contains their one precedence
query so incremental refresh and full repair cannot drift apart.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Collection, Iterator

_SQLITE_BIND_LIMIT = 900


def live_edit_precedence_predicate(*, edit_alias: str, op_alias: str) -> str:
    """Keep edits except reject-clear overlays superseded by a replacement."""

    return (
        f"({op_alias}.kind<>'review.decision' "
        f"OR COALESCE(json_extract({op_alias}.spec,'$.action_id'),'')<>"
        "'review.decision' "
        f"OR COALESCE(json_extract({op_alias}.spec,'$.params.decision'),'')<>"
        "'reject_clear' OR NOT EXISTS ("
        "SELECT 1 FROM cell_result_heads replacement_head "
        "JOIN runs replacement_run ON replacement_run.id=replacement_head.run_id "
        "JOIN ops replacement_op ON replacement_op.id=replacement_run.op_id "
        f"WHERE replacement_head.column_id={edit_alias}.column_id "
        f"AND replacement_head.row_id={edit_alias}.row_id "
        f"AND replacement_run.op_id>{edit_alias}.op_id "
        "AND replacement_op.status='applied' "
        "AND COALESCE(json_extract(replacement_op.spec,"
        "'$.replace_existing'),0)=1))"
    )


def _descriptor_boundary_predicate(*, edit_alias: str) -> str:
    """Exclude edits at or before the latest applied descriptor replacement."""

    return (
        "NOT EXISTS ("
        "SELECT 1 FROM run_output_generations boundary_generation "
        "JOIN runs boundary_run ON boundary_run.id=boundary_generation.run_id "
        "JOIN ops boundary_op ON boundary_op.id=boundary_run.op_id "
        f"WHERE boundary_generation.column_id={edit_alias}.column_id "
        "AND boundary_generation.state='sealed' "
        "AND boundary_op.status='applied' "
        f"AND boundary_op.id>={edit_alias}.op_id "
        "AND ("
        "EXISTS (SELECT 1 FROM json_each(COALESCE(json_extract("
        "boundary_op.undo_info,'$.column_types_after'),'{}')) item "
        f"WHERE item.key=CAST({edit_alias}.column_id AS TEXT)) "
        "OR EXISTS (SELECT 1 FROM json_each(COALESCE(json_extract("
        "boundary_op.undo_info,'$.column_formats_after'),'{}')) item "
        f"WHERE item.key=CAST({edit_alias}.column_id AS TEXT)) "
        "OR EXISTS (SELECT 1 FROM json_each(COALESCE(json_extract("
        "boundary_op.undo_info,'$.column_semantic_types_after'),'{}')) item "
        f"WHERE item.key=CAST({edit_alias}.column_id AS TEXT))"
        "))"
    )


def _chunks(values: list[int], size: int) -> Iterator[list[int]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _ordered_ids(values: Collection[int]) -> list[int]:
    return sorted({int(value) for value in values})


def _require_transaction(db: sqlite3.Connection) -> None:
    if not db.in_transaction:
        raise RuntimeError(
            "current-cell projection writes require a caller transaction"
        )


def _target_cte(name: str, values: list[int]) -> str:
    return f"{name}(id) AS (VALUES {','.join('(?)' for _ in values)})"


def _delete_region(
    db: sqlite3.Connection, *, column_ids: list[int], row_ids: list[int] | None
) -> None:
    column_placeholders = ",".join("?" for _ in column_ids)
    params: list[int] = list(column_ids)
    row_filter = ""
    if row_ids is not None:
        row_filter = f" AND row_id IN ({','.join('?' for _ in row_ids)})"
        params.extend(row_ids)
    db.execute(
        f"DELETE FROM current_cells WHERE column_id IN ({column_placeholders})"
        f"{row_filter}",
        params,
    )


def _insert_region(
    db: sqlite3.Connection, *, column_ids: list[int], row_ids: list[int] | None
) -> int:
    ctes = [_target_cte("target_columns", column_ids)]
    params: list[int] = list(column_ids)
    if row_ids is not None:
        ctes.append(_target_cte("target_rows", row_ids))
        params.extend(row_ids)
        # CROSS JOIN fixes the small target sets as the outer loops. Without
        # it SQLite can drive from the large value tables, turning a 100-cell
        # refresh into a full-column scan despite the exact composite keys.
        source_from = (
            "FROM target_rows target_row "
            "CROSS JOIN target_columns target_column "
            "JOIN cells source ON source.row_id=target_row.id "
            "AND source.column_id=target_column.id "
        )
        result_from = (
            "FROM target_columns target_column "
            "CROSS JOIN target_rows target_row "
            "CROSS JOIN cell_result_heads head ON head.column_id=target_column.id "
            "AND head.row_id=target_row.id "
        )
        edit_from = (
            "FROM target_columns target_column "
            "CROSS JOIN target_rows target_row "
            "CROSS JOIN edits source ON source.column_id=target_column.id "
            "AND source.row_id=target_row.id "
        )
    else:
        source_from = (
            "FROM target_columns target_column "
            "JOIN cells source ON source.column_id=target_column.id "
        )
        result_from = (
            "FROM target_columns target_column "
            "JOIN cell_result_heads head ON head.column_id=target_column.id "
        )
        edit_from = (
            "FROM target_columns target_column "
            "JOIN edits source ON source.column_id=target_column.id "
        )

    edit_precedence = live_edit_precedence_predicate(
        edit_alias="source", op_alias="source_op"
    )
    descriptor_precedence = _descriptor_boundary_predicate(edit_alias="source")
    db.execute(
        "WITH " + ",".join(ctes) + ", candidates AS ("
        "SELECT source.column_id,source.row_id,source.value,"
        "'source_cell' AS origin_kind,NULL AS origin_op_id,"
        "NULL AS origin_run_id,source.producer_id AS base_producer_id,"
        "0 AS layer_precedence,0 AS origin_precedence "
        + source_from
        + " JOIN rows source_row ON source_row.id=source.row_id "
        "JOIN columns source_column ON source_column.id=source.column_id "
        "AND source_column.sheet_id=source_row.sheet_id "
        "UNION ALL "
        "SELECT result.column_id,result.row_id,"
        "CASE WHEN result.publication_effect='publish_value' "
        "THEN result.value ELSE NULL END,"
        "'run_result',run.op_id,result.run_id,NULL,1,result.run_id "
        + result_from
        + " JOIN results result ON result.run_id=head.run_id "
        "AND result.row_id=head.row_id AND result.column_id=head.column_id "
        "JOIN runs run ON run.id=result.run_id "
        "JOIN rows result_row ON result_row.id=result.row_id "
        "JOIN columns result_column ON result_column.id=result.column_id "
        "AND result_column.sheet_id=result_row.sheet_id "
        "UNION ALL "
        "SELECT source.column_id,source.row_id,source.value,"
        "'manual_edit',source.op_id,NULL,NULL,2,source.op_id "
        + edit_from
        + " JOIN ops source_op ON source_op.id=source.op_id "
        "JOIN rows edit_row ON edit_row.id=source.row_id "
        "JOIN columns edit_column ON edit_column.id=source.column_id "
        "AND edit_column.sheet_id=edit_row.sheet_id "
        "WHERE source_op.status='applied' AND "
        + descriptor_precedence
        + " AND "
        + edit_precedence
        + "), ranked AS ("
        "SELECT *,ROW_NUMBER() OVER ("
        "PARTITION BY column_id,row_id "
        "ORDER BY layer_precedence DESC,origin_precedence DESC"
        ") AS rank FROM candidates) "
        "INSERT INTO current_cells "
        "(column_id,row_id,value,origin_kind,origin_op_id,origin_run_id,"
        "base_producer_id) "
        "SELECT column_id,row_id,value,origin_kind,origin_op_id,origin_run_id,"
        "base_producer_id FROM ranked WHERE rank=1",
        params,
    )
    return int(db.execute("SELECT changes()").fetchone()[0])


def refresh_current_cells(
    db: sqlite3.Connection,
    *,
    column_ids: Collection[int],
    row_ids: Collection[int] | None = None,
) -> int:
    """Refresh one column/row region inside the caller's transaction.

    ``row_ids=None`` means every row in the selected columns. Empty explicit
    collections are no-ops. The return value is the number of projected rows
    now present in the refreshed region.
    """

    _require_transaction(db)
    columns = _ordered_ids(column_ids)
    if not columns:
        return 0
    rows = None if row_ids is None else _ordered_ids(row_ids)
    if rows == []:
        return 0

    total = 0
    if rows is None:
        for column_chunk in _chunks(columns, _SQLITE_BIND_LIMIT):
            _delete_region(db, column_ids=column_chunk, row_ids=None)
            total += _insert_region(db, column_ids=column_chunk, row_ids=None)
        return total

    # Keep the combined target CTE below SQLite's conservative bind budget.
    column_chunk_size = min(len(columns), _SQLITE_BIND_LIMIT // 2)
    for column_chunk in _chunks(columns, column_chunk_size):
        row_chunk_size = _SQLITE_BIND_LIMIT - len(column_chunk)
        for row_chunk in _chunks(rows, row_chunk_size):
            _delete_region(db, column_ids=column_chunk, row_ids=row_chunk)
            total += _insert_region(db, column_ids=column_chunk, row_ids=row_chunk)
    return total


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
    return sum(
        refresh_current_cells(db, column_ids=sorted(column_ids), row_ids=row_ids)
        for row_ids, column_ids in sorted(columns_by_rows.items())
    )


def rebuild_current_cells(db: sqlite3.Connection) -> int:
    """Rebuild the complete projection in bounded column batches."""

    _require_transaction(db)
    db.execute("DELETE FROM current_cells")
    total = 0
    columns = db.execute("SELECT id FROM columns ORDER BY id")
    while batch := columns.fetchmany(_SQLITE_BIND_LIMIT):
        total += _insert_region(
            db,
            column_ids=[int(row[0]) for row in batch],
            row_ids=None,
        )
    return total
