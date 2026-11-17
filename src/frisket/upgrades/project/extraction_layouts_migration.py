"""Add project-local extraction drafts without changing source or result data."""

from __future__ import annotations
from frisket.upgrades.project.scalar_storage_migration import (
    SCALAR_CURRENT_VALUES_TO_DIGEST,
)
from frisket.upgrades.project._step import SchemaStep, ddl_statements


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


EXTRACTION_LAYOUTS_STEP: SchemaStep = (
    EXTRACTION_LAYOUTS_FROM_DIGEST,
    EXTRACTION_LAYOUTS_TO_DIGEST,
    ddl_statements(_EXTRACTION_LAYOUTS_MIGRATION_SQL),
)
