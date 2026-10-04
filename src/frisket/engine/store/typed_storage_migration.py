"""One-time migration from JSON cell payloads to native SQLite values."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .disk_capacity import require_disk_headroom
from .schema import BundleSchemaMismatch, SCHEMA_DIGEST_META_KEY
from .value_codec import migrate_legacy_json_value

TYPED_VALUES_TO_DIGEST = "frisket.schema.v1:ee2a5eb7c201829acb1c0ff01e371558"
TYPED_VALUE_COPY_BATCH_SIZE = 2_000


def _fresh_schema_statement(prefix: str) -> str:
    """Return one complete statement from the current fresh-bundle schema."""

    from .schema import SCHEMA

    start = SCHEMA.index(prefix)
    statement = ""
    for line in SCHEMA[start:].splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            return statement.strip()
    raise RuntimeError(f"incomplete schema statement starting with {prefix!r}")


def _typed_table_ddl(table: str, replacement: str) -> str:
    prefix = f"CREATE TABLE IF NOT EXISTS {table}"
    return _fresh_schema_statement(prefix).replace(
        prefix, f"CREATE TABLE {replacement}", 1
    )


def _copy_typed_authority(
    db: sqlite3.Connection, *, table: str, replacement: str
) -> int:
    """Copy and decode one legacy JSON authority in bounded batches."""

    columns = [str(row[1]) for row in db.execute(f"PRAGMA table_info({table})")]
    value_index = columns.index("value")
    result_effect_index = (
        columns.index("publication_effect") if table == "results" else None
    )
    result_error_index = columns.index("error") if table == "results" else None
    destination_columns = [*columns[:value_index], "value_kind", *columns[value_index:]]
    placeholders = ",".join("?" for _ in destination_columns)
    insert_sql = (
        f"INSERT INTO {replacement} ({','.join(destination_columns)}) "
        f"VALUES ({placeholders})"
    )
    copied = 0
    cursor = db.execute(f"SELECT {','.join(columns)} FROM {table}")
    while batch := cursor.fetchmany(TYPED_VALUE_COPY_BATCH_SIZE):
        converted = []
        for row in batch:
            values = list(row)
            raw_value = values[value_index]
            if table == "results":
                effect = values[result_effect_index]  # type: ignore[index]
                error = values[result_error_index]  # type: ignore[index]
                if effect == "publish_error" or (
                    raw_value is None and error is not None
                ):
                    value_kind, stored_value = None, None
                elif effect == "publish_null":
                    value_kind, stored_value = "null", None
                else:
                    value_kind, stored_value = migrate_legacy_json_value(raw_value)
            else:
                value_kind, stored_value = migrate_legacy_json_value(raw_value)
            values[value_index] = stored_value
            values.insert(value_index, value_kind)
            converted.append(values)
        db.executemany(insert_sql, converted)
        copied += len(converted)
    expected = int(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    actual = int(db.execute(f"SELECT COUNT(*) FROM {replacement}").fetchone()[0])
    if copied != expected or actual != expected:
        raise BundleSchemaMismatch(
            f"typed storage migration copied {actual} of {expected} {table} rows"
        )
    return copied


def migrate_typed_values(
    db: sqlite3.Connection, *, bundle_path: str | Path, from_digest: str
) -> None:
    """Replace JSON payload copies with native typed authority values once."""

    query = "SELECT value FROM meta WHERE key=?"
    try:
        row = db.execute(query, (SCHEMA_DIGEST_META_KEY,)).fetchone()
    except sqlite3.DatabaseError:
        return
    if row is None or row[0] != from_digest:
        return

    page_count = int(db.execute("PRAGMA page_count").fetchone()[0])
    page_size = int(db.execute("PRAGMA page_size").fetchone()[0])
    # One old+new authority copy plus rollback journal/WAL is the conservative
    # peak. The projection is dropped first inside the transaction so SQLite
    # can reuse its pages, but the preflight does not rely on that saving.
    require_disk_headroom(
        Path(bundle_path).parent,
        2 * page_count * page_size,
    )

    foreign_keys = int(db.execute("PRAGMA foreign_keys").fetchone()[0])
    legacy_alter = int(db.execute("PRAGMA legacy_alter_table").fetchone()[0])
    db.execute("PRAGMA foreign_keys=OFF")
    db.execute("PRAGMA legacy_alter_table=ON")
    try:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(query, (SCHEMA_DIGEST_META_KEY,)).fetchone()
        if row is not None and row[0] == from_digest:
            current_count = int(
                db.execute("SELECT COUNT(*) FROM current_cells").fetchone()[0]
            )
            # Release the duplicated payload projection before allocating typed
            # authority pages. It is rebuildable from the three authorities.
            db.execute("DROP TABLE current_cells")

            authority_objects = db.execute(
                "SELECT type,sql FROM sqlite_master "
                "WHERE tbl_name IN ('cells','results','edits') "
                "AND type IN ('index','trigger') AND sql IS NOT NULL "
                "ORDER BY CASE type WHEN 'index' THEN 0 ELSE 1 END,name"
            ).fetchall()
            for table in ("cells", "results", "edits"):
                replacement = f"{table}_typed_new"
                db.execute(_typed_table_ddl(table, replacement))
                _copy_typed_authority(db, table=table, replacement=replacement)

            for table in ("cells", "results", "edits"):
                db.execute(f"DROP TABLE {table}")
                db.execute(f"ALTER TABLE {table}_typed_new RENAME TO {table}")
            for _object_type, statement in authority_objects:
                db.execute(str(statement))
            # These two historical triggers enumerate semantic payload fields;
            # reinstall their typed definitions so value_kind cannot change
            # independently of a published value.
            for trigger in (
                "trg_results_semantic_update_open_generation",
                "trg_results_published_semantics_immutable",
            ):
                db.execute(f"DROP TRIGGER {trigger}")
                db.execute(
                    _fresh_schema_statement(f"CREATE TRIGGER IF NOT EXISTS {trigger}")
                )

            db.execute(_typed_table_ddl("current_cells", "current_cells"))
            db.execute(
                "CREATE INDEX idx_current_cells_row ON current_cells(row_id,column_id)"
            )
            db.execute(
                "CREATE UNIQUE INDEX idx_current_cells_column_row "
                "ON current_cells(column_id,row_id)"
            )
            from .current_cells import rebuild_current_cells

            rebuilt = rebuild_current_cells(db)
            if rebuilt != current_count:
                raise BundleSchemaMismatch(
                    "typed storage migration rebuilt a different number of current "
                    f"cells ({rebuilt}, expected {current_count})"
                )
            db.execute(
                _fresh_schema_statement("CREATE VIEW IF NOT EXISTS current_cell_values")
            )
            from .citation_text import (
                install_citation_text_schema,
                migrate_legacy_citation_texts,
            )

            install_citation_text_schema(db)
            migrate_legacy_citation_texts(db)
            visible = int(
                db.execute("SELECT COUNT(*) FROM current_cell_values").fetchone()[0]
            )
            if visible != current_count:
                raise BundleSchemaMismatch(
                    "typed storage migration left unresolved current-cell heads "
                    f"({visible}, expected {current_count})"
                )
            violations = db.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise BundleSchemaMismatch(
                    "typed storage migration found invalid foreign-key relationships"
                )
            db.execute(
                "UPDATE meta SET value=? WHERE key=?",
                (TYPED_VALUES_TO_DIGEST, SCHEMA_DIGEST_META_KEY),
            )
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.execute(f"PRAGMA legacy_alter_table={legacy_alter}")
        db.execute(f"PRAGMA foreign_keys={foreign_keys}")
