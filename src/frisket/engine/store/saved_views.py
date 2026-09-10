"""Saved views and lenses for a project bundle. A view is a named, reusable
filter/sort over a sheet; a lens is a saved semantic view (LensSpec). Both are
provenance-bearing: creating or re-saving one appends an op through the
facade's op log. Free functions over the facade's per-thread SQLite
connection; ``project`` stays duck-typed (``Any``) so this leaf never
re-imports the facade module."""

from __future__ import annotations

import json
import sqlite3
from typing import Any


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
    sheet_id: int,
    commit: bool = True,
) -> int:
    """Create a saved view and log it as an op.

    A view is a named, reusable filter/sort over a sheet. Unlike an
    ephemeral grid setting, creating one appends an op (kind='view') so the
    "sort/filter the sheet" action becomes provenance-bearing. Returns the
    new view id.
    """
    spec = spec or {}
    op_id = project.append_op(
        "view",
        {"name": name, "sheet_id": sheet_id, "spec": spec},
        label=f"view {name}",
        commit=commit,
    )
    cur = project.db.execute(
        "INSERT INTO views (sheet_id, name, spec, op_id) VALUES (?,?,?,?)",
        (sheet_id, name, json.dumps(spec), op_id),
    )
    if commit:
        project.db.commit()
    return cur.lastrowid


def update_view(
    project: Any,
    view_id: int,
    name: str | None = None,
    spec: dict[str, Any] | None = None,
) -> None:
    """Re-save a view's filter/sort. Logs an op so each saved change
    is provenance-bearing, not a silent overwrite."""
    sets: list[str] = []
    vals: list[Any] = []
    if name is not None:
        sets.append("name=?")
        vals.append(name)
    if spec is not None:
        sets.append("spec=?")
        vals.append(json.dumps(spec))
    if not sets:
        return
    op_id = project.append_op(
        "view",
        {"view_id": view_id, "name": name, "spec": spec},
        label=f"update view {name or view_id}",
    )
    sets.append("op_id=?")
    vals.append(op_id)
    sets.append("updated_at=datetime('now')")
    vals.append(view_id)
    project.db.execute(f"UPDATE views SET {', '.join(sets)} WHERE id=?", vals)
    project.db.commit()


def delete_view(project: Any, view_id: int) -> None:
    project.db.execute("DELETE FROM views WHERE id=?", (view_id,))
    project.db.commit()


def lenses(project: Any, sheet_id: int | None = None) -> list[sqlite3.Row]:
    if sheet_id is None:
        return project.db.execute(
            "SELECT * FROM lenses ORDER BY created_at, id"
        ).fetchall()
    return project.db.execute(
        "SELECT * FROM lenses WHERE sheet_id=? ORDER BY created_at, id",
        (sheet_id,),
    ).fetchall()


def get_lens(project: Any, lens_id: int) -> sqlite3.Row | None:
    return project.db.execute("SELECT * FROM lenses WHERE id=?", (lens_id,)).fetchone()


def add_lens(
    project: Any,
    name: str,
    spec: dict[str, Any],
    sheet_id: int | None = None,
) -> int:
    """Persist a saved lens (a LensSpec) and log it as an op (kind='lens') so
    saving a semantic view is provenance-bearing. Returns the new lens id."""
    op_id = project.append_op(
        "lens",
        {"name": name, "sheet_id": sheet_id, "spec": spec},
        label=f"lens {name}",
    )
    cur = project.db.execute(
        "INSERT INTO lenses (sheet_id, name, spec, op_id) VALUES (?,?,?,?)",
        (sheet_id, name, json.dumps(spec, sort_keys=True), op_id),
    )
    project.db.commit()
    return cur.lastrowid


def update_lens(
    project: Any,
    lens_id: int,
    name: str | None = None,
    spec: dict[str, Any] | None = None,
) -> None:
    sets: list[str] = []
    vals: list[Any] = []
    if name is not None:
        sets.append("name=?")
        vals.append(name)
    if spec is not None:
        sets.append("spec=?")
        vals.append(json.dumps(spec, sort_keys=True))
    if not sets:
        return
    op_id = project.append_op(
        "lens",
        {"lens_id": lens_id, "name": name, "spec": spec},
        label=f"update lens {name or lens_id}",
    )
    sets.append("op_id=?")
    vals.append(op_id)
    sets.append("updated_at=datetime('now')")
    vals.append(lens_id)
    project.db.execute(f"UPDATE lenses SET {', '.join(sets)} WHERE id=?", vals)
    project.db.commit()


def delete_lens(project: Any, lens_id: int) -> None:
    project.db.execute("DELETE FROM lenses WHERE id=?", (lens_id,))
    project.db.commit()
