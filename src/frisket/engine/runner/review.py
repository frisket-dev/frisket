"""Review queue: pending AI results triaged by humans,
sorted ascending by confidence — attention goes where the model is least sure.
Accept/reject judge saved results. Cell edits are separate Changes and never
change the judgment or saved answer."""

from __future__ import annotations

from typing import Any

from frisket.engine.store import Project
from frisket.engine.store.current_value_windows import read_current_column
from frisket.engine.store.review_stats import (
    current_review_run_predicate,
    ensure_run_review_stats,
)
from frisket.engine.store.review_values import read_review_results
from frisket.engine.store.runs import REVIEWABLE_OUTCOMES_SQL, RunResultStore
from frisket.review_predicate import authored_output_roles
from frisket.server.paging import offset_page_payload


_ACTIVE_HEAD_JOIN = (
    "LEFT JOIN effective_current_cells active_head "
    "ON active_head.column_id=res.column_id "
    "AND active_head.row_id=res.row_id AND active_head.origin_kind='run_result' "
    "AND active_head.origin_run_id=res.run_id"
)
_ACTIVE_RESULT_WHERE = "active_head.origin_run_id IS NOT NULL"


def _review_execution_runs(project: Project, run_id: int) -> tuple[int, list[int]]:
    from frisket.engine.store.execution_groups import execution_runs

    if (
        project.db.execute("SELECT 1 FROM runs WHERE id=?", (run_id,)).fetchone()
        is None
    ):
        return run_id, []
    return execution_runs(project, run_id)


def _loads(value: str | None) -> Any:
    import json as _json

    if value is None:
        return None
    try:
        return _json.loads(value)
    except (TypeError, ValueError):
        return value


def _bounded_current_value(current: Any) -> dict[str, Any] | None:
    if current is None or current.bounded_value is None:
        return None
    return {
        "preview": current.bounded_value.preview,
        "total_characters": current.bounded_value.total_characters,
        "total_bytes": current.bounded_value.total_bytes,
        "truncated": True,
        "download_href": None,
        "_download_revision": current.bounded_value.retrieval_revision,
    }


def _ensure_current_review_stats(
    project: Project, *, sheet_id: int | None = None
) -> None:
    filters = ["runs.review_stats_ready=0", current_review_run_predicate("runs")]
    params: tuple[Any, ...] = ()
    if sheet_id is not None:
        filters.append("runs.sheet_id=?")
        params = (sheet_id,)
    rows = project.db.execute(
        f"SELECT runs.id FROM runs WHERE {' AND '.join(filters)}", params
    ).fetchall()
    for row in rows:
        ensure_run_review_stats(project.db, int(row["id"]))


