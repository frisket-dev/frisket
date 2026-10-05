"""Add the ordered result index used by confidence review pages."""

from __future__ import annotations

import sqlite3

from .review_stats_migration import REVIEW_STATS_TO_DIGEST
from .schema import SCHEMA_DIGEST_META_KEY


REVIEW_CONFIDENCE_FROM_DIGEST = REVIEW_STATS_TO_DIGEST
REVIEW_CONFIDENCE_TO_DIGEST = "frisket.schema.v1:7a0c04a6a0810573f0fa6253759a87a5"


def migrate_review_confidence(db: sqlite3.Connection) -> None:
    """Install the confidence candidate index from the exact prior schema."""

    query = "SELECT value FROM meta WHERE key=?"
    try:
        row = db.execute(query, (SCHEMA_DIGEST_META_KEY,)).fetchone()
    except sqlite3.DatabaseError:
        return
    if row is None or row[0] != REVIEW_CONFIDENCE_FROM_DIGEST:
        return

    db.execute("BEGIN IMMEDIATE")
    try:
        row = db.execute(query, (SCHEMA_DIGEST_META_KEY,)).fetchone()
        if row is not None and row[0] == REVIEW_CONFIDENCE_FROM_DIGEST:
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_results_run_col_confidence_row "
                "ON results(run_id,column_id,confidence,row_id)"
            )
            db.execute(
                "UPDATE meta SET value=? WHERE key=?",
                (REVIEW_CONFIDENCE_TO_DIGEST, SCHEMA_DIGEST_META_KEY),
            )
        db.commit()
    except BaseException:
        db.rollback()
        raise


__all__ = [
    "REVIEW_CONFIDENCE_FROM_DIGEST",
    "REVIEW_CONFIDENCE_TO_DIGEST",
    "migrate_review_confidence",
]
