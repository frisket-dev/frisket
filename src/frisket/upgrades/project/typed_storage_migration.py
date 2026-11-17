"""One-time migration from JSON cell payloads to native SQLite values."""

from __future__ import annotations
import sqlite3
from collections.abc import Iterator
from frisket.engine.store.schema import BundleSchemaMismatch
from frisket.upgrades.project.typed_storage_migration_schema import (
    TYPED_STORAGE_STATEMENTS,
)
from frisket.engine.store.value_codec import (
    SQLiteValue,
    _reject_non_json_constant,
    _encode_complex,
    encode_stored_value,
)
from frisket.engine.store.value_codec import repair_unicode_value
import json
from frisket.engine.store.citation_text import store_captured_text


TYPED_VALUES_TO_DIGEST = "frisket.schema.v1:529f5f4e750711b310535f6ed083c331"


def _typed_table_ddl(table: str, replacement: str) -> str:
    prefix = f"CREATE TABLE IF NOT EXISTS {table}"
    return TYPED_STORAGE_STATEMENTS[prefix].replace(
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

    def converted_rows() -> Iterator[list[object]]:
        nonlocal copied
        for row in cursor:
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
            copied += 1
            yield values

    db.executemany(insert_sql, converted_rows())
    expected = int(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    actual = int(db.execute(f"SELECT COUNT(*) FROM {replacement}").fetchone()[0])
    if copied != expected or actual != expected:
        raise BundleSchemaMismatch(
            f"typed storage migration copied {actual} of {expected} {table} rows"
        )
    return copied


def replace_payload_authorities(db: sqlite3.Connection) -> None:
    """Replace JSON payload copies with native typed authority values once."""
    current_count = int(db.execute("SELECT COUNT(*) FROM current_cells").fetchone()[0])
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
        db.execute(TYPED_STORAGE_STATEMENTS[f"CREATE TRIGGER IF NOT EXISTS {trigger}"])

    db.execute(_typed_table_ddl("current_cells", "current_cells"))
    db.execute("CREATE INDEX idx_current_cells_row ON current_cells(row_id,column_id)")
    db.execute(
        "CREATE UNIQUE INDEX idx_current_cells_column_row "
        "ON current_cells(column_id,row_id)"
    )
    from frisket.upgrades.project.historical_current_cells import (
        rebuild_current_cells,
    )

    rebuilt = rebuild_current_cells(db)
    if rebuilt != current_count:
        raise BundleSchemaMismatch(
            "typed storage migration rebuilt a different number of current "
            f"cells ({rebuilt}, expected {current_count})"
        )
    db.execute(
        TYPED_STORAGE_STATEMENTS["CREATE VIEW IF NOT EXISTS current_cell_values"]
    )
    from frisket.engine.store.citation_text import install_citation_text_schema

    install_citation_text_schema(db)
    migrate_legacy_citation_texts(db)
    visible = int(db.execute("SELECT COUNT(*) FROM current_cell_values").fetchone()[0])
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


def migrate_legacy_json_value(encoded: str | None) -> tuple[str, SQLiteValue]:
    """Convert one historical JSON payload without losing invalid JSON bytes.

    Valid arrays and objects retain their original serialized bytes unless
    ill-formed Unicode requires repair. Valid scalars move to native SQLite
    storage. SQL NULL historically decoded as an empty value and becomes an
    explicit null authority row; callers that use SQL NULL for an absent/error
    payload must handle that case before invoking this helper.
    """

    if encoded is None:
        return "null", None
    try:
        decoded = json.loads(encoded, parse_constant=_reject_non_json_constant)
        # Reject overflow-to-infinity and non-finite values nested in containers.
        json.dumps(decoded, allow_nan=False)
    except (TypeError, ValueError, OverflowError, RecursionError):
        return "legacy_invalid", encoded
    try:
        repaired = repair_unicode_value(decoded)
    except RecursionError:
        return "legacy_invalid", encoded
    if isinstance(repaired, (list, dict)):
        return "json", encoded if repaired == decoded else _encode_complex(repaired)
    return encode_stored_value(repaired)


def migrate_legacy_citation_texts(db: sqlite3.Connection) -> None:
    """Deduplicate legacy inline contexts without discarding saved history."""

    after_id = 0
    while True:
        rows = db.execute(
            "SELECT id,metadata FROM source_artifacts WHERE id>? ORDER BY id LIMIT 500",
            (after_id,),
        ).fetchall()
        if not rows:
            return
        for row in rows:
            after_id = int(row[0])
            try:
                metadata = json.loads(row[1])
            except (TypeError, ValueError):
                continue
            if not isinstance(metadata, dict) or not isinstance(
                metadata.get("captured_text"), str
            ):
                continue
            stored = store_captured_text(
                db,
                metadata=metadata,
                text=metadata["captured_text"],
                native=False,
            )
            db.execute(
                "UPDATE source_artifacts SET metadata=? WHERE id=?",
                (
                    json.dumps(
                        stored,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    int(row[0]),
                ),
            )
