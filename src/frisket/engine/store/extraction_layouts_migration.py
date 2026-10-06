"""Add project-local extraction drafts without changing source or result data."""

from __future__ import annotations

import sqlite3

from .scalar_storage_migration import SCALAR_CURRENT_VALUES_TO_DIGEST
from .schema import SCHEMA_DIGEST_META_KEY


EXTRACTION_LAYOUTS_FROM_DIGEST = SCALAR_CURRENT_VALUES_TO_DIGEST
EXTRACTION_LAYOUTS_TO_DIGEST = "frisket.schema.v1:82e99299b4fe25eb46a92c8b0b56e265"

# Frozen at EXTRACTION_LAYOUTS_TO_DIGEST. Later changes to the live extraction
# layout schema belong in a later migration rather than changing this endpoint.
_EXTRACTION_LAYOUTS_MIGRATION_SQL = """
CREATE TABLE extraction_layouts (
  id INTEGER PRIMARY KEY,
  sheet_id INTEGER NOT NULL REFERENCES sheets(id) ON DELETE CASCADE,
  source_column_id INTEGER NOT NULL REFERENCES columns(id) ON DELETE CASCADE,
  ordinal INTEGER NOT NULL CHECK (ordinal > 0),
  reference_row_id INTEGER REFERENCES rows(id) ON DELETE SET NULL,
  draft TEXT NOT NULL CHECK (json_valid(draft)),
  repeat_group_id TEXT,
  has_applied INTEGER NOT NULL DEFAULT 0 CHECK (has_applied IN (0,1)),
  imported_recipe_id INTEGER UNIQUE,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE (sheet_id, source_column_id, ordinal)
);
CREATE TABLE extraction_layout_selection (
  sheet_id INTEGER NOT NULL REFERENCES sheets(id) ON DELETE CASCADE,
  source_column_id INTEGER NOT NULL REFERENCES columns(id) ON DELETE CASCADE,
  layout_id INTEGER NOT NULL REFERENCES extraction_layouts(id) ON DELETE CASCADE,
  PRIMARY KEY (sheet_id, source_column_id)
) WITHOUT ROWID;
CREATE TABLE extraction_layout_documents (
  source_column_id INTEGER NOT NULL REFERENCES columns(id) ON DELETE CASCADE,
  row_id INTEGER NOT NULL REFERENCES rows(id) ON DELETE CASCADE,
  layout_id INTEGER NOT NULL REFERENCES extraction_layouts(id) ON DELETE CASCADE,
  PRIMARY KEY (source_column_id, row_id)
) WITHOUT ROWID;
CREATE INDEX idx_extraction_layout_documents_layout
  ON extraction_layout_documents(layout_id, row_id);
"""


def migrate_extraction_layouts(db: sqlite3.Connection) -> None:
    query = "SELECT value FROM meta WHERE key=?"
    try:
        row = db.execute(query, (SCHEMA_DIGEST_META_KEY,)).fetchone()
    except sqlite3.DatabaseError:
        return
    if row is None or row[0] != EXTRACTION_LAYOUTS_FROM_DIGEST:
        return
    db.execute("BEGIN IMMEDIATE")
    try:
        row = db.execute(query, (SCHEMA_DIGEST_META_KEY,)).fetchone()
        if row is not None and row[0] == EXTRACTION_LAYOUTS_FROM_DIGEST:
            statement = ""
            for line in _EXTRACTION_LAYOUTS_MIGRATION_SQL.splitlines(keepends=True):
                statement += line
                if sqlite3.complete_statement(statement):
                    db.execute(statement)
                    statement = ""
            if statement.strip():
                raise RuntimeError("incomplete extraction layouts migration DDL")
            db.execute(
                "UPDATE meta SET value=? WHERE key=?",
                (EXTRACTION_LAYOUTS_TO_DIGEST, SCHEMA_DIGEST_META_KEY),
            )
        db.commit()
    except BaseException:
        db.rollback()
        raise
