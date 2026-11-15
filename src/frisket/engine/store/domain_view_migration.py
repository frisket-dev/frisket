"""One-time unification of saved Views, lenses and alert query definitions.

Run inside the domain migration transaction, before fresh schema installation.
Nothing is evaluated and no model/index preparation runs during conversion.
"""

from __future__ import annotations

import json
from typing import Any

from frisket.engine.views.specs import legacy_selection


class _Definitions:
    def __init__(self, db: Any):
        self.db = db

    def columns(self, sheet_id: int, include_hidden: bool = False):
        return self.db.execute(
            "SELECT * FROM columns WHERE sheet_id=?", (sheet_id,)
        ).fetchall()


def _convert(project: Any, row: Any, *, query: Any = None) -> str:
    raw = query if query is not None else row["spec"]
    try:
        spec = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(spec, dict):
            raise ValueError("Saved definition is not an object")
        if "selection" in spec:
            return json.dumps(spec, sort_keys=True)
        selection = legacy_selection(project, sheet_id=row["sheet_id"], spec=spec)
        presentation = spec.get("presentation") or {
            key: spec[key] for key in ("columns", "column_groups") if key in spec
        }
        return json.dumps(
            {"selection": selection, "presentation": presentation}, sort_keys=True
        )
    except (ValueError, KeyError, TypeError) as exc:
        return json.dumps(
            {
                "selection": None,
                "presentation": {},
                "unavailable_reason": str(exc),
                "original_definition": raw,
            },
            sort_keys=True,
        )


def migrate_unified_views(db: Any) -> None:
    """Keep IDs of ordinary Views and all watches; allocate IDs for former lenses."""
    tables = {
        row[0]
        for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if "views" not in tables:
        return
    watch_columns = {row[1] for row in db.execute("PRAGMA table_info(watches)")}
    if "view_id" in watch_columns:
        return
    project = _Definitions(db)
    db.execute("""CREATE TABLE domain_views (
        id INTEGER PRIMARY KEY,
        sheet_id INTEGER REFERENCES sheets(id) ON DELETE CASCADE,
        name TEXT NOT NULL, spec TEXT NOT NULL DEFAULT '{}',
        op_id INTEGER REFERENCES ops(id),
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    )""")
    for row in db.execute("SELECT * FROM views ORDER BY id").fetchall():
        db.execute(
            "INSERT INTO domain_views VALUES (?,?,?,?,?,?,?)",
            (
                row["id"],
                row["sheet_id"],
                row["name"],
                _convert(project, row),
                row["op_id"],
                row["created_at"],
                row["updated_at"],
            ),
        )
    if "lenses" in tables:
        for row in db.execute("SELECT * FROM lenses ORDER BY id").fetchall():
            db.execute(
                "INSERT INTO domain_views(sheet_id,name,spec,op_id,created_at,updated_at) VALUES (?,?,?,?,?,?)",
                (
                    row["sheet_id"],
                    row["name"],
                    _convert(project, row),
                    row["op_id"],
                    row["created_at"],
                    row["updated_at"],
                ),
            )
        db.execute("DROP TABLE lenses")
    db.execute("DROP TABLE views")
    db.execute("ALTER TABLE domain_views RENAME TO views")
    db.execute("CREATE INDEX idx_views_sheet ON views(sheet_id)")
    if "watches" not in tables:
        return
    db.execute(
        "ALTER TABLE watches ADD COLUMN view_id INTEGER REFERENCES views(id) ON DELETE CASCADE"
    )
    for row in db.execute("SELECT * FROM watches ORDER BY id").fetchall():
        cursor = db.execute(
            "INSERT INTO views(sheet_id,name,spec,created_at,updated_at) VALUES (?,?,?,?,?)",
            (
                row["sheet_id"],
                row["name"],
                _convert(project, row, query=row["query"]),
                row["created_at"],
                row["updated_at"],
            ),
        )
        db.execute(
            "UPDATE watches SET view_id=? WHERE id=?", (cursor.lastrowid, row["id"])
        )
    db.execute("DROP INDEX IF EXISTS idx_watches_sheet")
    for column in ("query", "query_version", "query_hash", "scope", "sheet_id"):
        if column in watch_columns:
            db.execute(f"ALTER TABLE watches DROP COLUMN {column}")
    db.execute("CREATE UNIQUE INDEX idx_watches_view ON watches(view_id)")
    # Different query semantics must establish a new silent successful baseline.
    if "baseline_run_id" in watch_columns:
        db.execute("UPDATE watches SET baseline_run_id=NULL")
    for table in ("watch_seen_rows", "watch_row_field_state"):
        if table in tables:
            db.execute(f"DELETE FROM {table}")
