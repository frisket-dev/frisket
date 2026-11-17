"""Add immutable prepared-page content and its internal value reference kind."""

from __future__ import annotations
import sqlite3
from frisket.upgrades.project.prepared_content_migration_schema import (
    PREPARED_AUTHORITY_TABLE_SQL,
    PREPARED_CONTENT_SCHEMA_SQL,
    PREPARED_CURRENT_CELL_VALUES_SQL,
)
from frisket.engine.store.schema import BundleSchemaMismatch
from frisket.upgrades.project._step import SchemaStep
from frisket.upgrades.project.extraction_layouts_migration import (
    EXTRACTION_LAYOUTS_TO_DIGEST,
)


PREPARED_CONTENT_FROM_DIGEST = EXTRACTION_LAYOUTS_TO_DIGEST
# Pinned to this migration's fresh-schema DDL.
PREPARED_CONTENT_TO_DIGEST = "frisket.schema.v1:59e024b9950ba4cc2232bd1bc42ae688"


def _install_prepared_schema(db: sqlite3.Connection) -> None:
    for statement in PREPARED_CONTENT_SCHEMA_SQL:
        db.execute(statement)


def _widen_authorities(db: sqlite3.Connection) -> None:
    """Widen typed authorities and install prepared content atomically."""
    visible_before = int(
        db.execute("SELECT COUNT(*) FROM current_cell_values").fetchone()[0]
    )
    db.execute("DROP VIEW current_cell_values")
    authority_objects = db.execute(
        "SELECT type,sql FROM sqlite_master "
        "WHERE tbl_name IN ('cells','results','edits') "
        "AND type IN ('index','trigger') AND sql IS NOT NULL "
        "ORDER BY CASE type WHEN 'index' THEN 0 ELSE 1 END,name"
    ).fetchall()
    for table in ("cells", "results", "edits"):
        columns = [str(info[1]) for info in db.execute(f"PRAGMA table_info({table})")]
        db.execute(PREPARED_AUTHORITY_TABLE_SQL[table])
        # Some older upgrade entry points retain already-added result
        # metadata. Preserve it through this pinned historical rebuild;
        # genuine predecessors still receive the original table layout.
        if table == "results" and "warnings" in columns:
            db.execute(
                "ALTER TABLE results_prepared_new ADD COLUMN warnings "
                "TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(warnings) "
                "AND json_type(warnings)='array')"
            )
        joined = ",".join(columns)
        db.execute(
            f"INSERT INTO {table}_prepared_new({joined}) SELECT {joined} FROM {table}"
        )
    for table in ("cells", "results", "edits"):
        db.execute(f"DROP TABLE {table}")
        db.execute(f"ALTER TABLE {table}_prepared_new RENAME TO {table}")
    for _object_type, statement in authority_objects:
        db.execute(str(statement))

    _install_prepared_schema(db)
    db.execute(PREPARED_CURRENT_CELL_VALUES_SQL)
    visible_after = int(
        db.execute("SELECT COUNT(*) FROM current_cell_values").fetchone()[0]
    )
    if visible_after != visible_before:
        raise BundleSchemaMismatch(
            "prepared content migration changed the number of visible cells "
            f"({visible_after}, expected {visible_before})"
        )
    violations = db.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise BundleSchemaMismatch(
            "prepared content migration found invalid foreign-key relationships"
        )


PREPARED_CONTENT_STEP: SchemaStep = (
    PREPARED_CONTENT_FROM_DIGEST,
    PREPARED_CONTENT_TO_DIGEST,
    _widen_authorities,
)


__all__ = [
    "PREPARED_CONTENT_FROM_DIGEST",
    "PREPARED_CONTENT_TO_DIGEST",
    "PREPARED_CONTENT_STEP",
]