def _current_review_runs_cte(
    *, sheet_id: int | None = None
) -> tuple[str, tuple[Any, ...]]:
    filters = [current_review_run_predicate("runs")]
    params: tuple[Any, ...] = ()
    if sheet_id is not None:
        filters.append("runs.sheet_id=?")
        params = (sheet_id,)
    return (
        "WITH current_review_runs AS MATERIALIZED ("
        "SELECT runs.id FROM runs WHERE " + " AND ".join(filters) + ")",
        params,
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
            int(column["id"]): read_current_column(
                project.db,
                sheet_id=sheet_id,
                column_id=int(column["id"]),
                row_ids=row_ids,
                bounded=True,
            )
            for _name, column in source_columns
        }
        for row_id in row_ids:
            items = []
            for name, column in source_columns:
                current = values_by_column[int(column["id"])].get(row_id)
                items.append(
                    {
                        "column_id": int(column["id"]),
                        "column_name": name,
                        "column_type": str(column["type"]),
                        "semantic_type": column["semantic_type"],
                        "format": column["format"],
                        "value": current.value if current is not None else None,
                        "bounded_value": _bounded_current_value(current),
                    }
                )
            contexts[(run_id, sheet_id, row_id)] = {
                "source": {
                    item["column_name"]: item["value"]
                    for item in items
                    if item["value"] is not None or item["bounded_value"] is not None
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
    attempt_only: bool = False,
) -> list[dict[str, Any]]:
    """Unreviewed primary results from current runs, least-confident first."""
    if run_id is not None:
        execution_id, group = _review_execution_runs(project, run_id)
        ids = [run_id] if attempt_only else group
        for item in ids:
            ensure_run_review_stats(project.db, item)
        cte = ""
        current_join = ""
        ph = ",".join("?" for _ in ids)
        where = (
            f"AND res.run_id IN ({ph}) "
            f"AND NOT EXISTS (SELECT 1 FROM results newer WHERE newer.run_id IN ({ph}) "
            "AND newer.run_id>res.run_id AND newer.row_id=res.row_id AND newer.column_id=res.column_id "
            "AND newer.outcome IN ('ok','empty') "
            "AND (newer.review_decision IS NULL OR newer.review_decision NOT IN ('reject','reject_clear')))"
        )
        params: tuple[Any, ...] = (*ids, *ids)
        if sheet_id is not None:
            where += " AND runs.sheet_id=?"
            params += (sheet_id,)
    else:
        _ensure_current_review_stats(project, sheet_id=sheet_id)
        cte, params = _current_review_runs_cte(sheet_id=sheet_id)
        current_join = (
            "JOIN current_review_runs current_run ON current_run.id=res.run_id"
        )
        where = ""
    rows = project.db.execute(
        f"""
        {cte}
        SELECT res.run_id, res.row_id, res.column_id,
               res.confidence, res.justification, res.error,
               res.review_decision, res.review_note,
               review_field.column_name, c.sheet_id, runs.action_kind, runs.model,
               EXISTS (SELECT 1 FROM effective_current_cells head
                 WHERE head.origin_kind='run_result' AND head.origin_run_id=res.run_id
                 AND head.row_id=res.row_id AND head.column_id=res.column_id) AS applied
        FROM results res
        {current_join}
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
        ORDER BY res.confidence ASC NULLS LAST,
                 res.row_id, res.run_id, res.column_id
        LIMIT ?
        """,
        (*params, limit),
    ).fetchall()

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
                "execution_run_id": execution_id if run_id is not None else r["run_id"],
                "applied": bool(r["applied"]),
                "result_ref": {
                    "run_id": r["run_id"],
                    "row_id": r["row_id"],
                    "column_id": r["column_id"],
                },
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


def _execution_bundle_query(project, run_id, *, sheet_id, include_reviewed, field_id):

    root, ids = _review_execution_runs(project, run_id)
    for item in ids:
        ensure_run_review_stats(project.db, item)
    ph = ",".join("?" for _ in ids)
    where = ""
    args = list(ids)
    if field_id is not None:
        where += " AND res.column_id=?"
        args.append(field_id)
    if sheet_id is not None:
        where += " AND runs.sheet_id=?"
        args.append(sheet_id)
    reviewed = (
        ""
        if include_reviewed
        else " AND review_state='unreviewed' AND review_decision IS NULL"
    )
    sql = f"""
        WITH successful AS (
            SELECT res.run_id,res.row_id,res.column_id,res.confidence,res.review_state,res.review_decision,
                   runs.sheet_id,runs.action_kind,runs.model,s.name AS sheet_name
            FROM results res JOIN runs ON runs.id=res.run_id
            JOIN sheets s ON s.id=runs.sheet_id
            JOIN run_review_fields field ON field.run_id=res.run_id AND field.column_id=res.column_id AND field.is_primary=1
            WHERE res.run_id IN ({ph})
              AND res.outcome IN ({REVIEWABLE_OUTCOMES_SQL})
              {where}
        ), latest AS (SELECT row_id,MAX(run_id) AS run_id FROM successful GROUP BY row_id)
        SELECT successful.run_id,successful.row_id,sheet_id,sheet_name,action_kind,model,MIN(confidence) AS confidence
        FROM successful JOIN latest ON latest.row_id=successful.row_id AND latest.run_id=successful.run_id
        WHERE 1=1 {reviewed}
        GROUP BY successful.run_id,successful.row_id,sheet_id
    """
    return root, sql, args


def review_bundles(
    project: Project,
    sheet_id: int | None = None,
    limit: int = 200,
    offset: int = 0,
    run_id: int | None = None,
    include_reviewed: bool = False,
    field_id: int | None = None,
    order: str = "confidence",
    cursor: int | None = None,
) -> list[dict[str, Any]]:
    """Group unreviewed primary results by run/row and attach support fields.

    The review state stays per result cell; bundles are only the queue shape.
    """
    if order not in {"confidence", "row"}:
        raise ValueError(f"unknown review order: {order}")
    if run_id is not None:
        root, query, args = _execution_bundle_query(
            project,
            run_id,
            sheet_id=sheet_id,
            include_reviewed=include_reviewed,
            field_id=field_id,
        )
        sql = f"SELECT * FROM ({query})"
        if cursor is not None:
            sql += " WHERE row_id>?"
            args.append(cursor)
        sql += (
            " ORDER BY row_id"
            if order == "row"
            else " ORDER BY confidence ASC NULLS LAST,row_id"
        )
        keys = project.db.execute(
            sql + " LIMIT ? OFFSET ?", (*args, limit, offset)
        ).fetchall()
        bundles = _hydrate_review_bundles(project, keys)
        for bundle in bundles:
            bundle["execution_run_id"] = root
        return bundles
    _ensure_current_review_stats(project, sheet_id=sheet_id)
    cte, cte_params = _current_review_runs_cte(sheet_id=sheet_id)
    params = list(cte_params)
    current_join = "JOIN current_review_runs current_run ON current_run.id=res.run_id"
    where = ""
    group_by = "res.run_id, res.row_id"
    if field_id is not None:
        where += " AND res.column_id = ?"
        params.append(field_id)
    review_where = (
        "1 = 1"
        if include_reviewed
        else "res.review_state = 'unreviewed' AND res.review_decision IS NULL"
    )
    if order == "row":
        if run_id is None:
            raise ValueError("row-order review pages require run_id")
        order_by = "res.row_id"
        if cursor is not None:
            where += " AND res.row_id > ?"
            params.append(cursor)
    else:
        order_by = "confidence ASC NULLS LAST, res.row_id, res.run_id, c.sheet_id"
    keys_sql = f"""
        {cte}
        SELECT res.run_id, res.row_id, c.sheet_id, s.name AS sheet_name,
               runs.action_kind, runs.model, MIN(res.confidence) AS confidence
        FROM results res
        {current_join}
        JOIN run_review_fields review_field
          ON review_field.run_id=res.run_id
         AND review_field.column_id=res.column_id
         AND review_field.is_primary=1
        JOIN columns c ON c.id = res.column_id
        JOIN sheets s ON s.id = c.sheet_id
        JOIN runs ON runs.id = res.run_id
        JOIN rows rr ON rr.id = res.row_id AND rr.sheet_id = c.sheet_id
        WHERE {review_where} AND res.outcome IN ({REVIEWABLE_OUTCOMES_SQL}) {where}
        GROUP BY {group_by}
        ORDER BY {order_by}
        LIMIT ? OFFSET ?
        """
    keys = project.db.execute(keys_sql, (*params, limit, offset)).fetchall()

    return _hydrate_review_bundles(project, keys)


def _hydrate_review_bundles(project: Project, keys: list[Any]) -> list[dict[str, Any]]:
    """Build review payloads for ordered bundle keys selected by any strategy."""

    source_contexts = _source_contexts(
        project,
        [
            (int(key["run_id"]), int(key["sheet_id"]), int(key["row_id"]))
            for key in keys
        ],
    )
    cells_by_bundle: dict[tuple[int, int, int], list[dict[str, Any]]] = {}
    rows_by_current: dict[tuple[int, int], list[int]] = {}
    output_roles_by_run: dict[int, dict[str, str]] = {}
    for key in keys:
        run_id = int(key["run_id"])
        if run_id not in output_roles_by_run:
            run = project.db.execute(
                "SELECT params FROM runs WHERE id=?", (run_id,)
            ).fetchone()
            output_roles_by_run[run_id] = authored_output_roles(
                run["params"] if run is not None else None
            )
        cells_sql = f"""
                SELECT res.run_id, res.row_id, res.column_id,
                       res.confidence, res.error,
                       res.review_state, res.review_decision, res.review_note,
                       review_field.column_name,
                       review_field.column_type,
                       c.semantic_type, c.format,
                       c.sheet_id,
                       CASE WHEN review_field.is_primary=1
                                  AND res.outcome IN ({REVIEWABLE_OUTCOMES_SQL})
                            THEN 1 ELSE 0 END AS is_primary,
                       CASE WHEN {_ACTIVE_RESULT_WHERE} THEN 1 ELSE 0 END AS can_edit
                FROM results res
                JOIN columns c ON c.id = res.column_id
                JOIN run_review_fields review_field
                  ON review_field.run_id=res.run_id
                 AND review_field.column_id=res.column_id
                {_ACTIVE_HEAD_JOIN}
                JOIN rows rr ON rr.id = res.row_id AND rr.sheet_id = c.sheet_id
                WHERE res.run_id=? AND res.row_id=? AND c.sheet_id=?
                ORDER BY review_field.column_position, res.column_id
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

    current_reads: dict[tuple[int, int], dict[int, Any]] = {}
    for (sheet_id, column_id), row_ids in rows_by_current.items():
        current_reads[(sheet_id, column_id)] = read_current_column(
            project.db,
            sheet_id=sheet_id,
            column_id=column_id,
            row_ids=list(dict.fromkeys(row_ids)),
            bounded=True,
            preserve_invalid=True,
        )

    result_coordinates: list[tuple[int, int, int]] = []
    for cells in cells_by_bundle.values():
        for cell in cells:
            current = current_reads[
                (int(cell["sheet_id"]), int(cell["column_id"]))
            ].get(int(cell["row_id"]))
            ref = current.value_ref if current is not None else None
            cell["changed"] = False
            cell["current_exact"] = (
                isinstance(ref, dict)
                and ref.get("kind") == "run_result"
                and ref.get("run_id") == cell["run_id"]
            )
            result_coordinates.append(
                (
                    int(cell["run_id"]),
                    int(cell["row_id"]),
                    int(cell["column_id"]),
                )
            )

    resolved = read_review_results(project.db, result_coordinates)
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
            current = current_reads[(int(cell["sheet_id"]), column_id)].get(row_id)
            if (cell["changed"] or cell["current_exact"]) and current is not None:
                value = current.value
                bounded_value = _bounded_current_value(current)
            else:
                coordinate = (int(cell["run_id"]), row_id, column_id)
                result = resolved.get(coordinate)
                value = result.value if result is not None else None
                bounded_value = (
                    result.bounded_value.as_dict()
                    if result is not None and result.bounded_value is not None
                    else None
                )
            coordinate = (int(cell["run_id"]), row_id, column_id)
            result = resolved.get(coordinate)
            justification = result.justification if result is not None else None
            bounded_justification = (
                result.bounded_justification.as_dict()
                if result is not None and result.bounded_justification is not None
                else None
            )
            role = "field" if bool(cell["is_primary"]) else "evidence"
            item = {
                "run_id": cell["run_id"],
                "row_id": cell["row_id"],
                "column_id": cell["column_id"],
                "column_name": cell["column_name"],
                "column_type": cell["column_type"],
                "output_key": output_roles_by_run[int(cell["run_id"])].get(
                    cell["column_name"]
                ),
                "semantic_type": cell["semantic_type"],
                "format": cell["format"],
                "sheet_id": cell["sheet_id"],
                "value": value,
                "bounded_value": bounded_value,
                "confidence": cell["confidence"],
                "justification": justification,
                "bounded_justification": bounded_justification,
                "error": cell["error"],
                "review_state": cell["review_state"],
                "review_decision": cell["review_decision"],
                "review_note": cell["review_note"],
                "role": role,
                "changed": bool(cell["changed"]),
                "applied": bool(cell["current_exact"]),
                "can_edit": role == "field"
                and bool(cell["current_exact"])
                and bounded_value is None,
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
                "execution_run_id": key["run_id"],
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
        _, query, args = _execution_bundle_query(
            project,
            run_id,
            sheet_id=sheet_id,
            include_reviewed=include_reviewed,
            field_id=field_id,
        )
        return project.db.execute(f"SELECT COUNT(*) FROM ({query})", args).fetchone()[0]
    _ensure_current_review_stats(project, sheet_id=sheet_id)
    cte, params = _current_review_runs_cte(sheet_id=sheet_id)
    if field_id is None:
        count_expression = (
            "runs.review_bundle_count"
            if include_reviewed
            else "runs.review_bundle_count-runs.review_resolved_bundle_count"
        )
        row = project.db.execute(
            f"""
            {cte}
            SELECT COALESCE(SUM({count_expression}),0) AS count
            FROM current_review_runs current_run
            JOIN runs ON runs.id=current_run.id
            """,
            params,
        ).fetchone()
    else:
        count_expression = (
            "review_field.eligible_count"
            if include_reviewed
            else "review_field.eligible_count-review_field.resolved_count"
        )
        row = project.db.execute(
            f"""
            {cte}
            SELECT COALESCE(SUM({count_expression}),0) AS count
            FROM current_review_runs current_run
            JOIN run_review_fields review_field
              ON review_field.run_id=current_run.id
             AND review_field.column_id=?
             AND review_field.is_primary=1
            """,
            (*params, field_id),
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
    cursor: int | None = None,
) -> dict[str, Any]:
    """Bounded public page of pending review bundles."""
    if order == "row" and offset != 0:
        raise ValueError("row-order review pages require offset=0")
    if order == "row" and run_id is None:
        raise ValueError("row-order review pages require run_id")
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
            "next_cursor": (
                int(bundles[-1]["row_id"]) if has_more and bundles else None
            ),
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


def _execution_review_fields(project: Project, run_id: int) -> list[dict[str, Any]]:
    """Count retained winning results without loading their values or provenance."""

    _, group = _review_execution_runs(project, run_id)
    for member in group:
        ensure_run_review_stats(project.db, member)
    marks = ",".join("?" for _ in group)
    rows = project.db.execute(
        f"""
        WITH candidates AS (
            SELECT res.row_id,res.column_id,res.review_decision,res.review_state,res.confidence,
                   field.column_name,field.column_type,field.column_position,
                   ROW_NUMBER() OVER (PARTITION BY res.row_id,res.column_id ORDER BY res.run_id DESC) AS position
            FROM results res JOIN run_review_fields field
              ON field.run_id=res.run_id AND field.column_id=res.column_id
            WHERE res.run_id IN ({marks}) AND field.is_primary=1
              AND res.outcome IN ({REVIEWABLE_OUTCOMES_SQL})
        )
        SELECT column_id,column_name,column_type,COUNT(*) AS eligible_count,
               SUM(review_decision IS NOT NULL) AS reviewed_count,
               SUM(CASE WHEN review_decision='accept' THEN 1 ELSE 0 END) AS accepted_count,
               SUM(CASE WHEN review_decision IN ('reject','reject_clear') THEN 1 ELSE 0 END) AS incorrect_count,
               SUM(review_state='unreviewed' AND review_decision IS NULL) AS unreviewed_count,
               SUM(confidence IS NOT NULL) AS confidence_count
        FROM candidates WHERE position=1 GROUP BY column_id ORDER BY column_position,column_id
    """,
        group,
    ).fetchall()
    return [dict(row) for row in rows]


def review_runs_page(
    project: Project,
    *,
    sheet_id: int | None = None,
    run_id: int | None = None,
    copied_op_id: int | None = None,
    offset: int = 0,
    limit: int = 25,
) -> dict[str, Any]:
    """List current review runs, or resolve one explicit historical run."""

    from .copied_review import copied_review_source

    if copied_op_id is not None:
        if run_id is not None:
            raise ValueError("select one review source")
        source = copied_review_source(project, copied_op_id)
        return offset_page_payload(
            "frisket.review_runs_page.v1",
            items=[source] if source else [],
            item_key="runs",
            offset=0,
            limit=limit,
            total=int(source is not None),
        )
    if run_id is not None:
        run_id, group = _review_execution_runs(project, run_id)
        for member in group:
            ensure_run_review_stats(project.db, member)
    else:
        _ensure_current_review_stats(project, sheet_id=sheet_id)
    filters = [
        "COALESCE(json_extract(runs.params,'$.execution_root_run_id'),runs.id)=runs.id",
    ]
    params: list[Any] = []
    if sheet_id is not None:
        filters.append("runs.sheet_id=?")
        params.append(sheet_id)
    if run_id is not None:
        filters.append("runs.id=?")
        params.append(run_id)
    filters.append(
        "EXISTS (SELECT 1 FROM run_review_fields review_field "
        "JOIN runs member ON member.id=review_field.run_id "
        "WHERE COALESCE(json_extract(member.params,'$.execution_root_run_id'),member.id)=runs.id "
        "AND review_field.is_primary=1 AND review_field.eligible_count>0)"
    )
    where = " AND ".join(f"({item})" for item in filters)
    source_sql = f"""SELECT runs.id run_id,NULL copied_op_id,runs.sheet_id,sheets.name sheet_name,
        runs.action_kind,runs.model,runs.started_at,runs.review_completed_at,(runs.status='running') running
        FROM runs JOIN sheets ON sheets.id=runs.sheet_id WHERE {where}"""
    if run_id is None:
        copy_filter = "AND col.sheet_id=?" if sheet_id is not None else ""
        source_sql += f""" UNION ALL SELECT NULL,op.id,col.sheet_id,sheet.name,'run.reapply',NULL,op.created_at,NULL,0
            FROM ops op JOIN copied_result_values copy ON copy.op_id=op.id
            JOIN columns col ON col.id=copy.column_id JOIN sheets sheet ON sheet.id=col.sheet_id
            JOIN json_each(op.spec,'$.output_bindings') binding ON json_extract(binding.value,'$.column_id')=copy.column_id
            WHERE op.status='applied' AND col.active=1 AND sheet.hidden=0 AND json_extract(binding.value,'$.review_role')='field'
              AND copy.outcome='completed' {copy_filter} GROUP BY op.id,col.sheet_id"""
        if sheet_id is not None:
            params.append(sheet_id)
    total = int(
        project.db.execute(f"SELECT COUNT(*) FROM ({source_sql})", params).fetchone()[0]
    )
    run_rows = project.db.execute(
        f"SELECT * FROM ({source_sql}) ORDER BY started_at DESC,COALESCE(run_id,copied_op_id) DESC LIMIT ? OFFSET ?",
        (*params, limit, offset),
    ).fetchall()
    fields_by_run = {
        int(row["run_id"]): _execution_review_fields(project, int(row["run_id"]))
        for row in run_rows
        if row["run_id"] is not None
    }
    runs = []
    for row in run_rows:
        if row["copied_op_id"] is not None:
            source = copied_review_source(project, int(row["copied_op_id"]))
            if source is not None:
                runs.append(source)
            continue
        fields = fields_by_run[int(row["run_id"])]
        runs.append(
            {
                **dict(row),
                "running": bool(row["running"]),
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
    """Set review workflow status for retained results across the execution."""
    run_id, members = _review_execution_runs(project, run_id)
    for member in members:
        ensure_run_review_stats(project.db, member)
    db = project.db
    db.execute("BEGIN IMMEDIATE")
    try:
        cursor = db.execute(
            """
            UPDATE runs
            SET review_completed_at = CASE WHEN ? = 'complete' THEN datetime('now') END
            WHERE id = ? AND status<>'running'
              AND EXISTS (
                SELECT 1
                FROM run_review_fields review_field
                JOIN runs member ON member.id=review_field.run_id
                WHERE COALESCE(json_extract(member.params, '$.execution_root_run_id'), member.id)=runs.id
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
        return sum(
            field["unreviewed_count"]
            for field in _execution_review_fields(project, run_id)
        )
    else:
        _ensure_current_review_stats(project)
        row = project.db.execute(
            f"""
            SELECT COALESCE(SUM(review_field.eligible_count-review_field.resolved_count),0)
                   AS count
            FROM run_review_fields review_field
            JOIN runs ON runs.id=review_field.run_id
            WHERE review_field.is_primary=1
              AND {current_review_run_predicate("runs", terminal_only=False)}
            """
        ).fetchone()
    copies = project.db.execute("""SELECT COUNT(*) FROM copied_result_values copy
        JOIN ops op ON op.id=copy.op_id JOIN columns col ON col.id=copy.column_id
        JOIN sheets sheet ON sheet.id=col.sheet_id JOIN rows target ON target.id=copy.row_id
        JOIN json_each(op.spec,'$.output_bindings') binding ON json_extract(binding.value,'$.column_id')=copy.column_id
        WHERE op.status='applied' AND col.active=1 AND sheet.hidden=0 AND target.hidden=0
          AND copy.outcome='completed' AND json_extract(binding.value,'$.review_role')='field'
          AND copy.review_state='unreviewed' AND copy.review_decision IS NULL""").fetchone()[
        0
    ]
    return int(row["count"] if row is not None else 0) + int(copies)
