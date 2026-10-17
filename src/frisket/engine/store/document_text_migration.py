"""Add designated document-text roles and sparse manual provenance pins."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .document_text import (
    DOCUMENT_TEXT_SCHEMA_SQL,
    DOCUMENT_TEXT_SELECTION_VIEW_SQL,
)
from .execution_metadata_migration import EXECUTION_METADATA_TO_DIGEST
from .schema import SCHEMA_DIGEST_META_KEY


DOCUMENT_TEXT_FROM_DIGEST = EXECUTION_METADATA_TO_DIGEST
DOCUMENT_TEXT_TO_DIGEST = "frisket.schema.v1:4f9371ba63bfc63818dba067d8e92e88"


def migrate_document_text(
    db: sqlite3.Connection, *, bundle_path: str | Path
) -> None:
    """Install empty role metadata without guessing from names or recency."""

    del bundle_path
    query = "SELECT value FROM meta WHERE key=?"
    try:
        row = db.execute(query, (SCHEMA_DIGEST_META_KEY,)).fetchone()
    except sqlite3.DatabaseError:
        return
    if row is None or row[0] != DOCUMENT_TEXT_FROM_DIGEST:
        return

    db.execute("BEGIN IMMEDIATE")
    try:
        row = db.execute(query, (SCHEMA_DIGEST_META_KEY,)).fetchone()
        if row is not None and row[0] == DOCUMENT_TEXT_FROM_DIGEST:
            columns = {
                str(info[1])
                for info in db.execute("PRAGMA table_info(run_output_generations)")
            }
            if "document_text_authority" not in columns:
                db.execute(
                    "ALTER TABLE run_output_generations ADD COLUMN "
                    "document_text_authority INTEGER NOT NULL DEFAULT 0 "
                    "CHECK (document_text_authority IN (0,1))"
                )
            for statement in DOCUMENT_TEXT_SCHEMA_SQL.split(";"):
                if statement.strip():
                    db.execute(statement)
            db.execute(DOCUMENT_TEXT_SELECTION_VIEW_SQL)
            db.execute(
                "UPDATE meta SET value=? WHERE key=?",
                (DOCUMENT_TEXT_TO_DIGEST, SCHEMA_DIGEST_META_KEY),
            )
        db.commit()
    except BaseException:
        db.rollback()
        raise


__all__ = [
    "DOCUMENT_TEXT_FROM_DIGEST",
    "DOCUMENT_TEXT_TO_DIGEST",
    "migrate_document_text",
]
