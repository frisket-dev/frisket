"""Review queue: pending AI results triaged by humans,
sorted ascending by confidence — attention goes where the model is least sure.
Accept/reject/edit write review_state on results; edits also land in the
edit overlay (so they beat model values and are undoable)."""

from __future__ import annotations

from hashlib import blake2b
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
_ELIGIBLE_PRIMARY_WHERE = (
    f"res.outcome IN ({REVIEWABLE_OUTCOMES_SQL}) "
    f"AND rr.hidden = 0 AND {_ACTIVE_RESULT_WHERE}"
)


def _loads(value: str | None) -> Any:
    import json as _json

    if value is None:
        return None
    try:
        return _json.loads(value)
    except (TypeError, ValueError):
        return value


def _source_contexts(
    project: Project, keys: list[tuple[int, int]]
) -> dict[tuple[int, int], dict[str, Any]]:
    contexts = {key: {} for key in keys}
    rows_by_sheet: dict[int, set[int]] = {}
    for sheet_id, row_id in keys:
        rows_by_sheet.setdefault(sheet_id, set()).add(row_id)
    for sheet_id, row_ids in rows_by_sheet.items():
        ordered_row_ids = sorted(row_ids)
        for column in project.columns(sheet_id):
            if column["ai_generated"]:
                continue
            values = project.get_values(sheet_id, column["id"], row_ids=ordered_row_ids)
            for row_id in ordered_row_ids:
                value = values.get(row_id)
                if value is not None:
                    contexts[(sheet_id, row_id)][column["name"]] = value
    return contexts


def _source_context(project: Project, sheet_id: int, row_id: int) -> dict[str, Any]:
    return _source_contexts(project, [(sheet_id, row_id)])[(sheet_id, row_id)]


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
        WHERE res.review_state = 'unreviewed' AND {_ELIGIBLE_PRIMARY_WHERE} {where}
          AND {primary_where}
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


def _shuffle_key(seed: int, run_id: int, row_id: int) -> bytes:
    return blake2b(f"{seed}:{run_id}:{row_id}".encode(), digest_size=8).digest()


def review_bundles(
    project: Project,
    sheet_id: int | None = None,
    limit: int = 200,
    offset: int = 0,
    run_id: int | None = None,
    include_reviewed: bool = False,
    field_id: int | None = None,
    order: str = "confidence",
    seed: int = 0,
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
    if field_id is not None:
        where += " AND res.column_id = ?"
        params.append(field_id)
    review_where = "1 = 1" if include_reviewed else "res.review_state = 'unreviewed'"
    primary_where = _primary_where("c")
    primary_params = _primary_params()
    if order == "shuffle":
        # A seeded hash gives each row a stable shuffled rank across pages and
        # refreshes without materializing the run's row IDs in Python.
        project.db.create_function(
            "review_shuffle_key", 3, _shuffle_key, deterministic=True
        )
        shuffle_select = (
            ", review_shuffle_key(?, res.run_id, res.row_id) AS shuffle_key"
        )
        order_by = "shuffle_key, res.row_id, res.run_id, c.sheet_id"
        order_params: tuple[Any, ...] = (seed,)
    else:
        shuffle_select = ""
        order_by = "confidence ASC NULLS LAST, res.row_id, res.run_id, c.sheet_id"
        order_params = ()
    keys = project.db.execute(
        f"""
        SELECT res.run_id, res.row_id, c.sheet_id, s.name AS sheet_name,
               runs.action_kind, runs.model, MIN(res.confidence) AS confidence
               {shuffle_select}
        FROM results res
        JOIN columns c ON c.id = res.column_id
        {_ACTIVE_HEAD_JOIN}
        JOIN sheets s ON s.id = c.sheet_id
        JOIN runs ON runs.id = res.run_id
        JOIN rows rr ON rr.id = res.row_id AND rr.sheet_id = c.sheet_id
        WHERE {review_where} AND {_ELIGIBLE_PRIMARY_WHERE} {where}
          AND {primary_where}
        GROUP BY res.run_id, res.row_id, c.sheet_id, runs.action_kind, runs.model
        ORDER BY {order_by}
        LIMIT ? OFFSET ?
        """,
        (*order_params, *params, *primary_params, limit, offset),
    ).fetchall()

    source_contexts = _source_contexts(
        project,
        [(int(key["sheet_id"]), int(key["row_id"])) for key in keys],
    )
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
                "source": source_contexts[(key["sheet_id"], key["row_id"])],
                "review_note": next(
                    (
                        field["review_note"]
                        for field in fields
                        if field["review_note"] not in (None, "")
                    ),
                    None,
                ),
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
    field_id: int | None = None,
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
    if field_id is not None:
        where += " AND res.column_id = ?"
        params.append(field_id)
    review_where = "1 = 1" if include_reviewed else "res.review_state = 'unreviewed'"
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
            WHERE {review_where} AND {_ELIGIBLE_PRIMARY_WHERE} {where}
              AND {primary_where}
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
    field_id: int | None = None,
    order: str = "confidence",
    seed: int = 0,
) -> dict[str, Any]:
    """Bounded public page of pending review bundles."""
    total = review_bundle_count(
        project,
        sheet_id=sheet_id,
        run_id=run_id,
        include_reviewed=include_reviewed,
        field_id=field_id,
    )
    bundles = review_bundles(
        project,
        sheet_id=sheet_id,
        limit=limit,
        offset=offset,
        run_id=run_id,
        include_reviewed=include_reviewed,
        field_id=field_id,
        order=order,
        seed=seed,
    )
    return offset_page_payload(
        "frisket.review_bundles_page.v1",
        items=bundles,
        item_key="bundles",
        offset=offset,
        limit=limit,
        total=total,
    )


