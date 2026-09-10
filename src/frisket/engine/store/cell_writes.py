"""Provenance-owned writes for authoritative base cells and edit overlays.

These helpers are deliberately transaction-neutral: they neither begin nor
commit work. Callers allocate the truthful operation/producer, open their
transaction, and this owner performs the protected-table DML plus projection
refresh as one unit with the caller's structural changes.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from typing import Any

from .current_cells import refresh_current_cell_pairs, refresh_current_cells


@dataclass(frozen=True)
class BaseCellWrite:
    row_id: int
    column_id: int
    value: Any


@dataclass(frozen=True)
class EditCellWrite:
    row_id: int
    column_id: int
    value: Any


def _require_transaction(db: sqlite3.Connection) -> None:
    if not db.in_transaction:
        raise RuntimeError("cell writes require a caller transaction")


def _require_applied_op(db: sqlite3.Connection, op_id: int) -> None:
    row = db.execute("SELECT status FROM ops WHERE id=?", (int(op_id),)).fetchone()
    if row is None:
        raise ValueError(f"unknown producing operation {int(op_id)}")
    if str(row[0]) != "applied":
        raise ValueError(f"producing operation {int(op_id)} is not applied")


def _require_base_producer(
    db: sqlite3.Connection, producer_id: int, *, allow_pending: bool = False
) -> int | None:
    row = db.execute(
        "SELECT producer.op_id,op.status FROM base_cell_producers producer "
        "LEFT JOIN ops op ON op.id=producer.op_id WHERE producer.id=?",
        (int(producer_id),),
    ).fetchone()
    if row is None:
        raise ValueError(f"unknown base-cell producer {int(producer_id)}")
    if row[0] is None:
        if not allow_pending:
            raise ValueError(
                f"base-cell producer {int(producer_id)} is not bound to an operation"
            )
        return None
    if row[0] is not None and row[1] != "applied":
        raise ValueError(
            f"base-cell producer {int(producer_id)} is not bound to an applied operation"
        )
    return int(row[0])


def create_base_cell_producer(
    db: sqlite3.Connection, *, stage_id: str, op_id: int | None = None
) -> int:
    """Create or recover one logical base-write producer.

    ``stage_id`` is an admitted opaque identity, not a provenance dictionary.
    For ordinary applied operations callers use ``op:<id>``. Streamed hidden
    writes use their durable staging identity and bind the final operation at
    publication.
    """

    _require_transaction(db)
    if not isinstance(stage_id, str) or not stage_id.strip():
        raise ValueError("base-cell producer stage_id must be non-empty")
    if op_id is not None:
        op_id = int(op_id)
        _require_applied_op(db, op_id)

    existing = db.execute(
        "SELECT id,op_id FROM base_cell_producers WHERE stage_id=?", (stage_id,)
    ).fetchone()
    if existing is not None:
        producer_id = int(existing[0])
        existing_op_id = None if existing[1] is None else int(existing[1])
        if op_id is None or existing_op_id == op_id:
            return producer_id
        if existing_op_id is not None:
            raise ValueError(
                f"base-cell producer {producer_id} is already bound to operation "
                f"{existing_op_id}"
            )
        db.execute(
            "UPDATE base_cell_producers SET op_id=? WHERE id=? AND op_id IS NULL",
            (op_id, producer_id),
        )
        return producer_id

    cursor = db.execute(
        "INSERT INTO base_cell_producers (stage_id,op_id) VALUES (?,?)",
        (stage_id, op_id),
    )
    return int(cursor.lastrowid)


def bind_base_cell_producer(
    db: sqlite3.Connection, producer_id: int, *, op_id: int
) -> None:
    """Bind a pending producer once; same-operation replay is idempotent."""

    _require_transaction(db)
    producer_id = int(producer_id)
    op_id = int(op_id)
    _require_applied_op(db, op_id)
    row = db.execute(
        "SELECT op_id FROM base_cell_producers WHERE id=?", (producer_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"unknown base-cell producer {producer_id}")
    existing_op_id = None if row[0] is None else int(row[0])
    if existing_op_id == op_id:
        return
    if existing_op_id is not None:
        raise ValueError(
            f"base-cell producer {producer_id} is already bound to operation "
            f"{existing_op_id}"
        )
    db.execute(
        "UPDATE base_cell_producers SET op_id=? WHERE id=? AND op_id IS NULL",
        (op_id, producer_id),
    )


def discard_pending_base_cell_producer(
    db: sqlite3.Connection, producer_id: int
) -> None:
    """Remove an unbound, now-unreferenced producer during staging abort."""

    _require_transaction(db)
    producer_id = int(producer_id)
    row = db.execute(
        "SELECT op_id FROM base_cell_producers WHERE id=?", (producer_id,)
    ).fetchone()
    if row is None:
        return
    if row[0] is not None:
        raise ValueError(f"cannot discard bound base-cell producer {producer_id}")
    if db.execute(
        "SELECT 1 FROM cells WHERE producer_id=? LIMIT 1", (producer_id,)
    ).fetchone():
        raise ValueError(f"cannot discard referenced base-cell producer {producer_id}")
    db.execute("DELETE FROM base_cell_producers WHERE id=?", (producer_id,))


def _encode_base_cells(cells: Iterable[BaseCellWrite]) -> list[tuple[int, int, str]]:
    encoded: list[tuple[int, int, str]] = []
    for cell in cells:
        encoded.append(
            (
                int(cell.row_id),
                int(cell.column_id),
                json.dumps(cell.value, allow_nan=False),
            )
        )
    return encoded


def _validate_cell_pairs(
    db: sqlite3.Connection,
    pairs: Collection[tuple[int, int]],
    *,
    require_hidden_sheet: bool = False,
) -> None:
    """Require every target row and column to exist in the same sheet."""

    if not pairs:
        return
    encoded = json.dumps([[row_id, column_id] for row_id, column_id in pairs])
    invalid = db.execute(
        "WITH requested(row_id,column_id) AS ("
        "SELECT CAST(json_extract(item.value,'$[0]') AS INTEGER),"
        "CAST(json_extract(item.value,'$[1]') AS INTEGER) FROM json_each(?) item"
        ") SELECT requested.row_id,requested.column_id,"
        "row.sheet_id,column.sheet_id,sheet.hidden "
        "FROM requested LEFT JOIN rows row ON row.id=requested.row_id "
        "LEFT JOIN columns column ON column.id=requested.column_id "
        "LEFT JOIN sheets sheet ON sheet.id=row.sheet_id "
        "WHERE row.id IS NULL OR column.id IS NULL OR row.sheet_id<>column.sheet_id "
        "OR (? AND COALESCE(sheet.hidden,0)<>1) "
        "LIMIT 1",
        (encoded, int(require_hidden_sheet)),
    ).fetchone()
    if invalid is not None:
        if (
            require_hidden_sheet
            and invalid[2] is not None
            and invalid[2] == invalid[3]
            and invalid[4] != 1
        ):
            raise ValueError(
                "pending base-cell producer requires a hidden staging sheet"
            )
        raise ValueError(
            f"cell target row {int(invalid[0])} and column {int(invalid[1])} "
            "do not belong to the same sheet"
        )


def _refresh_written_region(
    db: sqlite3.Connection, encoded: Collection[tuple[int, int, str]]
) -> None:
    if not encoded:
        return
    refresh_current_cell_pairs(
        db, {(row_id, column_id) for row_id, column_id, _value in encoded}
    )


def initialize_base_cells(
    db: sqlite3.Connection,
    *,
    producer_id: int,
    cells: Iterable[BaseCellWrite],
) -> int:
    """Initialize absent base cells, refusing accidental overwrite."""

    _require_transaction(db)
    producer_id = int(producer_id)
    producing_op_id = _require_base_producer(db, producer_id, allow_pending=True)
    encoded = _encode_base_cells(cells)
    if not encoded:
        return 0
    _validate_cell_pairs(
        db,
        [(row_id, column_id) for row_id, column_id, _value in encoded],
        require_hidden_sheet=producing_op_id is None,
    )
    db.executemany(
        "INSERT INTO cells (row_id,column_id,value,producer_id) VALUES (?,?,?,?)",
        [(*cell, producer_id) for cell in encoded],
    )
    _refresh_written_region(db, encoded)
    return len(encoded)


def remove_base_cells(
    db: sqlite3.Connection,
    *,
    producer_id: int,
    column_ids: Collection[int],
    row_ids: Collection[int] | None = None,
) -> int:
    """Remove a deliberate base-cell region and refresh its visible winners."""

    _require_transaction(db)
    _require_base_producer(db, int(producer_id))
    columns = sorted({int(column_id) for column_id in column_ids})
    rows = None if row_ids is None else sorted({int(row_id) for row_id in row_ids})
    if not columns or rows == []:
        return 0
    params: list[int] = list(columns)
    where = f"column_id IN ({','.join('?' for _ in columns)})"
    if rows is not None:
        where += f" AND row_id IN ({','.join('?' for _ in rows)})"
        params.extend(rows)
    cursor = db.execute(f"DELETE FROM cells WHERE {where}", params)
    refresh_current_cells(db, column_ids=columns, row_ids=rows)
    return int(cursor.rowcount)


def delete_sheet_rows(
    db: sqlite3.Connection, *, sheet_id: int, producer_id: int
) -> int:
    """Delete one sheet's rows under a known base-write producer.

    Row foreign-key cascades own removal of authoritative cells and their
    derived projection rows. The sheet itself and any operation-specific
    lineage remain the higher-level caller's responsibility.
    """

    _require_transaction(db)
    _require_base_producer(db, int(producer_id))
    cursor = db.execute("DELETE FROM rows WHERE sheet_id=?", (int(sheet_id),))
    return int(cursor.rowcount)


def replace_base_cells(
    db: sqlite3.Connection,
    *,
    producer_id: int,
    column_ids: Collection[int],
    cells: Iterable[BaseCellWrite],
    row_ids: Collection[int] | None = None,
) -> int:
    """Replace exactly one declared base-cell region with a new sparse set."""

    _require_transaction(db)
    producer_id = int(producer_id)
    _require_base_producer(db, producer_id)
    columns = sorted({int(column_id) for column_id in column_ids})
    rows = None if row_ids is None else sorted({int(row_id) for row_id in row_ids})
    if not columns or rows == []:
        return 0
    encoded = _encode_base_cells(cells)
    column_scope = set(columns)
    row_scope = None if rows is None else set(rows)
    if any(
        column_id not in column_scope
        or (row_scope is not None and row_id not in row_scope)
        for row_id, column_id, _value in encoded
    ):
        raise ValueError("replacement cell falls outside its declared region")
    _validate_cell_pairs(
        db, [(row_id, column_id) for row_id, column_id, _value in encoded]
    )

    params: list[int] = list(columns)
    where = f"column_id IN ({','.join('?' for _ in columns)})"
    if rows is not None:
        where += f" AND row_id IN ({','.join('?' for _ in rows)})"
        params.extend(rows)
    db.execute(f"DELETE FROM cells WHERE {where}", params)
    if encoded:
        db.executemany(
            "INSERT INTO cells (row_id,column_id,value,producer_id) VALUES (?,?,?,?)",
            [(*cell, producer_id) for cell in encoded],
        )
    refresh_current_cells(db, column_ids=columns, row_ids=rows)
    return len(encoded)


def insert_edits(
    db: sqlite3.Connection, *, op_id: int, edits: Iterable[EditCellWrite]
) -> int:
    """Insert operation-owned edit overlays and refresh their exact cells."""

    _require_transaction(db)
    op_id = int(op_id)
    _require_applied_op(db, op_id)
    encoded = [
        (
            op_id,
            int(edit.row_id),
            int(edit.column_id),
            json.dumps(edit.value, allow_nan=False),
        )
        for edit in edits
    ]
    if not encoded:
        return 0
    _validate_cell_pairs(
        db, [(row_id, column_id) for _op, row_id, column_id, _value in encoded]
    )
    db.executemany(
        "INSERT INTO edits (op_id,row_id,column_id,value) VALUES (?,?,?,?)",
        encoded,
    )
    refresh_current_cell_pairs(
        db,
        {(row_id, column_id) for _op, row_id, column_id, _value in encoded},
    )
    return len(encoded)
