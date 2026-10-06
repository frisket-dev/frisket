"""Add immutable prepared-page content and its internal value reference kind."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .disk_capacity import require_disk_headroom
from .schema import BundleSchemaMismatch, SCHEMA_DIGEST_META_KEY
from .scalar_storage_migration import SCALAR_CURRENT_VALUES_TO_DIGEST


PREPARED_CONTENT_FROM_DIGEST = SCALAR_CURRENT_VALUES_TO_DIGEST
# Pinned to this migration's fresh-schema DDL.
PREPARED_CONTENT_TO_DIGEST = "frisket.schema.v1:06ac9904c47e80559033f8faddc6ae10"


def _fresh_schema_statement(prefix: str) -> str:
    from .schema import SCHEMA

    start = SCHEMA.index(prefix)
    statement = ""
    for line in SCHEMA[start:].splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            return statement.strip()
    raise RuntimeError(f"incomplete schema statement starting with {prefix!r}")


def _replacement_ddl(table: str) -> str:
    prefix = f"CREATE TABLE IF NOT EXISTS {table}"
    return _fresh_schema_statement(prefix).replace(
        prefix, f"CREATE TABLE {table}_prepared_new", 1
    )


def _install_prepared_schema(db: sqlite3.Connection) -> None:
    for prefix in (
        "CREATE TABLE IF NOT EXISTS prepared_page_versions",
        "CREATE INDEX IF NOT EXISTS idx_prepared_page_versions_artifact_page",
        "CREATE TABLE IF NOT EXISTS prepared_content_sets",
        "CREATE TABLE IF NOT EXISTS prepared_content_set_pages",
        "CREATE INDEX IF NOT EXISTS idx_prepared_content_set_pages_version",
        "CREATE TABLE IF NOT EXISTS prepared_content_refs",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_prepared_content_refs_document",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_prepared_content_refs_page",
        "CREATE VIEW IF NOT EXISTS prepared_content_ref_values",
    ):
        db.execute(_fresh_schema_statement(prefix))


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
                db.execute(_replacement_ddl(table))
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
            db.execute(
                _fresh_schema_statement("CREATE VIEW IF NOT EXISTS current_cell_values")
            )
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
