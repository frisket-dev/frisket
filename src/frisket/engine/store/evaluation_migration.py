"""Add expected-answer metadata while preserving all ordinary source/result cells."""

from __future__ import annotations

import sqlite3

from .schema import SCHEMA_DIGEST_META_KEY

EVALUATION_FROM_DIGEST = "frisket.schema.v1:bba6b2017e73276ec859d32dc995b59a"
EVALUATION_TO_DIGEST = "frisket.schema.v1:bc273e27e46119ac1586031c26bf2d81"

# Frozen migration DDL; later schema edits belong to a subsequent migration.
_MIGRATION_SQL = "\nCREATE TABLE IF NOT EXISTS expected_answer_links (\n  id INTEGER PRIMARY KEY AUTOINCREMENT,\n  target_sheet_id INTEGER NOT NULL REFERENCES sheets(id) ON DELETE CASCADE,\n  expected_sheet_id INTEGER NOT NULL REFERENCES sheets(id) ON DELETE CASCADE,\n  action_id TEXT NOT NULL,\n  keys_json TEXT NOT NULL CHECK (json_valid(keys_json)),\n  fields_json TEXT NOT NULL CHECK (json_valid(fields_json)),\n  hidden INTEGER NOT NULL DEFAULT 0 CHECK (hidden IN (0,1))\n);\nCREATE INDEX IF NOT EXISTS idx_expected_answer_links_target\n  ON expected_answer_links(target_sheet_id, action_id);\nCREATE TABLE IF NOT EXISTS expected_answer_rules (\n  expected_column_id INTEGER PRIMARY KEY REFERENCES columns(id) ON DELETE CASCADE,\n  rule TEXT NOT NULL CHECK (rule IN ('exact','ignore_case','number','date','set','hand'))\n);\nCREATE TABLE IF NOT EXISTS evaluation_judgments (\n  sheet_id INTEGER NOT NULL REFERENCES sheets(id) ON DELETE CASCADE,\n  row_id INTEGER NOT NULL REFERENCES rows(id) ON DELETE CASCADE,\n  expected_column_id INTEGER NOT NULL REFERENCES columns(id) ON DELETE CASCADE,\n  output_column_id INTEGER NOT NULL REFERENCES columns(id) ON DELETE CASCADE,\n  source_fingerprint TEXT NOT NULL,\n  expected_hash TEXT NOT NULL,\n  answer_hash TEXT NOT NULL,\n  verdict TEXT NOT NULL CHECK (verdict IN ('match','miss')),\n  actor TEXT NOT NULL,\n  updated_at TEXT NOT NULL DEFAULT (datetime('now')),\n  PRIMARY KEY (sheet_id,row_id,expected_column_id,output_column_id,\n               source_fingerprint,expected_hash,answer_hash)\n) WITHOUT ROWID;\nCREATE TABLE IF NOT EXISTS saved_compare_variants (\n  id INTEGER PRIMARY KEY AUTOINCREMENT,\n  sheet_id INTEGER NOT NULL REFERENCES sheets(id) ON DELETE CASCADE,\n  action_id TEXT NOT NULL,\n  label TEXT NOT NULL,\n  output_columns_json TEXT NOT NULL CHECK (json_valid(output_columns_json)),\n  settings_json TEXT NOT NULL CHECK (json_valid(settings_json)),\n  rows_json TEXT NOT NULL CHECK (json_valid(rows_json)),\n  fingerprints_json TEXT NOT NULL CHECK (json_valid(fingerprints_json)),\n  kept INTEGER NOT NULL DEFAULT 0 CHECK (kept IN (0,1)),\n  saved_at TEXT NOT NULL DEFAULT (datetime('now'))\n);\nCREATE INDEX IF NOT EXISTS idx_saved_compare_variants_group\n  ON saved_compare_variants(sheet_id,action_id,id);\n"


def migrate_evaluation(db: sqlite3.Connection) -> None:
    query = "SELECT value FROM meta WHERE key=?"
    row = db.execute(query, (SCHEMA_DIGEST_META_KEY,)).fetchone()
    if row is None or row[0] != EVALUATION_FROM_DIGEST:
        return
    db.execute("BEGIN IMMEDIATE")
    try:
        row = db.execute(query, (SCHEMA_DIGEST_META_KEY,)).fetchone()
        if row is not None and row[0] == EVALUATION_FROM_DIGEST:
            statement = ""
            for line in _MIGRATION_SQL.splitlines(keepends=True):
                statement += line
                if sqlite3.complete_statement(statement):
                    db.execute(statement)
                    statement = ""
            if statement.strip():
                raise RuntimeError("Incomplete evaluation migration DDL")
            db.execute(
                "UPDATE meta SET value=? WHERE key=?",
                (EVALUATION_TO_DIGEST, SCHEMA_DIGEST_META_KEY),
            )
        db.commit()
    except BaseException:
        db.rollback()
        raise
