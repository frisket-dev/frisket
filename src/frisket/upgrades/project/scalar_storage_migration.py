"""Add bounded native scalar copies to the current-cell projection."""

from __future__ import annotations
import sqlite3
from frisket.upgrades.project.historical_current_cells import (
    backfill_inline_scalar_values,
)
from frisket.upgrades.project.review_confidence_migration import (
    REVIEW_CONFIDENCE_TO_DIGEST,
)
from frisket.engine.store.schema import BundleSchemaMismatch
from frisket.upgrades.project._step import SchemaStep
from frisket.upgrades.project.historical_schema import SCALAR_CURRENT_CELL_VALUES_SQL


SCALAR_CURRENT_VALUES_FROM_DIGEST = REVIEW_CONFIDENCE_TO_DIGEST
# Pinned to the fresh-schema DDL installed by this migration. A later schema
# edit must add its own ordered endpoint instead of silently moving this one.
SCALAR_CURRENT_VALUES_TO_DIGEST = "frisket.schema.v1:0cafff8434fb9d00fb3c069601b5eab5"


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


def _apply_scalar_current_values(db: sqlite3.Connection) -> None:
    before = int(db.execute("SELECT COUNT(*) FROM current_cells").fetchone()[0])
    _ensure_inline_columns(db)
    backfill_inline_scalar_values(db)
    db.execute("DROP VIEW current_cell_values")
    db.execute(SCALAR_CURRENT_CELL_VALUES_SQL)
    after = int(db.execute("SELECT COUNT(*) FROM current_cell_values").fetchone()[0])
    if after != before:
        raise BundleSchemaMismatch(
            "scalar current-value migration changed the number of visible cells "
            f"({after}, expected {before})"
        )


SCALAR_CURRENT_VALUES_STEP: SchemaStep = (
    SCALAR_CURRENT_VALUES_FROM_DIGEST,
    SCALAR_CURRENT_VALUES_TO_DIGEST,
    _apply_scalar_current_values,
)


__all__ = [
    "SCALAR_CURRENT_VALUES_FROM_DIGEST",
    "SCALAR_CURRENT_VALUES_TO_DIGEST",
    "SCALAR_CURRENT_CELL_VALUES_SQL",
    "SCALAR_CURRENT_VALUES_STEP",
]