def _eligible_run_where(
    *, sheet_id: int | None = None, run_id: int | None = None
) -> tuple[str, tuple[Any, ...]]:
    where = ""
    params: list[Any] = []
    if sheet_id is not None:
        where += " AND c.sheet_id = ?"
        params.append(sheet_id)
    if run_id is not None:
        where += " AND res.run_id = ?"
        params.append(run_id)
    return where, tuple(params)


def review_runs_page(
    project: Project,
    *,
    sheet_id: int | None = None,
    run_id: int | None = None,
    offset: int = 0,
    limit: int = 25,
) -> dict[str, Any]:
    """List runs with active, visible primary results and decision counts."""
    where, params = _eligible_run_where(sheet_id=sheet_id, run_id=run_id)
    primary_where = _primary_where("c")
    primary_params = _primary_params()
    eligible_from = f"""
        FROM results res
        JOIN columns c ON c.id = res.column_id
        {_ACTIVE_HEAD_JOIN}
        JOIN rows rr ON rr.id = res.row_id AND rr.sheet_id = c.sheet_id
        WHERE res.run_id = runs.id
          AND {_ELIGIBLE_PRIMARY_WHERE} AND {primary_where} {where}
    """
    total_row = project.db.execute(
        f"SELECT COUNT(*) AS count FROM runs WHERE EXISTS (SELECT 1 {eligible_from})",
        (*primary_params, *params),
    ).fetchone()
    total = int(total_row["count"] if total_row is not None else 0)
    run_rows = project.db.execute(
        f"""
        SELECT runs.id AS run_id, runs.sheet_id, sheets.name AS sheet_name,
               runs.action_kind, runs.model, runs.started_at,
               runs.review_completed_at
        FROM runs
        JOIN sheets ON sheets.id = runs.sheet_id
        WHERE EXISTS (SELECT 1 {eligible_from})
        ORDER BY runs.started_at DESC, runs.id DESC
        LIMIT ? OFFSET ?
        """,
        (*primary_params, *params, limit, offset),
    ).fetchall()
    selected_ids = [int(row["run_id"]) for row in run_rows]
    fields_by_run: dict[int, list[dict[str, Any]]] = {item: [] for item in selected_ids}
    if selected_ids:
        placeholders = ",".join("?" for _ in selected_ids)
        field_rows = project.db.execute(
            f"""
            SELECT res.run_id, c.id AS column_id, c.name AS column_name,
                   c.type AS column_type,
                   COUNT(*) AS eligible_count,
                   SUM(CASE WHEN res.review_decision IS NOT NULL THEN 1 ELSE 0 END)
                       AS reviewed_count,
                   SUM(CASE WHEN res.review_decision = 'accept' THEN 1 ELSE 0 END)
                       AS accepted_count,
                   SUM(CASE WHEN res.review_decision IN ('edit','reject','reject_clear')
                            THEN 1 ELSE 0 END) AS incorrect_count,
                   SUM(CASE WHEN res.review_decision IS NULL AND res.review_state = 'unreviewed'
                            THEN 1 ELSE 0 END) AS unreviewed_count,
                   SUM(CASE WHEN res.confidence IS NOT NULL THEN 1 ELSE 0 END)
                       AS confidence_count
            FROM results res
            JOIN columns c ON c.id = res.column_id
            {_ACTIVE_HEAD_JOIN}
            JOIN rows rr ON rr.id = res.row_id AND rr.sheet_id = c.sheet_id
            WHERE res.run_id IN ({placeholders})
              AND {_ELIGIBLE_PRIMARY_WHERE} AND {primary_where}
            GROUP BY res.run_id, c.id, c.name, c.type, c.position
            ORDER BY res.run_id DESC, c.position, c.id
            """,
            (*selected_ids, *primary_params),
        ).fetchall()
        for row in field_rows:
            fields_by_run[int(row["run_id"])].append(
                {
                    "column_id": row["column_id"],
                    "column_name": row["column_name"],
                    "column_type": row["column_type"],
                    **{name: int(row[name]) for name in _COUNT_NAMES},
                }
            )
    runs = []
    for row in run_rows:
        fields = fields_by_run[int(row["run_id"])]
        runs.append(
            {
                **dict(row),
                "review_status": (
                    "complete" if row["review_completed_at"] is not None else "open"
                ),
                "total": {
                    name: sum(int(field[name]) for field in fields)
                    for name in _COUNT_NAMES
                },
                "fields": fields,
            }
        )
    return offset_page_payload(
        "frisket.review_runs_page.v1",
        items=runs,
        item_key="runs",
        offset=offset,
        limit=limit,
        total=total,
    )


