"""Source catalog and polling-history store."""

from __future__ import annotations

import json
import sqlite3
from typing import Any


class SourceStore:
    def __init__(self, project: Any):
        self.project = project

    @property
    def db(self) -> sqlite3.Connection:
        """The CALLING thread's connection, resolved per access — see
        ``RunResultStore.db``: capturing ``project.db`` in ``__init__`` hands
        every thread the CONSTRUCTING thread's connection, which races
        sqlite3's statement cache."""
        return self.project.db

    def sources(self) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM sources ORDER BY created_at, id"
        ).fetchall()

    def get_source(self, source_id: int) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM sources WHERE id=?", (source_id,)
        ).fetchone()

    def add_source(
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
        return int(cur.lastrowid)

    def update_source(
        self, source_id: int, *, commit: bool = True, **fields: Any
    ) -> None:
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
                raise ValueError(f"unknown source field: {key}")
            if key == "config" and isinstance(value, (dict, list)):
                value = json.dumps(value)
            if key == "enabled":
                value = int(bool(value))
            sets.append(f"{key}=?")
            vals.append(value)
        if not sets:
            return
        vals.append(source_id)
        self.db.execute(f"UPDATE sources SET {', '.join(sets)} WHERE id=?", vals)
        if commit:
            self.db.commit()

    def delete_source(self, source_id: int, *, commit: bool = True) -> None:
        self.db.execute("DELETE FROM sources WHERE id=?", (source_id,))
        if commit:
            self.db.commit()

    def record_source_run(
        self,
        source_id: int,
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
            (source_id, op_id, status, new_rows, error),
        )
        self.db.execute(
            "UPDATE sources SET last_checked_at=datetime('now'), "
            "last_status=?, new_rows_total=new_rows_total+?, "
            "cursor=COALESCE(?, cursor) WHERE id=?",
            (status, new_rows, cursor, source_id),
        )
        if commit:
            self.db.commit()
        return int(cur.lastrowid)

    def start_source_run(
        self,
        source_id: int,
        *,
        cursor_before: str | None = None,
        commit: bool = True,
    ) -> int:
        cur = self.db.execute(
            "INSERT INTO source_runs (source_id, status, new_rows, "
            "cursor_before) VALUES (?, 'running', 0, ?)",
            (source_id, cursor_before),
        )
        if commit:
            self.db.commit()
        return int(cur.lastrowid)

    def finish_source_run(
        self,
        source_run_id: int,
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
                source_run_id,
            ),
        )
        row = self.db.execute(
            "SELECT source_id FROM source_runs WHERE id=?",
            (source_run_id,),
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

    def source_item_for_dedupe(
        self,
        source_id: int,
        dedupe_key: str,
    ) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM source_items WHERE source_id=? AND dedupe_key=?",
            (source_id, dedupe_key),
        ).fetchone()

    def upsert_source_item(
        self,
        *,
        source_id: int,
        source_item_id: str,
        dedupe_key: str,
        item_hash: str,
        row_id: int | None,
        source_run_id: int,
        revision: int | None = None,
        raw_ref: dict[str, Any] | None = None,
        commit: bool = True,
    ) -> None:
        """Write the ledger entry for one (source_id, dedupe_key).

        The database decides whether this is an insert or an update, via the
        real ``UNIQUE(source_id, dedupe_key)`` constraint. A prior SELECT would
        only be advisory: two calls for the same key inside one transaction --
        or two writers racing -- can both read "absent" and both insert. Here a
        second row for a key is unrepresentable; the conflict resolves to the
        update arm. ``first_seen_run_id`` is deliberately absent from the
        update arm, so it keeps recording the first sighting.
        """
        self.db.execute(
            "INSERT INTO source_items (source_id, source_item_id, dedupe_key, "
            "item_hash, row_id, first_seen_run_id, last_seen_run_id, "
            "revision, raw_ref_json) "
            "VALUES (:source_id, :source_item_id, :dedupe_key, :item_hash, "
            ":row_id, :source_run_id, :source_run_id, "
            "COALESCE(:revision, 0), :raw_ref_json) "
            "ON CONFLICT(source_id, dedupe_key) DO UPDATE SET "
            "source_item_id=excluded.source_item_id, "
            "item_hash=excluded.item_hash, "
            "row_id=excluded.row_id, "
            "last_seen_run_id=excluded.last_seen_run_id, "
            # An omitted revision means "leave the ledger's count alone", which
            # excluded.revision (already COALESCEd to 0) cannot express.
            "revision=COALESCE(:revision, source_items.revision), "
            "raw_ref_json=excluded.raw_ref_json, "
            "updated_at=datetime('now')",
            {
                "source_id": source_id,
                "source_item_id": source_item_id,
                "dedupe_key": dedupe_key,
                "item_hash": item_hash,
                "row_id": row_id,
                "source_run_id": source_run_id,
                "revision": None if revision is None else int(revision),
                "raw_ref_json": json.dumps(raw_ref or {}, sort_keys=True),
            },
        )
        if commit:
            self.db.commit()

    def source_runs(self, source_id: int, limit: int = 50) -> list[sqlite3.Row]:
        return self.source_runs_page(source_id, offset=0, limit=limit)

    def source_runs_page(
        self,
        source_id: int,
        offset: int = 0,
        limit: int = 50,
    ) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM source_runs WHERE source_id=? "
            "ORDER BY started_at DESC, id DESC LIMIT ? OFFSET ?",
            (source_id, limit, offset),
        ).fetchall()

    def source_runs_total(self, source_id: int) -> int:
        row = self.db.execute(
            "SELECT COUNT(*) AS count FROM source_runs WHERE source_id=?",
            (source_id,),
        ).fetchone()
        return int(row["count"] if row else 0)

    def latest_source_run(self, source_id: int) -> sqlite3.Row | None:
        rows = self.source_runs_page(source_id, offset=0, limit=1)
        return rows[0] if rows else None

    def latest_source_run_with_status(
        self, source_id: int, status: str
    ) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM source_runs WHERE source_id=? AND status=? "
            "ORDER BY started_at DESC, id DESC LIMIT 1",
            (source_id, status),
        ).fetchone()

    def source_run_statuses_desc(self, source_id: int) -> list[str]:
        return [
            str(row["status"])
            for row in self.db.execute(
                "SELECT status FROM source_runs WHERE source_id=? "
                "ORDER BY started_at DESC, id DESC",
                (source_id,),
            )
        ]

    def latest_source_run_after(
        self, source_id: int, run_id: int
    ) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM source_runs WHERE source_id=? AND id>? "
            "ORDER BY id DESC LIMIT 1",
            (source_id, run_id),
        ).fetchone()
