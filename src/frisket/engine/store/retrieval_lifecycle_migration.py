"""Add monotonic current-cell revisions for retrieval binding authority."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .current_cells import stamp_retrieval_revision
from .prepared_content_migration import PREPARED_CONTENT_TO_DIGEST
from .schema import BundleSchemaMismatch, SCHEMA_DIGEST_META_KEY
from .search_index_work import enqueue_dirty_scope

RETRIEVAL_LIFECYCLE_FROM_DIGEST = PREPARED_CONTENT_TO_DIGEST
RETRIEVAL_LIFECYCLE_TO_DIGEST = "frisket.schema.v1:4aade3b42272e8da098dabcf418502f8"


def migrate_retrieval_lifecycle(
    db: sqlite3.Connection, *, bundle_path: str | Path
) -> None:
    """Backfill one revision without changing values or historical authorities."""

    del bundle_path  # Kept for the uniform migration boundary and diagnostics API.
    query = "SELECT value FROM meta WHERE key=?"
    try:
        row = db.execute(query, (SCHEMA_DIGEST_META_KEY,)).fetchone()
    except sqlite3.DatabaseError:
        return
    if row is None or row[0] != RETRIEVAL_LIFECYCLE_FROM_DIGEST:
        return

    db.execute("BEGIN IMMEDIATE")
    try:
        row = db.execute(query, (SCHEMA_DIGEST_META_KEY,)).fetchone()
        if row is not None and row[0] == RETRIEVAL_LIFECYCLE_FROM_DIGEST:
            before = int(db.execute("SELECT COUNT(*) FROM current_cells").fetchone()[0])
            columns = {
                str(info[1]) for info in db.execute("PRAGMA table_info(current_cells)")
            }
            if "retrieval_revision" not in columns:
                db.execute(
                    "ALTER TABLE current_cells ADD COLUMN retrieval_revision INTEGER "
                    "NOT NULL DEFAULT 1 CHECK (retrieval_revision > 0)"
                )
            revision = enqueue_dirty_scope(db)
            stamp_retrieval_revision(db, revision)
            after = int(db.execute("SELECT COUNT(*) FROM current_cells").fetchone()[0])
            if after != before:
                raise BundleSchemaMismatch(
                    "retrieval lifecycle migration changed the number of current cells "
                    f"({after}, expected {before})"
                )
            invalid = int(
                db.execute(
                    "SELECT COUNT(*) FROM current_cells WHERE retrieval_revision<>?",
                    (revision,),
                ).fetchone()[0]
            )
            if invalid:
                raise BundleSchemaMismatch(
                    "retrieval lifecycle migration did not stamp every current cell"
                )
            db.execute(
                "UPDATE meta SET value=? WHERE key=?",
                (RETRIEVAL_LIFECYCLE_TO_DIGEST, SCHEMA_DIGEST_META_KEY),
            )
        db.commit()
    except BaseException:
        db.rollback()
        raise


__all__ = [
    "RETRIEVAL_LIFECYCLE_FROM_DIGEST",
    "RETRIEVAL_LIFECYCLE_TO_DIGEST",
    "migrate_retrieval_lifecycle",
]
