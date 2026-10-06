"""Add immutable prepared-page content and its internal value reference kind."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .disk_capacity import require_disk_headroom
from .prepared_content_migration_schema import (
    PREPARED_AUTHORITY_TABLE_SQL,
    PREPARED_CONTENT_SCHEMA_SQL,
    PREPARED_CURRENT_CELL_VALUES_SQL,
)
from .schema import BundleSchemaMismatch, SCHEMA_DIGEST_META_KEY
from .extraction_layouts_migration import EXTRACTION_LAYOUTS_TO_DIGEST


PREPARED_CONTENT_FROM_DIGEST = EXTRACTION_LAYOUTS_TO_DIGEST
# Pinned to this migration's fresh-schema DDL.
PREPARED_CONTENT_TO_DIGEST = "frisket.schema.v1:59e024b9950ba4cc2232bd1bc42ae688"


def _install_prepared_schema(db: sqlite3.Connection) -> None:
    for statement in PREPARED_CONTENT_SCHEMA_SQL:
        db.execute(statement)


def migrate_prepared_content(
    db: sqlite3.Connection, *, bundle_path: str | Path
) -> None:
    """Widen typed authorities and install prepared content atomically."""

    query = "SELECT value FROM meta WHERE key=?"
    try:
        row = db.execute(query, (SCHEMA_DIGEST_META_KEY,)).fetchone()
    except sqlite3.DatabaseError:
        return
    if row is None or row[0] != PREPARED_CONTENT_FROM_DIGEST:
        return

    page_count = int(db.execute("PRAGMA page_count").fetchone()[0])
    page_size = int(db.execute("PRAGMA page_size").fetchone()[0])
    require_disk_headroom(Path(bundle_path).parent, 2 * page_count * page_size)

    foreign_keys = int(db.execute("PRAGMA foreign_keys").fetchone()[0])
    legacy_alter = int(db.execute("PRAGMA legacy_alter_table").fetchone()[0])
    db.execute("PRAGMA foreign_keys=OFF")
    db.execute("PRAGMA legacy_alter_table=ON")
    try:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(query, (SCHEMA_DIGEST_META_KEY,)).fetchone()
        if row is not None and row[0] == PREPARED_CONTENT_FROM_DIGEST:
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
                columns = [
                    str(info[1]) for info in db.execute(f"PRAGMA table_info({table})")
                ]
                db.execute(PREPARED_AUTHORITY_TABLE_SQL[table])
                joined = ",".join(columns)
                db.execute(
                    f"INSERT INTO {table}_prepared_new({joined}) "
                    f"SELECT {joined} FROM {table}"
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
            db.execute(
                "UPDATE meta SET value=? WHERE key=?",
                (PREPARED_CONTENT_TO_DIGEST, SCHEMA_DIGEST_META_KEY),
            )
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.execute(f"PRAGMA legacy_alter_table={legacy_alter}")
        db.execute(f"PRAGMA foreign_keys={foreign_keys}")


__all__ = [
    "PREPARED_CONTENT_FROM_DIGEST",
    "PREPARED_CONTENT_TO_DIGEST",
    "migrate_prepared_content",
]
