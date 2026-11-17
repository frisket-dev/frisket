"""Add monotonic current-cell revisions for retrieval binding authority."""

from __future__ import annotations
import sqlite3
from frisket.upgrades.project.historical_current_cells import stamp_retrieval_revision
from frisket.upgrades.project.prepared_content_migration import (
    PREPARED_CONTENT_TO_DIGEST,
)
from frisket.engine.store.schema import BundleSchemaMismatch
from frisket.upgrades.project._step import SchemaStep
from frisket.engine.store.search_index_work import enqueue_dirty_scope


RETRIEVAL_LIFECYCLE_FROM_DIGEST = PREPARED_CONTENT_TO_DIGEST
RETRIEVAL_LIFECYCLE_TO_DIGEST = "frisket.schema.v1:4aade3b42272e8da098dabcf418502f8"


def _apply_retrieval_lifecycle(db: sqlite3.Connection) -> None:
    """Backfill one revision without changing values or historical authorities."""
    before = int(db.execute("SELECT COUNT(*) FROM current_cells").fetchone()[0])
    columns = {str(info[1]) for info in db.execute("PRAGMA table_info(current_cells)")}
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


RETRIEVAL_LIFECYCLE_STEP: SchemaStep = (
    RETRIEVAL_LIFECYCLE_FROM_DIGEST,
    RETRIEVAL_LIFECYCLE_TO_DIGEST,
    _apply_retrieval_lifecycle,
)


__all__ = [
    "RETRIEVAL_LIFECYCLE_FROM_DIGEST",
    "RETRIEVAL_LIFECYCLE_TO_DIGEST",
    "RETRIEVAL_LIFECYCLE_STEP",
]
