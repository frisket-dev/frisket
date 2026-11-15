"""Snapshot-bound current read relations with coordinate-scoped winner resolution.

A clean read uses cached heads only after checking the exact requested scope in
its transaction. Dirty reads resolve all candidates before applying value
predicates, including candidates absent from the old cache. SQL is generated
from the authoritative view definitions, not a second precedence algorithm.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Collection


def _ids(values: Collection[int]) -> str:
    ids = sorted(set(values))
    if any(type(value) is not int or value <= 0 for value in ids):
        raise ValueError("current read coordinates must be positive integers")
    return ",".join(map(str, ids)) or "NULL"


def current_heads_sql(
    db: sqlite3.Connection,
    column_ids: Collection[int],
    row_ids: Collection[int] | None = None,
) -> str:
    """Return a scoped SQL relation; consume it within this same transaction."""
    columns = _ids(column_ids)
    rows = None if row_ids is None else _ids(row_ids)
    coordinate = f"column_id IN ({columns})"
    if rows is not None:
        coordinate += f" AND row_id IN ({rows})"
    dirty = f"column_id IN ({columns})"
    if rows is not None:
        dirty += (
            f" AND EXISTS (SELECT 1 FROM json_each('[{rows if rows != 'NULL' else ''}]') "
            "requested WHERE requested.value BETWEEN row_id_start AND row_id_end)"
        )
    # Never select the cache in autocommit: a concurrent writer could invalidate
    # it between this check and the caller executing its query.
    if (
        db.in_transaction
        and db.execute(
            f"SELECT 1 FROM current_cell_repair WHERE {dirty} LIMIT 1"
        ).fetchone()
        is None
    ):
        return f"(SELECT * FROM current_cells WHERE {coordinate})"

    from .change_schema import CHANGE_SCHEMA_SQL

    canonical = CHANGE_SCHEMA_SQL.split(
        "CREATE VIEW IF NOT EXISTS canonical_cell_heads AS", 1
    )[1].split(";", 1)[0]
    contribution_scope = f"contribution.column_id IN ({columns})"
    if rows is not None:
        contribution_scope += f" AND contribution.row_id IN ({rows})"
    canonical = canonical.replace(
        "FROM cell_contributions contribution",
        "FROM current_cell_repair dirty CROSS JOIN cell_contributions contribution "
        "ON contribution.column_id=dirty.column_id "
        "AND contribution.row_id BETWEEN dirty.row_id_start AND dirty.row_id_end "
        f"WHERE {contribution_scope}",
    )
    intersects = (
        "EXISTS (SELECT 1 FROM current_cell_repair dirty "
        "WHERE dirty.column_id=head.column_id "
        "AND head.row_id BETWEEN dirty.row_id_start AND dirty.row_id_end)"
    )
    return (
        f"(SELECT head.* FROM current_cells head WHERE {coordinate} AND NOT {intersects} "
        f"UNION ALL SELECT head.* FROM ({canonical}) head WHERE {intersects})"
    )


def current_values_sql(
    db: sqlite3.Connection,
    column_ids: Collection[int],
    row_ids: Collection[int] | None = None,
) -> str:
    """Expose the canonical value decoder over the same scoped head relation."""
    from .schema import CURRENT_CELL_VALUES_SQL

    heads = current_heads_sql(db, column_ids, row_ids)
    values = CURRENT_CELL_VALUES_SQL.split(" AS", 1)[1].rstrip(";\n ")
    return (
        "("
        + values.replace(
            "FROM effective_current_cells AS head", f"FROM {heads} AS head"
        )
        + ")"
    )
