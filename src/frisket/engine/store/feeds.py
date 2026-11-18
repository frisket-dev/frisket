"""Feed catalog and check-history store (tables ``sources`` and ``source_runs``)."""

from __future__ import annotations

from frisket.engine.store.transactions import inserted_rowid
import json
import sqlite3
from typing import Any


class FeedStore:
    def __init__(self, project: Any):
        self.project = project

    @property
    def db(self) -> sqlite3.Connection:
        """The CALLING thread's connection, resolved per access — see
        ``RunResultStore.db``: capturing ``project.db`` in ``__init__`` hands
        every thread the CONSTRUCTING thread's connection, which races
        sqlite3's statement cache."""
        return self.project.db

    def feeds(self) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM sources ORDER BY created_at, id"
        ).fetchall()

    def get_feed(self, feed_id: int) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM sources WHERE id=?", (feed_id,)
        ).fetchone()

    def add_feed(
        self,
        name: str,
        kind: str = "url",
        url: str | None = None,
        config: dict[str, Any] | None = None,
        sheet_id: int | None = None,
        schedule: str | None = None,
        enabled: bool = True,
        commit: bool = True,
    ) -> int:
        cur = self.db.execute(
            "INSERT INTO sources (name, kind, url, config, sheet_id, schedule, "
            "enabled, last_status) VALUES (?,?,?,?,?,?,?, 'never')",
            (
                name,
                kind,
                url,
                json.dumps(config or {}),
                sheet_id,
                schedule,
                int(enabled),
            ),
        )
        if commit:
            self.db.commit()
        return inserted_rowid(cur)

    def update_feed(self, feed_id: int, *, commit: bool = True, **fields: Any) -> None:
        """Patch a subset of mutable source columns."""
        allowed = {
            "name",
            "kind",
            "url",
            "config",
            "sheet_id",
            "schedule",
            "enabled",
        }
        sets: list[str] = []
        vals: list[Any] = []
        for key, value in fields.items():
            if key not in allowed:
                raise ValueError(f"unknown feed field: {key}")
            if key == "config" and isinstance(value, (dict, list)):
                value = json.dumps(value)
            if key == "enabled":
                value = int(bool(value))
            sets.append(f"{key}=?")
            vals.append(value)
        if not sets:
            return
        vals.append(feed_id)
        self.db.execute(f"UPDATE sources SET {', '.join(sets)} WHERE id=?", vals)
        if commit:
            self.db.commit()

    def delete_feed(self, feed_id: int, *, commit: bool = True) -> None:
        self.db.execute(
            "DELETE FROM keyed_relationships WHERE id=?", (f"source:{feed_id}",)
        )
        self.db.execute("DELETE FROM sources WHERE id=?", (feed_id,))
        if commit:
            self.db.commit()

    def record_feed_check(
        self,
        feed_id: int,
        new_rows: int = 0,
        status: str = "ok",
        error: str | None = None,
        cursor: str | None = None,
        op_id: int | None = None,
        commit: bool = True,
    ) -> int:
        """Log one poll and advance the source monitoring state."""
        cur = self.db.execute(
            "INSERT INTO source_runs (source_id, op_id, status, new_rows, error, "
            "finished_at) VALUES (?,?,?,?,?, datetime('now'))",
            (feed_id, op_id, status, new_rows, error),
        )
        self.db.execute(
            "UPDATE sources SET last_checked_at=datetime('now'), "
            "last_status=?, new_rows_total=new_rows_total+?, "
            "cursor=COALESCE(?, cursor) WHERE id=?",
            (status, new_rows, cursor, feed_id),
        )
        if commit:
            self.db.commit()
        return inserted_rowid(cur)

    def start_feed_check(
        self,
        feed_id: int,
        *,
        cursor_before: str | None = None,
        commit: bool = True,
    ) -> int:
        cur = self.db.execute(
            "INSERT INTO source_runs (source_id, status, new_rows, "
            "cursor_before) VALUES (?, 'running', 0, ?)",
            (feed_id, cursor_before),
        )
        if commit:
            self.db.commit()
        return inserted_rowid(cur)

    def finish_feed_check(
        self,
        feed_check_id: int,
        *,
        status: str,
        receipt_id: str | None = None,
        op_id: int | None = None,
        new_rows: int = 0,
        skipped_rows: int = 0,
        changed_rows: int = 0,
        revisions: int = 0,
        error: str | None = None,
        cursor_after: str | None = None,
        duration_ms: int | None = None,
        warning_count: int = 0,
        cost_micro: int = 0,
        summary: dict[str, Any] | None = None,
        commit: bool = True,
    ) -> None:
        self.db.execute(
            "UPDATE source_runs SET receipt_id=?, op_id=?, status=?, "
            "new_rows=?, skipped_rows=?, changed_rows=?, revisions=?, error=?, "
            "cursor_after=COALESCE(?, cursor_after), duration_ms=?, "
            "warning_count=?, cost_micro=?, summary_json=?, "
            "finished_at=datetime('now') WHERE id=?",
            (
                receipt_id,
                op_id,
                status,
                int(new_rows),
                int(skipped_rows),
                int(changed_rows),
                int(revisions),
                error,
                cursor_after,
                duration_ms,
                int(warning_count),
                int(cost_micro),
                json.dumps(summary or {}, sort_keys=True),
                feed_check_id,
            ),
        )
        row = self.db.execute(
            "SELECT source_id FROM source_runs WHERE id=?",
            (feed_check_id,),
        ).fetchone()
        if row is not None:
            self.db.execute(
                "UPDATE sources SET last_checked_at=datetime('now'), "
                "last_status=?, new_rows_total=new_rows_total+?, "
                "cursor=COALESCE(?, cursor) WHERE id=?",
                (
                    "ok" if status == "ok" else "error",
                    int(new_rows) + int(revisions),
                    cursor_after,
                    int(row["source_id"]),
                ),
            )
        if commit:
            self.db.commit()

    def feed_checks_page(
        self,
        feed_id: int,
        offset: int = 0,
        limit: int = 50,
    ) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM source_runs WHERE source_id=? "
            "ORDER BY started_at DESC, id DESC LIMIT ? OFFSET ?",
            (feed_id, limit, offset),
        ).fetchall()

    def feed_checks_total(self, feed_id: int) -> int:
        row = self.db.execute(
            "SELECT COUNT(*) AS count FROM source_runs WHERE source_id=?",
            (feed_id,),
        ).fetchone()
        return int(row["count"] if row else 0)

    def latest_feed_check(self, feed_id: int) -> sqlite3.Row | None:
        rows = self.feed_checks_page(feed_id, offset=0, limit=1)
        return rows[0] if rows else None

    def latest_feed_check_with_status(
        self, feed_id: int, status: str
    ) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM source_runs WHERE source_id=? AND status=? "
            "ORDER BY started_at DESC, id DESC LIMIT 1",
            (feed_id, status),
        ).fetchone()

    def feed_check_statuses_desc(self, feed_id: int) -> list[str]:
        return [
            str(row["status"])
            for row in self.db.execute(
                "SELECT status FROM source_runs WHERE source_id=? "
                "ORDER BY started_at DESC, id DESC",
                (feed_id,),
            )
        ]

    def latest_feed_check_after(self, feed_id: int, run_id: int) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM source_runs WHERE source_id=? AND id>? "
            "ORDER BY id DESC LIMIT 1",
            (feed_id, run_id),
        ).fetchone()
