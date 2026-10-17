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
DOCUMENT_TEXT_TO_DIGEST = "frisket.schema.v1:099bb8f1ab7ff5230f6b6b3af36d7a32"

_IMMUTABLE_GENERATION_TRIGGER_SQL = """
CREATE TRIGGER trg_run_output_generations_immutable_declaration
BEFORE UPDATE ON run_output_generations
WHEN NEW.run_id IS NOT OLD.run_id
  OR NEW.column_id IS NOT OLD.column_id
  OR NEW.output_role IS NOT OLD.output_role
  OR NEW.compatibility_key IS NOT OLD.compatibility_key
  OR NEW.write_mode IS NOT OLD.write_mode
  OR NEW.claim_token IS NOT OLD.claim_token
  OR NEW.expected_base_run_id IS NOT OLD.expected_base_run_id
  OR NEW.document_text_authority IS NOT OLD.document_text_authority
  OR NEW.declared_at IS NOT OLD.declared_at
BEGIN
  SELECT RAISE(ABORT, 'output generation declaration is immutable');
END
"""


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
            db.execute(
                "DROP TRIGGER IF EXISTS "
                "trg_run_output_generations_immutable_declaration"
            )
            db.execute(_IMMUTABLE_GENERATION_TRIGGER_SQL)
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
