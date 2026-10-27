"""Extend bounded text caching and index native numeric ranges without rewriting data."""

from __future__ import annotations

import sqlite3

from .current_cells import backfill_short_text_values
from .schema import (
    NUMERIC_CURRENT_INDEX_SQL,
    SCHEMA_DIGEST_META_KEY,
)

SHORT_TEXT_CACHE_FROM_DIGEST = "frisket.schema.v1:bd1859c234b8d13f81e7c64a694579a2"
SHORT_TEXT_CACHE_TO_DIGEST = "frisket.schema.v1:bba6b2017e73276ec859d32dc995b59a"


def migrate_short_text_cache(db: sqlite3.Connection) -> None:
    query = "SELECT value FROM meta WHERE key=?"
    row = db.execute(query, (SCHEMA_DIGEST_META_KEY,)).fetchone()
    if row is None or row[0] != SHORT_TEXT_CACHE_FROM_DIGEST:
        return
    db.execute("BEGIN IMMEDIATE")
    try:
        row = db.execute(query, (SCHEMA_DIGEST_META_KEY,)).fetchone()
        if row is not None and row[0] == SHORT_TEXT_CACHE_FROM_DIGEST:
            backfill_short_text_values(db)
            db.execute(NUMERIC_CURRENT_INDEX_SQL)
            db.execute(
                "UPDATE meta SET value=? WHERE key=?",
                (SHORT_TEXT_CACHE_TO_DIGEST, SCHEMA_DIGEST_META_KEY),
            )
        db.commit()
    except BaseException:
        db.rollback()
        raise
