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
    visible_result_where as _visible_result_where,
)
from frisket.server.paging import offset_page_payload
from frisket.engine.store import Project
from frisket.engine.store.review_stats import (
    current_review_run_predicate,
    ensure_run_review_stats,
)
from frisket.engine.store.runs import REVIEWABLE_OUTCOMES_SQL, RunResultStore


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
    f"AND {_visible_result_where('rr', 'c')} AND {_ACTIVE_RESULT_WHERE}"
)


def _loads(value: str | None) -> Any:
    import json as _json

    if value is None:
        return None
    try:
        return _json.loads(value)
    except (TypeError, ValueError):
        return value


def _is_exact_review_correction(
    cell: dict[str, Any],
    ref: dict[str, Any] | None,
    op: dict[str, Any] | None,
) -> bool:
    if (
        not isinstance(ref, dict)
        or ref.get("kind") != "manual_edit"
        or not isinstance(op, dict)
        or op.get("status") != "applied"
        or op.get("kind") != "review.decision"
    ):
        return False
    spec = _loads(op.get("spec"))
    params = spec.get("params") if isinstance(spec, dict) else None
    return (
        isinstance(params, dict)
        and spec.get("action_id") == "review.decision"
        and params.get("run_id") == cell["run_id"]
        and params.get("row_id") == cell["row_id"]
        and params.get("column_id") == cell["column_id"]
        and params.get("decision") in {"edit", "reject_clear"}
    )


def _run_source_names(project: Project, run_id: int) -> tuple[str, ...]:
    """Return the exact ordered input-column roster persisted for one run.

    Both the legacy MapRunner and native typed-row executor persist their
    admitted references as ``input_columns``.  Review must use that durable
    declaration: generated inputs (notably timestamped transcripts) are real
    sources, while unrelated imported columns are not.
    """

    row = project.db.execute("SELECT params FROM runs WHERE id=?", (run_id,)).fetchone()
    spec = _loads(row["params"]) if row is not None else None
    raw = spec.get("input_columns") if isinstance(spec, dict) else None
    if not isinstance(raw, list):
        return ()
    names: list[str] = []
    for value in raw:
        if isinstance(value, str) and value and value not in names:
            names.append(value)
    return tuple(names)


def _source_contexts(
    project: Project, keys: list[tuple[int, int, int]]
) -> dict[tuple[int, int, int], dict[str, Any]]:
    contexts: dict[tuple[int, int, int], dict[str, Any]] = {}
    rows_by_run_sheet: dict[tuple[int, int], list[int]] = {}
    for run_id, sheet_id, row_id in keys:
        rows = rows_by_run_sheet.setdefault((run_id, sheet_id), [])
        if row_id not in rows:
            rows.append(row_id)
    for (run_id, sheet_id), row_ids in rows_by_run_sheet.items():
        columns = {
            str(column["name"]): column
            for column in project.columns(sheet_id, include_hidden=True)
        }
        source_columns = []
        for name in _run_source_names(project, run_id):
            column = columns.get(name)
            if column is None or bool(column["hidden"]):
                continue
            source_columns.append((name, column))
        values_by_column = {
            int(column["id"]): project.get_values(
                sheet_id, int(column["id"]), row_ids=row_ids
            )
            for _name, column in source_columns
        }
        for row_id in row_ids:
            items = [
                {
                    "column_id": int(column["id"]),
                    "column_name": name,
                    "column_type": str(column["type"]),
                    "semantic_type": column["semantic_type"],
                    "format": column["format"],
                    "value": values_by_column[int(column["id"])].get(row_id),
                }
                for name, column in source_columns
            ]
            contexts[(run_id, sheet_id, row_id)] = {
                "source": {
                    item["column_name"]: item["value"]
                    for item in items
                    if item["value"] is not None
                },
                "sources": items,
            }
    return contexts


def _source_context(project: Project, sheet_id: int, row_id: int) -> dict[str, Any]:
    """Legacy queue context has no grouped-run source projection."""

    context: dict[str, Any] = {}
    for column in project.columns(sheet_id):
        if column["ai_generated"]:
            continue
        value = project.get_values(sheet_id, column["id"], row_ids=[row_id]).get(row_id)
        if value is not None:
            context[column["name"]] = value
    return context