_COUNT_NAMES = (
    "eligible_count",
    "reviewed_count",
    "accepted_count",
    "incorrect_count",
    "unreviewed_count",
    "confidence_count",
)


def set_review_run_status(
    project: Project, *, run_id: int, status: str
) -> dict[str, Any] | None:
    """Set workflow status only when the run still has reviewable active output."""
    primary_where = _primary_where("c")
    db = project.db
    db.execute("BEGIN IMMEDIATE")
    try:
        cursor = db.execute(
            f"""
            UPDATE runs
            SET review_completed_at = CASE WHEN ? = 'complete' THEN datetime('now') END
            WHERE id = ? AND EXISTS (
                SELECT 1
                FROM results res
                JOIN columns c ON c.id = res.column_id
                {_ACTIVE_HEAD_JOIN}
                JOIN rows rr ON rr.id = res.row_id AND rr.sheet_id = c.sheet_id
                WHERE res.run_id = runs.id
                  AND {_ELIGIBLE_PRIMARY_WHERE} AND {primary_where}
            )
            """,
            (status, run_id, *_primary_params()),
        )
        if cursor.rowcount != 1:
            db.rollback()
            return None
        row = db.execute(
            "SELECT review_completed_at FROM runs WHERE id=?", (run_id,)
        ).fetchone()
        db.commit()
    except BaseException:
        db.rollback()
        raise
    completed_at = row["review_completed_at"] if row is not None else None
    return {
        "schema_version": "frisket.review_run_status.v1",
        "run_id": run_id,
        "status": "complete" if completed_at is not None else "open",
        "review_completed_at": completed_at,
    }


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
        WHERE res.review_state = 'unreviewed' AND {_ELIGIBLE_PRIMARY_WHERE}
          AND {primary_where} {run_where}
        """,
        params,
    ).fetchone()[0]
