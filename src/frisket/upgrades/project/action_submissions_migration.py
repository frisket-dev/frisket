"""Add submitted action settings without changing execution or undo history."""

from __future__ import annotations
from frisket.upgrades.project._step import SchemaStep, ddl_statements


ACTION_SUBMISSIONS_FROM_DIGEST = "frisket.schema.v1:eacabe5ca733e513082aeeb74d2ad676"
ACTION_SUBMISSIONS_TO_DIGEST = "frisket.schema.v1:7e339a8995763aa40346e7d36187837d"

# Frozen migration DDL; later schema edits belong to a subsequent migration.
_MIGRATION_SQL = """
CREATE TABLE IF NOT EXISTS action_submissions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  action_id TEXT NOT NULL,
  sheet_id INTEGER,
  request_key TEXT,
  draft_hash TEXT NOT NULL,
  draft_json TEXT NOT NULL CHECK (json_valid(draft_json)),
  submitted_at TEXT NOT NULL DEFAULT (datetime('now')),
  receipt_id TEXT,
  run_id INTEGER,
  UNIQUE(request_key, draft_hash)
);
CREATE INDEX IF NOT EXISTS idx_action_submissions_action
  ON action_submissions(action_id, submitted_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_action_submissions_run ON action_submissions(run_id);
CREATE INDEX IF NOT EXISTS idx_runs_action_history
  ON runs(action_kind, started_at DESC, id DESC);
"""


ACTION_SUBMISSIONS_STEP: SchemaStep = (
    ACTION_SUBMISSIONS_FROM_DIGEST,
    ACTION_SUBMISSIONS_TO_DIGEST,
    ddl_statements(_MIGRATION_SQL),
)
