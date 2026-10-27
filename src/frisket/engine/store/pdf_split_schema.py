"""Compact source checkpoints and replacement membership for PDF splits."""

PDF_SPLIT_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS import_session_pdf_sources (
  session_id TEXT NOT NULL REFERENCES import_sessions(id) ON DELETE CASCADE,
  ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
  source_id TEXT NOT NULL,
  sheet_id INTEGER,
  row_id INTEGER,
  column_id INTEGER,
  expected_value TEXT,
  child_count INTEGER NOT NULL CHECK (child_count > 0),
  first_position INTEGER NOT NULL CHECK (first_position > 0),
  state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','published','failed')),
  replaced INTEGER NOT NULL DEFAULT 0 CHECK (replaced IN (0,1)),
  error TEXT,
  PRIMARY KEY(session_id, ordinal),
  UNIQUE(session_id, source_id),
  UNIQUE(session_id, row_id),
  CHECK ((row_id IS NULL AND sheet_id IS NULL AND column_id IS NULL AND expected_value IS NULL)
      OR (row_id IS NOT NULL AND sheet_id IS NOT NULL AND column_id IS NOT NULL AND expected_value IS NOT NULL)),
  CHECK (replaced=0 OR (row_id IS NOT NULL AND state='published'))
);
"""
