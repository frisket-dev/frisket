"""Lazy, pull-computed derived-sheet staleness (Workbench IA increment 7).

Derived sheets are LIVE: when an ancestor changes, the child is *stale* until it
is refreshed. Staleness is computed at READ time (sheets-list / lineage endpoint)
from a watermark, mirroring the FTS sidecar idiom (``fts_state.indexed_at_op`` vs
``project.op_cursor`` in ``frisket.search``) -- NOT pushed from mutation sites,
whose incomplete coverage is the documented failure mode
(``store/evidence.py`` ``mark_evidence_stale_for_cell_refs`` fires from only 2 of
many mutation sites).

Watermark: ``sheets.last_verified_op_cursor`` -- the op_cursor a derived sheet was
last materialized/refreshed against. NULL (never refreshed) falls back to
``parent_op_id`` (the create op). A derived sheet is stale iff some APPLIED op with
id greater than the watermark touched one of its ancestors. "Applied" is the key:
undo/redo move ``op_cursor`` backward and flip ops to ``undone``/``discarded``, so
an undone edit no longer counts -- the resolver recomputes from live op status
every read and never trusts a stored flag across a cursor rewind (survey risk 2).

The sheet forest is append-only (``parent_sheet_id`` is write-once), so ancestry
has no cycles. Multi-parent sheets (join/resolve/reduce) contribute extra
ancestors through ``materialized_row_sources.source_sheet_id``; stale detection
covers all of them.
"""

from __future__ import annotations

import json
from typing import Any

STALE_REASON_PARENT_CHANGED = "parent_changed"


def _placeholders(n: int) -> str:
    return ",".join("?" * n)


def _ancestor_sheet_ids(
    sheet_id: int,
    parent_of: dict[int, int | None],
    sources_of: dict[int, set[int]],
) -> set[int]:
    """Transitive ancestor sheet ids of ``sheet_id`` (excluding itself).

    Union of the single-parent chain (``parent_sheet_id``) and every
    multi-parent source sheet (``materialized_row_sources``). Guarded against
    accidental cycles even though the forest is structurally acyclic.
    """
    seen: set[int] = set()
    frontier: list[int] = [sheet_id]
    while frontier:
        current = frontier.pop()
        parents: set[int] = set()
        direct = parent_of.get(current)
        if direct is not None:
            parents.add(int(direct))
        parents |= sources_of.get(current, set())
        for parent in parents:
            if parent == sheet_id or parent in seen:
                continue
            seen.add(parent)
            frontier.append(parent)
    seen.discard(sheet_id)
    return seen


def _ancestor_changed(project: Any, ancestors: set[int], watermark: int) -> bool:
    """True if any APPLIED op after ``watermark`` mutated an ancestor sheet.

    Attribution joins through the tables that carry BOTH a sheet link and an op
    link -- never ``ops.spec`` parsing, whose keys differ per kind:

    * ``edits`` -> ``rows.sheet_id``: manual cell edits AND review reject/edit
      (both write an edit overlay row keyed by op_id).
    * ``runs.sheet_id``: AI-column runs, imports, and any run-producing op.
    * ``review.decision`` verify flips write no edit overlay; they are attributed
      via the op's ``undo_info`` review-state keys -> ``run_id`` -> sheet.
      Review-state flips mark descendants stale like any data edit —
      a downstream action can filter on ``review_state='verified'``, so a flip
      changes what it would consume.
    """
    if not ancestors:
        return False
    ids = sorted(ancestors)
    ph = _placeholders(len(ids))
    edit_hit = project.db.execute(
        f"SELECT 1 FROM edits e "
        f"JOIN rows r ON e.row_id = r.id "
        f"JOIN ops o ON e.op_id = o.id "
        f"WHERE o.status='applied' AND e.op_id > ? AND r.sheet_id IN ({ph}) "
        f"LIMIT 1",
        (watermark, *ids),
    ).fetchone()
    if edit_hit is not None:
        return True
    run_hit = project.db.execute(
        f"SELECT 1 FROM runs run "
        f"JOIN ops o ON run.op_id = o.id "
        f"WHERE o.status='applied' AND run.op_id > ? AND run.sheet_id IN ({ph}) "
        f"LIMIT 1",
        (watermark, *ids),
    ).fetchone()
    if run_hit is not None:
        return True
    return _review_verify_touched_ancestor(project, ids, watermark)


