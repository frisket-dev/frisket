"""Review queue: pending AI results triaged by humans,
sorted ascending by confidence — attention goes where the model is least sure.
Accept/reject/edit write review_state on results; edits also land in the
edit overlay (so they beat model values and are undoable)."""

from __future__ import annotations

from typing import Any

from frisket.review_predicate import (
    is_support_column,
    primary_params as _primary_params,
    primary_where as _primary_where,
)
from frisket.server.paging import offset_page_payload
from frisket.engine.store import Project
from frisket.engine.store.runs import REVIEWABLE_OUTCOMES_SQL


_ACTIVE_HEAD_JOIN = (
    "LEFT JOIN cell_result_heads active_head "
    "ON active_head.column_id=res.column_id "
    "AND active_head.row_id=res.row_id AND active_head.run_id=res.run_id"
)
_ACTIVE_RESULT_WHERE = (
    "(active_head.run_id IS NOT NULL OR ("
    "c.current_run_id=res.run_id AND NOT EXISTS ("
    "SELECT 1 FROM run_output_generations generation "
    "WHERE generation.column_id=c.id)))"
)


def _loads(value: str | None) -> Any:
    import json as _json

    if value is None:
        return None
    try:
        return _json.loads(value)
    except (TypeError, ValueError):
        return value


def _source_context(project: Project, sheet_id: int, row_id: int) -> dict[str, Any]:
    source = {}
    for c in project.columns(sheet_id):
        if not c["ai_generated"]:
            vals = project.get_values(sheet_id, c["id"], row_ids=[row_id])
            v = vals.get(row_id)
            if v is not None:
                source[c["name"]] = v
    return source


def review_queue(
    project: Project,
    sheet_id: int | None = None,
    limit: int = 200,
    *,
    run_id: int | None = None,
) -> list[dict[str, Any]]:
    """Unreviewed primary results from current runs, least-confident first."""
    where = ""
    params: list[Any] = []
    if sheet_id is not None:
        where = "AND c.sheet_id = ?"
        params.append(sheet_id)
    if run_id is not None:
        where += " AND res.run_id = ?"
        params.append(run_id)
    primary_where = _primary_where("c")
    primary_params = _primary_params()
    rows = project.db.execute(
        f"""
        SELECT res.run_id, res.row_id, res.column_id, res.value,
               res.confidence, res.justification, res.error,
               res.review_decision, res.review_note,
               c.name AS column_name, c.sheet_id, runs.action_kind, runs.model
        FROM results res
        JOIN columns c ON c.id = res.column_id
        {_ACTIVE_HEAD_JOIN}
        JOIN runs ON runs.id = res.run_id
        JOIN rows rr ON rr.id = res.row_id AND rr.sheet_id = c.sheet_id
        WHERE res.review_state = 'unreviewed' AND res.outcome IN ({REVIEWABLE_OUTCOMES_SQL}) {where}
          AND rr.hidden = 0 AND {_ACTIVE_RESULT_WHERE} AND {primary_where}
        ORDER BY res.confidence ASC NULLS LAST, res.row_id
        LIMIT ?
        """,
        (*params, *primary_params, limit),
    ).fetchall()

    out = []
    for r in rows:
        out.append(
            {
                "bundle_id": f"{r['run_id']}:{r['row_id']}",
                "run_id": r["run_id"],
                "row_id": r["row_id"],
                "column_id": r["column_id"],
                "column_name": r["column_name"],
                "sheet_id": r["sheet_id"],
                "value": _loads(r["value"]),
                "confidence": r["confidence"],
                "justification": r["justification"],
                "review_decision": r["review_decision"],
                "review_note": r["review_note"],
                "action_kind": r["action_kind"],
                "model": r["model"],
                "source": _source_context(project, r["sheet_id"], r["row_id"]),
            }
        )
    return out


