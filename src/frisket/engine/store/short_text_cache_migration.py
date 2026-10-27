"""Extend bounded text caching and index native numeric ranges without rewriting data."""

from __future__ import annotations

import sqlite3

from .schema import (
    NUMERIC_CURRENT_INDEX_SQL,
    SCALAR_CURRENT_CELL_VALUES_SQL,
    SCHEMA_DIGEST_META_KEY,
)

SHORT_TEXT_CACHE_FROM_DIGEST = "frisket.schema.v1:887874773c58ac58982913e6604d9ec9"
SHORT_TEXT_CACHE_TO_DIGEST = "frisket.schema.v1:b8cb89527b0bf56f254ad46ec9966727"


def migrate_short_text_cache(db: sqlite3.Connection) -> None:
    query = "SELECT value FROM meta WHERE key=?"
    row = db.execute(query, (SCHEMA_DIGEST_META_KEY,)).fetchone()
    if row is None or row[0] != SHORT_TEXT_CACHE_FROM_DIGEST:
        return
    db.execute("BEGIN IMMEDIATE")
    try:
        row = db.execute(query, (SCHEMA_DIGEST_META_KEY,)).fetchone()
        if row is not None and row[0] == SHORT_TEXT_CACHE_FROM_DIGEST:
            # This pinned scalar view returns raw refs, not expanded document
            # text. Prepared documents must keep both their locator and citation.
            raw_values = "SELECT" + SCALAR_CURRENT_CELL_VALUES_SQL.split("SELECT", 1)[1]
            db.execute(
                f"WITH raw_values AS ({raw_values}), eligible AS ("
                "SELECT live.column_id,live.row_id,live.value_kind,live.value "
                "FROM raw_values live JOIN columns descriptor ON descriptor.id=live.column_id "
                "WHERE live.value_kind='text' AND descriptor.type IN ('text','category','date') "
                "AND length(CAST(live.value AS BLOB))<=512) "
                "UPDATE current_cells AS head SET inline_value_kind=eligible.value_kind,"
                "inline_value=eligible.value FROM eligible "
                "WHERE head.column_id=eligible.column_id AND head.row_id=eligible.row_id "
                "AND head.inline_value_kind IS NULL"
            )
            db.execute(NUMERIC_CURRENT_INDEX_SQL)
            db.execute(
                "UPDATE meta SET value=? WHERE key=?",
                (SHORT_TEXT_CACHE_TO_DIGEST, SCHEMA_DIGEST_META_KEY),
            )
        db.commit()
    except BaseException:
        db.rollback()
        raise
