"""Add bounded native scalar copies to the current-cell projection."""

from __future__ import annotations

import sqlite3

from .current_cells import inline_scalar_eligibility_sql
from .review_confidence_migration import REVIEW_CONFIDENCE_TO_DIGEST
from .schema import BundleSchemaMismatch, SCHEMA_DIGEST_META_KEY


SCALAR_CURRENT_VALUES_FROM_DIGEST = REVIEW_CONFIDENCE_TO_DIGEST
# Pinned to the fresh-schema DDL installed by this migration. A later schema
# edit must add its own ordered endpoint instead of silently moving this one.
SCALAR_CURRENT_VALUES_TO_DIGEST = "frisket.schema.v1:0cafff8434fb9d00fb3c069601b5eab5"


def _fresh_schema_statement(prefix: str) -> str:
    from .schema import SCHEMA

    start = SCHEMA.index(prefix)
    statement = ""
    for line in SCHEMA[start:].splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            return statement.strip()
    raise RuntimeError(f"incomplete schema statement starting with {prefix!r}")


def _ensure_inline_columns(db: sqlite3.Connection) -> None:
    columns = {str(row[1]) for row in db.execute("PRAGMA table_info(current_cells)")}
    inline = {"inline_value_kind", "inline_value"}
    present = columns & inline
    if present and present != inline:
        raise BundleSchemaMismatch(
            "scalar current-value migration found a partial inline projection"
        )
    if present:
        # The typed-value migration rebuilds current_cells from the live table
        # DDL. A bundle traversing that historical endpoint in this same open
        # may therefore already have the pair; the exact prior digest still
        # controls whether this migration may backfill and stamp it.
        return
    db.execute(
        "ALTER TABLE current_cells ADD COLUMN inline_value_kind TEXT "
        "CHECK (inline_value_kind IN "
        "('null','text','integer','real','boolean','bigint'))"
    )
    db.execute(
        "ALTER TABLE current_cells ADD COLUMN inline_value "
        "CHECK ("
        "(inline_value_kind IS NULL AND inline_value IS NULL) "
        "OR (inline_value_kind='null' AND inline_value IS NULL) "
        "OR (inline_value_kind='text' AND typeof(inline_value)='text') "
        "OR (inline_value_kind='integer' AND typeof(inline_value)='integer') "
        "OR (inline_value_kind='real' AND typeof(inline_value)='real' "
        "AND inline_value=inline_value "
        "AND abs(inline_value)<=1.7976931348623157e308) "
        "OR (inline_value_kind='boolean' AND typeof(inline_value)='integer' "
        "AND inline_value IN (0,1)) "
        "OR (inline_value_kind='bigint' AND typeof(inline_value)='text'))"
    )


def _backfill_inline_values(db: sqlite3.Connection) -> None:
    eligible = inline_scalar_eligibility_sql(
        descriptor_alias="descriptor",
        value_kind_sql="live.value_kind",
        value_sql="live.value",
    )
    db.execute(
        "WITH eligible AS ("
        "SELECT live.column_id,live.row_id,live.value_kind,live.value "
        "FROM current_cell_values live "
        "JOIN columns descriptor ON descriptor.id=live.column_id "
        f"WHERE {eligible}"
        ") UPDATE current_cells AS head SET "
        "inline_value_kind=eligible.value_kind,inline_value=eligible.value "
        "FROM eligible WHERE head.column_id=eligible.column_id "
        "AND head.row_id=eligible.row_id"
    )


def _apply_scalar_current_values(db: sqlite3.Connection) -> None:
    before = int(db.execute("SELECT COUNT(*) FROM current_cells").fetchone()[0])
    _ensure_inline_columns(db)
    _backfill_inline_values(db)
    db.execute("DROP VIEW current_cell_values")
    db.execute(_fresh_schema_statement("CREATE VIEW IF NOT EXISTS current_cell_values"))
    after = int(db.execute("SELECT COUNT(*) FROM current_cell_values").fetchone()[0])
    if after != before:
        raise BundleSchemaMismatch(
            "scalar current-value migration changed the number of visible cells "
            f"({after}, expected {before})"
        )
    db.execute(
        "UPDATE meta SET value=? WHERE key=?",
        (SCALAR_CURRENT_VALUES_TO_DIGEST, SCHEMA_DIGEST_META_KEY),
    )


def migrate_scalar_current_values(db: sqlite3.Connection) -> None:
    """Backfill the scalar projection from the exact preceding schema once."""

    query = "SELECT value FROM meta WHERE key=?"
    try:
        row = db.execute(query, (SCHEMA_DIGEST_META_KEY,)).fetchone()
    except sqlite3.DatabaseError:
        return
    if row is None or row[0] != SCALAR_CURRENT_VALUES_FROM_DIGEST:
        return

    db.execute("BEGIN IMMEDIATE")
    try:
        row = db.execute(query, (SCHEMA_DIGEST_META_KEY,)).fetchone()
        if row is not None and row[0] == SCALAR_CURRENT_VALUES_FROM_DIGEST:
            _apply_scalar_current_values(db)
        db.commit()
    except BaseException:
        db.rollback()
        raise


__all__ = [
    "SCALAR_CURRENT_VALUES_FROM_DIGEST",
    "SCALAR_CURRENT_VALUES_TO_DIGEST",
    "migrate_scalar_current_values",
]
