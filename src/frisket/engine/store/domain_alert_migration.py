"""Preserve alert definitions/history while starting a coherent observation baseline."""

from __future__ import annotations

import sqlite3


def migrate_alert_baselines(db: sqlite3.Connection) -> None:
    """Called inside the domain cutover transaction, without committing it.

    Old seen rows were cumulative, not the latest successful membership. Keep
    definitions, check history and notifications, but establish a silent baseline
    at the next successful check. Reopening an already migrated project is inert.
    """
    columns = {row[1] for row in db.execute("PRAGMA table_info(watches)")}
    if "baseline_run_id" in columns:
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_watch_runs_notification_pending ON watch_runs(id) WHERE notification_pending=1"
        )
        return
    db.execute("ALTER TABLE watches ADD COLUMN baseline_run_id INTEGER")
    db.execute("ALTER TABLE watch_runs ADD COLUMN detection_policy TEXT")
    db.execute(
        "ALTER TABLE watch_runs ADD COLUMN notification_pending INTEGER NOT NULL DEFAULT 0"
    )
    db.execute("DELETE FROM watch_seen_rows")
    db.execute("DELETE FROM watch_row_field_state")

    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_watch_runs_notification_pending ON watch_runs(id) WHERE notification_pending=1"
    )