def review_bundles(
    project: Project,
    sheet_id: int | None = None,
    limit: int = 200,
    offset: int = 0,
    run_id: int | None = None,
    include_reviewed: bool = False,
) -> list[dict[str, Any]]:
    """Group unreviewed primary results by run/row and attach support fields.

    The review state stays per result cell; bundles are only the queue shape.
    """
    where = ""
    params: list[Any] = []
    if sheet_id is not None:
        where = "AND c.sheet_id = ?"
        params.append(sheet_id)
    if run_id is not None:
        where += " AND res.run_id = ?"
        params.append(run_id)
    review_where = (
        "(res.review_state = 'unreviewed' OR res.review_decision IS NOT NULL)"
        if include_reviewed
        else "res.review_state = 'unreviewed'"
    )
    primary_where = _primary_where("c")
    primary_params = _primary_params()
    keys = project.db.execute(
        f"""
        SELECT res.run_id, res.row_id, c.sheet_id, s.name AS sheet_name,
               runs.action_kind, runs.model, MIN(res.confidence) AS confidence
        FROM results res
        JOIN columns c ON c.id = res.column_id
        {_ACTIVE_HEAD_JOIN}
        JOIN sheets s ON s.id = c.sheet_id
        JOIN runs ON runs.id = res.run_id
        JOIN rows rr ON rr.id = res.row_id AND rr.sheet_id = c.sheet_id
        WHERE {review_where} AND res.outcome IN ({REVIEWABLE_OUTCOMES_SQL}) {where}
          AND rr.hidden = 0 AND {_ACTIVE_RESULT_WHERE} AND {primary_where}
        GROUP BY res.run_id, res.row_id, c.sheet_id, runs.action_kind, runs.model
        ORDER BY confidence ASC NULLS LAST, res.row_id, res.run_id, c.sheet_id
        LIMIT ? OFFSET ?
        """,
        (*params, *primary_params, limit, offset),
    ).fetchall()

    bundles: list[dict[str, Any]] = []
    for key in keys:
        cells = project.db.execute(
            f"""
            SELECT res.run_id, res.row_id, res.column_id, res.value,
                   res.confidence, res.justification, res.error,
                   res.review_state, res.review_decision, res.review_note,
                   c.name AS column_name, c.type AS column_type,
                   c.sheet_id
            FROM results res
            JOIN columns c ON c.id = res.column_id
            {_ACTIVE_HEAD_JOIN}
            JOIN rows rr ON rr.id = res.row_id AND rr.sheet_id = c.sheet_id
            WHERE res.run_id=? AND res.row_id=? AND c.sheet_id=? AND rr.hidden=0
              AND {_ACTIVE_RESULT_WHERE}
            ORDER BY c.position, c.id
            """,
            (key["run_id"], key["row_id"], key["sheet_id"]),
        ).fetchall()
        items = []
        fields = []
        evidence = []
        for cell in cells:
            role = "evidence" if is_support_column(cell["column_name"]) else "field"
            item = {
                "run_id": cell["run_id"],
                "row_id": cell["row_id"],
                "column_id": cell["column_id"],
                "column_name": cell["column_name"],
                "column_type": cell["column_type"],
                "sheet_id": cell["sheet_id"],
                "value": _loads(cell["value"]),
                "confidence": cell["confidence"],
                "justification": cell["justification"],
                "error": cell["error"],
                "review_state": cell["review_state"],
                "review_decision": cell["review_decision"],
                "review_note": cell["review_note"],
                "role": role,
                "chore": role == "field"
                and cell["review_state"] == "unreviewed"
                and cell["error"] is None,
            }
            items.append(item)
            if role == "field":
                fields.append(item)
            else:
                evidence.append(item)
        bundles.append(
            {
                "id": f"{key['run_id']}:{key['row_id']}",
                "run_id": key["run_id"],
                "row_id": key["row_id"],
                "sheet_id": key["sheet_id"],
                "sheet_name": key["sheet_name"],
                "action_kind": key["action_kind"],
                "model": key["model"],
                "confidence": key["confidence"],
                "source": _source_context(project, key["sheet_id"], key["row_id"]),
                "fields": fields,
                "evidence": evidence,
                "items": items,
            }
        )
    return bundles


def review_bundle_count(
    project: Project,
    sheet_id: int | None = None,
    *,
    run_id: int | None = None,
    include_reviewed: bool = False,
) -> int:
    """Count pending row/run review bundles without loading their payloads."""
    where = ""
    params: list[Any] = []
    if sheet_id is not None:
        where = "AND c.sheet_id = ?"
        params.append(sheet_id)
    if run_id is not None:
        where += " AND res.run_id = ?"
        params.append(run_id)
    review_where = (
        "(res.review_state = 'unreviewed' OR res.review_decision IS NOT NULL)"
        if include_reviewed
        else "res.review_state = 'unreviewed'"
    )
    primary_where = _primary_where("c")
    primary_params = _primary_params()
    row = project.db.execute(
        f"""
        SELECT COUNT(*) AS count
        FROM (
            SELECT res.run_id, res.row_id, c.sheet_id
            FROM results res
            JOIN columns c ON c.id = res.column_id
            {_ACTIVE_HEAD_JOIN}
            JOIN runs ON runs.id = res.run_id
            JOIN rows rr ON rr.id = res.row_id AND rr.sheet_id = c.sheet_id
            WHERE {review_where} AND res.outcome IN ({REVIEWABLE_OUTCOMES_SQL}) {where}
              AND rr.hidden = 0 AND {_ACTIVE_RESULT_WHERE} AND {primary_where}
            GROUP BY res.run_id, res.row_id, c.sheet_id, runs.action_kind, runs.model
        ) pending_bundles
        """,
        (*params, *primary_params),
    ).fetchone()
    return int(row["count"] if row is not None else 0)


def review_bundle_page(
    project: Project,
    sheet_id: int | None = None,
    *,
    offset: int = 0,
    limit: int = 25,
    run_id: int | None = None,
    include_reviewed: bool = False,
) -> dict[str, Any]:
    """Bounded public page of pending review bundles."""
    total = review_bundle_count(
        project,
        sheet_id=sheet_id,
        run_id=run_id,
        include_reviewed=include_reviewed,
    )
    bundles = review_bundles(
        project,
        sheet_id=sheet_id,
        limit=limit,
        offset=offset,
        run_id=run_id,
        include_reviewed=include_reviewed,
    )
    return offset_page_payload(
        "frisket.review_bundles_page.v1",
        items=bundles,
        item_key="bundles",
        offset=offset,
        limit=limit,
        total=total,
    )


def queue_count(project: Project, *, run_id: int | None = None) -> int:
    primary_where = _primary_where("c")
    run_where = "AND res.run_id=?" if run_id is not None else ""
    params = (*_primary_params(), *((run_id,) if run_id is not None else ()))
    return project.db.execute(
        f"""
        SELECT COUNT(*) FROM results res
        JOIN columns c ON c.id = res.column_id
        {_ACTIVE_HEAD_JOIN}
        JOIN rows rr ON rr.id = res.row_id AND rr.sheet_id = c.sheet_id
        WHERE res.review_state = 'unreviewed' AND res.outcome IN ({REVIEWABLE_OUTCOMES_SQL})
          AND rr.hidden = 0 AND {_ACTIVE_RESULT_WHERE} AND {primary_where} {run_where}
        """,
        params,
    ).fetchone()[0]