def _review_verify_touched_ancestor(
    project: Any, ancestor_ids: list[int], watermark: int
) -> bool:
    review_ops = project.db.execute(
        "SELECT undo_info FROM ops "
        "WHERE kind='review.decision' AND status='applied' AND id > ?",
        (watermark,),
    ).fetchall()
    if not review_ops:
        return False
    ancestor_set = set(ancestor_ids)
    for row in review_ops:
        try:
            info = json.loads(row["undo_info"] or "{}")
        except (ValueError, TypeError):
            continue
        states = info.get("review_states") or info.get("review_states_after") or {}
        for key in states:
            run_id = str(key).split(":", 1)[0]
            if not run_id.isdigit():
                continue
            sheet_row = project.db.execute(
                "SELECT sheet_id FROM runs WHERE id=?", (int(run_id),)
            ).fetchone()
            if sheet_row is not None and int(sheet_row["sheet_id"]) in ancestor_set:
                return True
    return False


def compute_sync_states(project: Any) -> dict[int, dict[str, str | None]]:
    """Lazy staleness for every visible DERIVED sheet.

    Returns ``{sheet_id: {"sync_state": "synced"|"stale", "stale_reason": str|None}}``
    for derived sheets only (``parent_sheet_id`` NOT NULL). ROOT sheets are absent
    entirely -- syncState is meaningless for a sheet with no parent.

    Fast paths keep the sheets-list read cheap: a project with no derived sheets
    returns ``{}`` immediately (root-only projects pay nothing), and a derived
    sheet whose watermark equals the global op_cursor is ``synced`` without any
    ancestor scan (nothing changed anywhere since it was materialized).
    """
    rows = project.db.execute(
        "SELECT id, parent_sheet_id, parent_op_id, last_verified_op_cursor "
        "FROM sheets WHERE hidden=0"
    ).fetchall()
    derived = [r for r in rows if r["parent_sheet_id"] is not None]
    if not derived:
        return {}

    cursor = project.op_cursor
    parent_of: dict[int, int | None] = {
        int(r["id"]): (
            int(r["parent_sheet_id"]) if r["parent_sheet_id"] is not None else None
        )
        for r in rows
    }
    sources_of = _multi_parent_sources(project, [int(r["id"]) for r in derived])

    out: dict[int, dict[str, str | None]] = {}
    for sheet in derived:
        sheet_id = int(sheet["id"])
        watermark = sheet["last_verified_op_cursor"]
        if watermark is None:
            watermark = sheet["parent_op_id"]
        watermark = int(watermark or 0)
        if watermark >= cursor:
            out[sheet_id] = {"sync_state": "synced", "stale_reason": None}
            continue
        ancestors = _ancestor_sheet_ids(sheet_id, parent_of, sources_of)
        if _ancestor_changed(project, ancestors, watermark):
            out[sheet_id] = {
                "sync_state": "stale",
                "stale_reason": STALE_REASON_PARENT_CHANGED,
            }
        else:
            out[sheet_id] = {"sync_state": "synced", "stale_reason": None}
    return out


def _multi_parent_sources(project: Any, derived_ids: list[int]) -> dict[int, set[int]]:
    """Map each derived sheet id to the extra source sheets that feed it via
    ``materialized_row_sources`` (join/resolve/reduce membership)."""
    if not derived_ids:
        return {}
    ph = _placeholders(len(derived_ids))
    rows = project.db.execute(
        f"SELECT DISTINCT r.sheet_id AS child_sheet_id, mrs.source_sheet_id "
        f"FROM materialized_row_sources mrs "
        f"JOIN rows r ON mrs.materialized_row_id = r.id "
        f"WHERE r.sheet_id IN ({ph})",
        tuple(derived_ids),
    ).fetchall()
    out: dict[int, set[int]] = {}
    for row in rows:
        child = int(row["child_sheet_id"])
        src = int(row["source_sheet_id"])
        out.setdefault(child, set()).add(src)
    from frisket.engine.store.sheet_lifecycle import materialized_source_sheets

    for child, sources in materialized_source_sheets(project).items():
        if child in derived_ids:
            out.setdefault(child, set()).update(sources)
    return out


def sheet_is_stale(project: Any, sheet_id: int) -> bool:
    """Convenience single-sheet check (used by refresh precondition tests)."""
    states = compute_sync_states(project)
    entry = states.get(int(sheet_id))
    return entry is not None and entry["sync_state"] == "stale"
