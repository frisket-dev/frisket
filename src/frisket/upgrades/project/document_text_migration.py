"""Add designated document-text roles and sparse manual provenance pins."""

from __future__ import annotations
import sqlite3
from frisket.engine.store.document_text import (
    DOCUMENT_TEXT_SCHEMA_SQL,
    LEGACY_DOCUMENT_TEXT_SELECTION_VIEW_SQL,
)
from frisket.upgrades.project.execution_metadata_migration import (
    EXECUTION_METADATA_TO_DIGEST,
)
from frisket.upgrades.project._step import SchemaStep


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


def _apply_document_text(db: sqlite3.Connection) -> None:
    """Install empty role metadata without guessing from names or recency."""
    columns = {
        str(info[1]) for info in db.execute("PRAGMA table_info(run_output_generations)")
    }
    if "document_text_authority" not in columns:
        db.execute(
            "ALTER TABLE run_output_generations ADD COLUMN "
            "document_text_authority INTEGER NOT NULL DEFAULT 0 "
            "CHECK (document_text_authority IN (0,1))"
        )
    db.execute(
        "DROP TRIGGER IF EXISTS trg_run_output_generations_immutable_declaration"
    )
    db.execute(_IMMUTABLE_GENERATION_TRIGGER_SQL)
    for statement in DOCUMENT_TEXT_SCHEMA_SQL.split(";"):
        if statement.strip():
            db.execute(statement)
    db.execute(LEGACY_DOCUMENT_TEXT_SELECTION_VIEW_SQL)


DOCUMENT_TEXT_STEP: SchemaStep = (
    DOCUMENT_TEXT_FROM_DIGEST,
    DOCUMENT_TEXT_TO_DIGEST,
    _apply_document_text,
)


__all__ = [
    "DOCUMENT_TEXT_FROM_DIGEST",
    "DOCUMENT_TEXT_TO_DIGEST",
    "DOCUMENT_TEXT_STEP",
]