def review_queue(
    project: Project,
    sheet_id: int | None = None,
    limit: int = 200,
    *,
    run_id: int | None = None,
) -> list[dict[str, Any]]:
    """Unreviewed primary results from current runs, least-confident first."""
    if run_id is not None:
        ensure_run_review_stats(project.db, run_id)
    where = ""
    params: list[Any] = []
    if sheet_id is not None:
        where = "AND c.sheet_id = ?"
        params.append(sheet_id)
    if run_id is not None:
        where += " AND res.run_id = ?"
        params.append(run_id)
    primary_where = _primary_where("c", run_alias="runs")
    primary_params = _primary_params()
    if run_id is not None:
        queue_sql = f"""
        SELECT res.run_id, res.row_id, res.column_id,
               res.confidence, res.justification, res.error,
               res.review_decision, res.review_note,
               c.name AS column_name, c.sheet_id, runs.action_kind, runs.model
        FROM results res
        JOIN run_review_fields review_field
          ON review_field.run_id=res.run_id
         AND review_field.column_id=res.column_id
         AND review_field.is_primary=1
        JOIN columns c ON c.id = res.column_id
        JOIN runs ON runs.id = res.run_id
        JOIN rows rr ON rr.id = res.row_id AND rr.sheet_id = c.sheet_id
        WHERE res.review_state='unreviewed' AND res.review_decision IS NULL
          AND res.outcome IN ({REVIEWABLE_OUTCOMES_SQL})
          {where}
        ORDER BY res.confidence ASC NULLS LAST, res.row_id, res.column_id
        LIMIT ?
        """
        queue_params = (*params, limit)
    else:
        queue_sql = f"""
        SELECT res.run_id, res.row_id, res.column_id,
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
        """
        queue_params = (*params, *primary_params, limit)
    rows = project.db.execute(queue_sql, queue_params).fetchall()

    coordinates = [
        (int(row["run_id"]), int(row["row_id"]), int(row["column_id"])) for row in rows
    ]
    resolved = RunResultStore(project).decoded_result_rows(
        coordinates, tolerate_decode_errors=True
    )
    out = []
    for r in rows:
        coordinate = (int(r["run_id"]), int(r["row_id"]), int(r["column_id"]))
        result = resolved.get(coordinate)
        out.append(
            {
                "bundle_id": f"{r['run_id']}:{r['row_id']}",
                "run_id": r["run_id"],
                "row_id": r["row_id"],
                "column_id": r["column_id"],
                "column_name": r["column_name"],
                "sheet_id": r["sheet_id"],
                "value": result["value"] if result is not None else None,
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
    cursor: int | None = None,
) -> list[dict[str, Any]]:
    """Group unreviewed primary results by run/row and attach support fields.

    The review state stays per result cell; bundles are only the queue shape.
    """
    if run_id is not None:
        ensure_run_review_stats(project.db, run_id)
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
    review_where = (
        "1 = 1"
        if include_reviewed
        else "res.review_state = 'unreviewed' AND res.review_decision IS NULL"
    )
    primary_where = _primary_where("c", run_alias="runs")
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
    elif order == "row":
        shuffle_select = ""
        order_by = "res.row_id, res.run_id, c.sheet_id"
        order_params = ()
        if cursor is not None:
            where += " AND res.row_id > ?"
            params.append(cursor)
    else:
        shuffle_select = ""
        order_by = "confidence ASC NULLS LAST, res.row_id, res.run_id, c.sheet_id"
        order_params = ()
    if run_id is not None:
        keys_sql = f"""
        SELECT res.run_id, res.row_id, c.sheet_id, s.name AS sheet_name,
               runs.action_kind, runs.model, MIN(res.confidence) AS confidence
               {shuffle_select}
        FROM results res
        JOIN run_review_fields review_field
          ON review_field.run_id=res.run_id
         AND review_field.column_id=res.column_id
         AND review_field.is_primary=1
        JOIN columns c ON c.id = res.column_id
        JOIN sheets s ON s.id = c.sheet_id
        JOIN runs ON runs.id = res.run_id
        JOIN rows rr ON rr.id = res.row_id AND rr.sheet_id = c.sheet_id
        WHERE {review_where} AND res.outcome IN ({REVIEWABLE_OUTCOMES_SQL}) {where}
        GROUP BY res.row_id
        ORDER BY {order_by}
        LIMIT ? OFFSET ?
        """
        key_params = (*order_params, *params, limit, offset)
    else:
        keys_sql = f"""
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
        """
        key_params = (*order_params, *params, *primary_params, limit, offset)
    keys = project.db.execute(keys_sql, key_params).fetchall()

    source_contexts = _source_contexts(
        project,
        [
            (int(key["run_id"]), int(key["sheet_id"]), int(key["row_id"]))
            for key in keys
        ],
    )
    cells_by_bundle: dict[tuple[int, int, int], list[dict[str, Any]]] = {}
    rows_by_current: dict[tuple[int, int], list[int]] = {}
    for key in keys:
        if run_id is not None:
            cells_sql = f"""
                SELECT res.run_id, res.row_id, res.column_id,
                       res.confidence, res.justification, res.error,
                       res.review_state, res.review_decision, res.review_note,
                       c.name AS column_name, c.type AS column_type,
                       c.semantic_type, c.format,
                       c.sheet_id,
                       review_field.is_primary,
                       CASE WHEN {_ACTIVE_RESULT_WHERE} THEN 1 ELSE 0 END AS can_edit
                FROM results res
                JOIN columns c ON c.id = res.column_id
                LEFT JOIN run_review_fields review_field
                  ON review_field.run_id=res.run_id
                 AND review_field.column_id=res.column_id
                {_ACTIVE_HEAD_JOIN}
                JOIN rows rr ON rr.id = res.row_id AND rr.sheet_id = c.sheet_id
                WHERE res.run_id=? AND res.row_id=? AND c.sheet_id=?
                ORDER BY c.position, c.id
                """
        else:
            cells_sql = f"""
                SELECT res.run_id, res.row_id, res.column_id,
                       res.confidence, res.justification, res.error,
                       res.review_state, res.review_decision, res.review_note,
                       c.name AS column_name, c.type AS column_type,
                       c.semantic_type, c.format,
                       c.sheet_id,
                       NULL AS is_primary,
                       1 AS can_edit
                FROM results res
                JOIN columns c ON c.id = res.column_id
                {_ACTIVE_HEAD_JOIN}
                JOIN rows rr ON rr.id = res.row_id AND rr.sheet_id = c.sheet_id
                WHERE res.run_id=? AND res.row_id=? AND c.sheet_id=?
                  AND rr.hidden=0 AND {_ACTIVE_RESULT_WHERE}
                ORDER BY c.position, c.id
                """
        cells = [
            dict(cell)
            for cell in project.db.execute(
                cells_sql,
                (key["run_id"], key["row_id"], key["sheet_id"]),
            ).fetchall()
        ]
        bundle_key = (
            int(key["run_id"]),
            int(key["sheet_id"]),
            int(key["row_id"]),
        )
        cells_by_bundle[bundle_key] = cells
        for cell in cells:
            rows_by_current.setdefault(
                (int(cell["sheet_id"]), int(cell["column_id"])), []
            ).append(int(cell["row_id"]))

    current_values: dict[tuple[int, int], dict[int, Any]] = {}
    current_refs: dict[tuple[int, int], dict[int, dict[str, Any]]] = {}
    for (sheet_id, column_id), row_ids in rows_by_current.items():
        values, refs = project.get_values_with_refs(
            sheet_id,
            column_id,
            row_ids=list(dict.fromkeys(row_ids)),
            preserve_invalid=True,
        )
        current_values[(sheet_id, column_id)] = values
        current_refs[(sheet_id, column_id)] = refs

    op_ids = sorted(
        {
            int(ref["op_id"])
            for refs in current_refs.values()
            for ref in refs.values()
            if ref.get("kind") == "manual_edit" and ref.get("op_id") is not None
        }
    )
    ops_by_id: dict[int, dict[str, Any]] = {}
    for offset in range(0, len(op_ids), 800):
        chunk = op_ids[offset : offset + 800]
        placeholders = ",".join("?" for _ in chunk)
        for op in project.db.execute(
            f"SELECT id,status,kind,spec FROM ops WHERE id IN ({placeholders})",
            chunk,
        ):
            ops_by_id[int(op["id"])] = dict(op)

    result_coordinates: list[tuple[int, int, int]] = []
    for cells in cells_by_bundle.values():
        for cell in cells:
            ref = current_refs[(int(cell["sheet_id"]), int(cell["column_id"]))].get(
                int(cell["row_id"])
            )
            op_id = ref.get("op_id") if isinstance(ref, dict) else None
            cell["changed"] = _is_exact_review_correction(
                cell,
                ref,
                ops_by_id.get(int(op_id)) if op_id is not None else None,
            )
            cell["can_edit"] = bool(cell["can_edit"]) and (
                bool(cell["changed"])
                or (
                    isinstance(ref, dict)
                    and ref.get("kind") == "run_result"
                    and ref.get("run_id") == cell["run_id"]
                )
            )
            if not cell["changed"]:
                result_coordinates.append(
                    (
                        int(cell["run_id"]),
                        int(cell["row_id"]),
                        int(cell["column_id"]),
                    )
                )

    resolved = RunResultStore(project).decoded_result_rows(
        result_coordinates, tolerate_decode_errors=True
    )
    bundles: list[dict[str, Any]] = []
    for key in keys:
        bundle_key = (
            int(key["run_id"]),
            int(key["sheet_id"]),
            int(key["row_id"]),
        )
        cells = cells_by_bundle[bundle_key]
        items = []
        fields = []
        evidence = []
        for cell in cells:
            row_id = int(cell["row_id"])
            column_id = int(cell["column_id"])
            if cell["changed"]:
                value = current_values[(int(cell["sheet_id"]), column_id)].get(row_id)
            else:
                coordinate = (int(cell["run_id"]), row_id, column_id)
                result = resolved.get(coordinate)
                value = result["value"] if result is not None else None
            if run_id is not None:
                role = "field" if bool(cell["is_primary"]) else "evidence"
            else:
                role = (
                    "evidence"
                    if is_support_column(
                        cell["column_name"], action_kind=str(key["action_kind"])
                    )
                    else "field"
                )
            item = {
                "run_id": cell["run_id"],
                "row_id": cell["row_id"],
                "column_id": cell["column_id"],
                "column_name": cell["column_name"],
                "column_type": cell["column_type"],
                "semantic_type": cell["semantic_type"],
                "format": cell["format"],
                "sheet_id": cell["sheet_id"],
                "value": value,
                "confidence": cell["confidence"],
                "justification": cell["justification"],
                "error": cell["error"],
                "review_state": cell["review_state"],
                "review_decision": cell["review_decision"],
                "review_note": cell["review_note"],
                "role": role,
                "changed": bool(cell["changed"]),
                "can_edit": role == "field" and bool(cell["can_edit"]),
                "chore": role == "field"
                and cell["review_state"] == "unreviewed"
                and cell["review_decision"] is None
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
                "source": source_contexts[
                    (key["run_id"], key["sheet_id"], key["row_id"])
                ]["source"],
                "sources": source_contexts[
                    (key["run_id"], key["sheet_id"], key["row_id"])
                ]["sources"],
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
    if run_id is not None:
        ensure_run_review_stats(project.db, run_id)
        if sheet_id is not None:
            run_sheet = project.db.execute(
                "SELECT sheet_id FROM runs WHERE id=?", (run_id,)
            ).fetchone()
            if run_sheet is None or int(run_sheet["sheet_id"]) != sheet_id:
                return 0
        if field_id is None:
            row = project.db.execute(
                "SELECT review_bundle_count,review_resolved_bundle_count "
                "FROM runs WHERE id=?",
                (run_id,),
            ).fetchone()
            if row is None:
                return 0
            total = int(row["review_bundle_count"] or 0)
            if include_reviewed:
                return total
            return max(0, total - int(row["review_resolved_bundle_count"] or 0))
        field = project.db.execute(
            "SELECT eligible_count,resolved_count FROM run_review_fields "
            "WHERE run_id=? AND column_id=? AND is_primary=1",
            (run_id, field_id),
        ).fetchone()
        if field is None:
            return 0
        eligible = int(field["eligible_count"] or 0)
        if include_reviewed:
            return eligible
        return max(0, eligible - int(field["resolved_count"] or 0))
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
    primary_where = _primary_where("c", run_alias="runs")
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
    cursor: int | None = None,
) -> dict[str, Any]:
    """Bounded public page of pending review bundles."""
    if order == "row" and offset != 0:
        raise ValueError("row-order review pages require offset=0")
    fetch_limit = limit + 1 if order == "row" else limit
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
        limit=fetch_limit,
        offset=offset,
        run_id=run_id,
        include_reviewed=include_reviewed,
        field_id=field_id,
        order=order,
        seed=seed,
        cursor=cursor,
    )
    if order == "row":
        has_more = len(bundles) > limit
        bundles = bundles[:limit]
        return {
            "schema_version": "frisket.review_bundles_page.v1",
            "offset": 0,
            "limit": limit,
            "total": total,
            "has_more": has_more,
            "next_offset": None,
            "next_cursor": int(bundles[-1]["row_id"]) if has_more and bundles else None,
            "bundles": bundles,
        }
    return {
        **offset_page_payload(
            "frisket.review_bundles_page.v1",
            items=bundles,
            item_key="bundles",
            offset=offset,
            limit=limit,
            total=total,
        ),
        "next_cursor": None,
    }


