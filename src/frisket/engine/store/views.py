"""One View definition: selection plus display preferences."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from frisket.engine.store.transactions import inserted_rowid, write_transaction


def views(project: Any, sheet_id: int | None = None) -> list[sqlite3.Row]:
    if sheet_id is None:
        return project.db.execute(
            "SELECT * FROM views ORDER BY created_at, id"
        ).fetchall()
    return project.db.execute(
        "SELECT * FROM views WHERE sheet_id=? ORDER BY created_at, id",
        (sheet_id,),
    ).fetchall()


def get_view(project: Any, view_id: int) -> sqlite3.Row | None:
    return project.db.execute("SELECT * FROM views WHERE id=?", (view_id,)).fetchone()


def add_view(
    project: Any,
    name: str,
    spec: dict[str, Any] | None = None,
    *,
    sheet_id: int | None = None,
) -> int:
    """Create a view and log it as an op, in one owned transaction.

    A view is a named, reusable filter/sort over a sheet. Unlike an
    ephemeral grid setting, creating one appends an op (kind='view') so the
    "sort/filter the sheet" action becomes provenance-bearing. Returns the
    new view id.
    """
    from frisket.engine.views.specs import normalize_view_spec

    spec = normalize_view_spec(project, spec or {})
    derived_sheet = spec["selection"]["scope"].get("sheet_id")
    if sheet_id is not None and sheet_id != derived_sheet:
        raise ValueError("view sheet_id does not match selection scope")
    sheet_id = derived_sheet
    with write_transaction(project.db, what="view add"):
        op_id = project.append_change(
            "view",
            {"name": name, "sheet_id": sheet_id, "spec": spec},
            label=f"view {name}",
            commit=False,
        )
        cur = project.db.execute(
            "INSERT INTO views (sheet_id, name, spec, op_id) VALUES (?,?,?,?)",
            (sheet_id, name, json.dumps(spec), op_id),
        )
    return inserted_rowid(cur)


def update_view(
    project: Any,
    view_id: int,
    name: str | None = None,
    spec: dict[str, Any] | None = None,
) -> None:
    """Re-save a view's filter/sort. Logs an op in the same owned transaction
    so each saved change is provenance-bearing, not a silent overwrite."""
    sets: list[str] = []
    vals: list[Any] = []
    if name is not None:
        sets.append("name=?")
        vals.append(name)
    if spec is not None:
        from frisket.engine.views.specs import normalize_view_spec

        spec = normalize_view_spec(project, spec)
        sets.extend(["spec=?", "sheet_id=?"])
        vals.extend([json.dumps(spec), spec["selection"]["scope"].get("sheet_id")])
    if not sets:
        return
    with write_transaction(project.db, what="view update"):
        op_id = project.append_change(
            "view",
            {"view_id": view_id, "name": name, "spec": spec},
            label=f"update view {name or view_id}",
            commit=False,
        )
        sets.append("op_id=?")
        vals.append(op_id)
        sets.append("updated_at=datetime('now')")
        vals.append(view_id)
        project.db.execute(f"UPDATE views SET {', '.join(sets)} WHERE id=?", vals)


def delete_view(project: Any, view_id: int) -> None:
    """Delete a view and its alerts in one owned transaction."""
    # Reuse alert deletion so notification payloads cannot retain deleted details.
    from .alerts import delete_alert_rows

    with write_transaction(project.db, what="view delete"):
        for row in project.db.execute(
            "SELECT id FROM alerts WHERE view_id=?", (view_id,)
        ).fetchall():
            delete_alert_rows(project, int(row["id"]))
        project.db.execute("DELETE FROM views WHERE id=?", (view_id,))
