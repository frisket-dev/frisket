"""Project-owned submitted settings. Execution remains owned by runs/receipts."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any


ACTION_SUBMISSIONS_SCHEMA_SQL = """
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


def record_submission(
    db: sqlite3.Connection, draft: dict[str, Any], *, request_key: str | None
) -> int:
    encoded = json.dumps(draft, sort_keys=True, separators=(",", ":"), allow_nan=False)
    digest = hashlib.sha256(encoded.encode()).hexdigest()
    with db:
        db.execute(
            "INSERT INTO action_submissions "
            "(action_id,sheet_id,request_key,draft_hash,draft_json) VALUES (?,?,?,?,?) "
            "ON CONFLICT(request_key,draft_hash) DO NOTHING",
            (
                draft["action_id"],
                draft["scope"].get("sheet_id"),
                request_key,
                digest,
                encoded,
            ),
        )
        if request_key is None:
            return int(db.execute("SELECT last_insert_rowid()").fetchone()[0])
        return int(
            db.execute(
                "SELECT id FROM action_submissions WHERE request_key=? AND draft_hash=?",
                (request_key, digest),
            ).fetchone()[0]
        )


def link_submission(
    db: sqlite3.Connection,
    submission_id: int,
    *,
    receipt_id: str | None,
    run_id: int | None,
) -> None:
    with db:
        db.execute(
            "UPDATE action_submissions SET receipt_id=COALESCE(?,receipt_id), "
            "run_id=COALESCE(?,run_id) WHERE id=?",
            (receipt_id, run_id, submission_id),
        )
