"""Snapshot-bound current read relations with coordinate-scoped winner resolution.

A clean read uses cached heads only after checking the exact requested scope in
its transaction. Dirty reads resolve all candidates before applying value
predicates, including candidates absent from the old cache. SQL is generated
by the shared canonical builder, not parsed from persisted view DDL.
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

    from .change_schema import canonical_heads_sql

    contribution_scope = f"contribution.column_id IN ({columns})"
    if rows is not None:
        contribution_scope += f" AND contribution.row_id IN ({rows})"
    canonical = canonical_heads_sql(
        "current_cell_repair dirty CROSS JOIN cell_contributions contribution "
        "ON contribution.column_id=dirty.column_id "
        "AND contribution.row_id BETWEEN dirty.row_id_start AND dirty.row_id_end "
        f"WHERE {contribution_scope}"
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
    from .current_value_sql import current_value_select_sql

    return (
        "(" + current_value_select_sql(current_heads_sql(db, column_ids, row_ids)) + ")"
    )


def generated_result_coordinate_sql(coordinate_alias: str) -> str:
    """Discover a top non-edit result identity for one correlated coordinate."""
    import re
    from .change_schema import CELL_CONTRIBUTION_ARMS, generated_candidates_sql

    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", coordinate_alias):
        raise ValueError("coordinate SQL alias must be an identifier")
    # SQLite does not push a correlated predicate through this UNION/window.
    scoped = " UNION ALL ".join(
        "SELECT * FROM (" + arm + ") candidate "
        f"WHERE candidate.column_id={coordinate_alias}.column_id "
        f"AND candidate.row_id={coordinate_alias}.row_id"
        for arm in CELL_CONTRIBUTION_ARMS
    )
    return "(" + generated_candidates_sql(f"({scoped}) contribution") + ")"


def reconciliation_heads_sql(db, column_id, row_ids, *, exclude_change_id):
    """Recover working values beneath this writer's mask, using real authority.

    Internal reconciliation reads never consult cached heads or suppress later
    independent Changes. The omitted Change is still active everywhere else.
    """
    from .change_schema import canonical_heads_sql

    owner = db.execute(
        "SELECT change_order FROM changes WHERE id=?", (exclude_change_id,)
    ).fetchone()
    if owner is None or owner[0] is None:
        raise ValueError("Reconciliation requires an ordered owning Change")
    scope = f"contribution.column_id IN ({_ids([column_id])}) AND contribution.row_id IN ({_ids(row_ids)}) AND contribution.change_order != {int(owner[0])}"
    return (
        "("
        + canonical_heads_sql("cell_contributions contribution WHERE " + scope)
        + ")"
    )