def review_runs_page(
    project: Project,
    *,
    sheet_id: int | None = None,
    run_id: int | None = None,
    offset: int = 0,
    limit: int = 25,
) -> dict[str, Any]:
    """List finished reviewable runs which still own any current output."""
    if run_id is not None:
        ensure_run_review_stats(project.db, run_id)
    filters = [current_review_run_predicate("runs", terminal_only=True)]
    params: list[Any] = []
    if sheet_id is not None:
        filters.append("runs.sheet_id=?")
        params.append(sheet_id)
    if run_id is not None:
        filters.append("runs.id=?")
        params.append(run_id)
    filters.append(
        "EXISTS (SELECT 1 FROM run_review_fields review_field "
        "WHERE review_field.run_id=runs.id AND review_field.is_primary=1 "
        "AND review_field.eligible_count>0)"
    )
    where = " AND ".join(f"({item})" for item in filters)
    total_row = project.db.execute(
        f"SELECT COUNT(*) AS count FROM runs WHERE {where}",
        params,
    ).fetchone()
    total = int(total_row["count"] if total_row is not None else 0)
    run_rows = project.db.execute(
        f"""
        SELECT runs.id AS run_id, runs.sheet_id, sheets.name AS sheet_name,
               runs.action_kind, runs.model, runs.started_at,
               runs.review_completed_at
        FROM runs
        JOIN sheets ON sheets.id = runs.sheet_id
        WHERE {where}
        ORDER BY runs.started_at DESC, runs.id DESC
        LIMIT ? OFFSET ?
        """,
        (*params, limit, offset),
    ).fetchall()
    selected_ids = [int(row["run_id"]) for row in run_rows]
    fields_by_run: dict[int, list[dict[str, Any]]] = {item: [] for item in selected_ids}
    if selected_ids:
        for selected_id in selected_ids:
            ensure_run_review_stats(project.db, selected_id)
        placeholders = ",".join("?" for _ in selected_ids)
        field_rows = project.db.execute(
            f"""
            SELECT run_id, column_id, column_name, column_type,
                   eligible_count, reviewed_count, accepted_count,
                   incorrect_count,
                   eligible_count - resolved_count AS unreviewed_count,
                   confidence_count
            FROM run_review_fields
            WHERE run_id IN ({placeholders}) AND is_primary=1
            ORDER BY run_id DESC, column_position, column_id
            """,
            selected_ids,
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
    """Set workflow status for a run with a frozen reviewable field inventory."""
    ensure_run_review_stats(project.db, run_id)
    db = project.db
    db.execute("BEGIN IMMEDIATE")
    try:
        cursor = db.execute(
            """
            UPDATE runs
            SET review_completed_at = CASE WHEN ? = 'complete' THEN datetime('now') END
            WHERE id = ? AND EXISTS (
                SELECT 1
                FROM run_review_fields review_field
                WHERE review_field.run_id = runs.id
                  AND review_field.is_primary=1
                  AND review_field.eligible_count>0
            )
            """,
            (status, run_id),
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
    if run_id is not None:
        ensure_run_review_stats(project.db, run_id)
        row = project.db.execute(
            "SELECT COALESCE(SUM(eligible_count-resolved_count),0) AS count "
            "FROM run_review_fields WHERE run_id=? AND is_primary=1",
            (run_id,),
        ).fetchone()
    else:
        row = project.db.execute(
            f"""
            SELECT COALESCE(SUM(review_field.eligible_count-review_field.resolved_count),0)
                   AS count
            FROM run_review_fields review_field
            JOIN runs ON runs.id=review_field.run_id
            WHERE review_field.is_primary=1
              AND {current_review_run_predicate("runs", terminal_only=True)}
            """
        ).fetchone()
    return int(row["count"] if row is not None else 0)
