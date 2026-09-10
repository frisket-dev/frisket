"""Public run inspection and provenance payload helpers."""

from __future__ import annotations

import json
from typing import Any

from frisket.authoring.action_metadata import (
    action_metadata_for_action_kind,
    run_row_action_kind,
)
from frisket.engine.executor.action_reservations import QUEUED_ACTION_RUN_MARKER_PARAM
from frisket.server.paging import offset_page_meta
from frisket.engine.store import Project
from frisket.engine.store.result_generations import ResultGenerationStore
from frisket.engine.store.runs import FAILURE_OUTCOMES, RunResultStore, outcome_sql_list
from frisket.operability.trace import read_trace_row_retries


_RUN_TOKEN_COLUMNS = {"tokens_in", "tokens_out"}


def _json_loads(value: Any) -> Any:
    try:
        return json.loads(value) if value is not None else None
    except (TypeError, ValueError):
        return value


def _batch_history_op_run_payloads(
    project: Project, op_ids: list[int]
) -> dict[int, dict[str, Any]]:
    """The FULL run record behind each history op, batched for a whole page.

    The history list previously showed only kind/label/status; callers need
    the complete record, including its ID and deep link. Batched (one
    op_id-partitioned window-function query
    for the latest run per op + one IN-list query for every output column
    those runs wrote) rather than two extra queries PER op — a page of 50
    ops would otherwise run 100+ queries (the per-row lookup pattern was found on
    the first cut of this endpoint). No per-row/trace reads here (those
    stay behind the existing on-demand
    /actions/runs/{run_id}/rows?status=error endpoint the frontend already
    has, reused rather than duplicated)."""
    if not op_ids:
        return {}
    op_placeholders = ",".join("?" for _ in op_ids)
    run_rows = project.db.execute(
        "SELECT * FROM ("
        "  SELECT *, ROW_NUMBER() OVER (PARTITION BY op_id ORDER BY id DESC) AS rn"
        f"  FROM runs WHERE op_id IN ({op_placeholders})"
        ") WHERE rn=1",
        op_ids,
    ).fetchall()
    if not run_rows:
        return {}
    run_ids = [r["id"] for r in run_rows]
    columns_by_run: dict[int, list[dict[str, Any]]] = {rid: [] for rid in run_ids}
    run_placeholders = ",".join("?" for _ in run_ids)
    for r in project.db.execute(
        "SELECT DISTINCT res.run_id AS run_id, c.id AS column_id, c.name AS name,"
        " c.position AS position "
        "FROM results res JOIN columns c ON c.id=res.column_id "
        f"WHERE res.run_id IN ({run_placeholders}) "
        "ORDER BY res.run_id, c.position, c.id",
        run_ids,
    ):
        columns_by_run[r["run_id"]].append({"id": r["column_id"], "name": r["name"]})
    # One batched query for every run's error
    # groups (same discipline as the columns query above), not one per op.
    row_errors_by_run = RunResultStore(project).batch_row_error_summaries(run_ids)
    payloads: dict[int, dict[str, Any]] = {}
    for row in run_rows:
        payloads[row["op_id"]] = {
            "run_id": row["id"],
            "status": row["status"],
            "action_kind": run_row_action_kind(row),
            "model": row["model"],
            "params": _json_loads(row["params"]) or {},
            "total_rows": row["total_rows"],
            "completed_rows": row["completed_rows"],
            "failed_rows": row["failed_rows"],
            "cost_estimate": row["cost_estimate"],
            "cost_actual": row["cost_actual"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            # The claimant stamp, surfaced for hand-off
            # to an admin/agent debugging a divergent-worker incident.
            "worker_version": row["worker_version"],
            "output_columns": columns_by_run.get(row["id"], []),
            # None when the run has no failed rows (kept out of the JSON
            # entirely by the caller's key-presence, not stamped {}).
            "row_errors": row_errors_by_run.get(row["id"]),
        }
    return payloads


def _history_op_payload(
    row: Any, index: int, cursor: int, run_payload: dict[str, Any] | None
) -> dict[str, Any]:
    return {
        "id": row["id"],
        "index": index,
        "kind": row["kind"],
        "label": row["label"],
        "status": row["status"],
        "barrier": bool(row["barrier"]),
        "at_cursor": row["id"] == cursor,
        "created_at": row["created_at"],
        "run": run_payload,
    }


def _history_target_payload(project: Project, row: Any | None) -> dict[str, Any] | None:
    if row is None:
        return None
    index = (
        int(
            project.db.execute(
                "SELECT COUNT(*) FROM ops WHERE id <= ?",
                (row["id"],),
            ).fetchone()[0]
            or 0
        )
        - 1
    )
    return {
        "id": row["id"],
        "index": index,
        "barrier": bool(row["barrier"]),
    }


def history_page_payload(
    project: Project,
    *,
    offset: int | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    total = project.history_total()
    cursor = project.op_cursor
    cursor_index = project.history_cursor_index()
    if offset is None:
        offset = max(0, min(max(total - limit, 0), cursor_index - limit + 1))
    rows = project.history_page(offset, limit)
    cursor_row = (
        project.db.execute("SELECT * FROM ops WHERE id=?", (cursor,)).fetchone()
        if cursor > 0
        else None
    )
    undo_row = (
        project.db.execute(
            "SELECT * FROM ops WHERE id <= ? AND status='applied' "
            "ORDER BY id DESC LIMIT 1",
            (cursor,),
        ).fetchone()
        if cursor > 0
        else None
    )
    redo_row = project.db.execute(
        "SELECT * FROM ops WHERE id > ? AND status='undone' ORDER BY id LIMIT 1",
        (cursor,),
    ).fetchone()
    next_offset = offset + len(rows)
    # Batch every op's run payload in two queries total (not two PER op) —
    # the cursor op may lie outside the current page, so its id joins the
    # batch too.
    op_ids = [row["id"] for row in rows]
    if cursor_row is not None and cursor_row["id"] not in op_ids:
        op_ids.append(cursor_row["id"])
    run_payloads = _batch_history_op_run_payloads(project, op_ids)
    return {
        "schema_version": "frisket.history_page.v1",
        "order": "asc",
        "offset": offset,
        "limit": limit,
        "total": total,
        "has_more_before": offset > 0,
        "has_more_after": next_offset < total,
        "prev_offset": max(0, offset - limit) if offset > 0 else None,
        "next_offset": next_offset if next_offset < total else None,
        "cursor_index": cursor_index,
        "cursor_op": (
            _history_op_payload(
                cursor_row, cursor_index, cursor, run_payloads.get(cursor_row["id"])
            )
            if cursor_row is not None
            else None
        ),
        "cursor_op_loaded": any(row["id"] == cursor for row in rows),
        "undo_target": _history_target_payload(project, undo_row),
        "redo_target": _history_target_payload(project, redo_row),
        "revision": {
            "total": total,
            "max_op_id": project.db.execute(
                "SELECT COALESCE(MAX(id), 0) FROM ops"
            ).fetchone()[0],
            "op_cursor": cursor,
        },
        "ops": [
            _history_op_payload(
                row, offset + index, cursor, run_payloads.get(row["id"])
            )
            for index, row in enumerate(rows)
        ],
    }


def run_duration_ms(project: Project, run_id: int) -> int | None:
    row = project.db.execute(
        "SELECT started_at, finished_at FROM runs WHERE id=?", (run_id,)
    ).fetchone()
    if not row or not row["started_at"] or not row["finished_at"]:
        return None
    start = project.db.execute("SELECT julianday(?)", (row["started_at"],)).fetchone()[
        0
    ]
    finish = project.db.execute(
        "SELECT julianday(?)", (row["finished_at"],)
    ).fetchone()[0]
    if start is None or finish is None:
        return None
    return int(round((finish - start) * 86400 * 1000))


def run_tokens(project: Project, run_id: int, column: str) -> int | None:
    if column not in _RUN_TOKEN_COLUMNS:
        raise ValueError(f"unsupported token column: {column}")
    value = project.db.execute(
        f"SELECT SUM({column}) AS total FROM results WHERE run_id=?",  # noqa: S608
        (run_id,),
    ).fetchone()["total"]
    return int(value) if value is not None else None


def _run_row_status(cells: list[dict[str, Any]], run: Any) -> str:
    if any(c.get("error") for c in cells):
        return "error"
    if cells:
        return "complete"
    if run["status"] == "running":
        return "pending"
    if run["status"] == "cancelled":
        return "cancelled"
    return "not_run"


def action_run_rows_payload(
    project: Project,
    run_id: int,
    *,
    offset: int = 0,
    limit: int = 50,
    status: str | None = None,
) -> dict[str, Any]:
    run = project.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    if run is None:
        raise KeyError("no such run")
    offset = max(0, int(offset))
    limit = min(max(1, int(limit)), 500)
    if status not in (None, "error"):
        raise ValueError("unsupported row status filter")

    has_stored_scope = RunResultStore(project).has_run_row_scope(run_id)
    source_sql = "rows r"
    source_params: list[Any] = []
    base_where: list[str] = []
    base_params: list[Any] = []
    order_sql = "r.position"
    total = 0
    rows: list[Any] = []
    if has_stored_scope:
        # Stored scopes are historical run targets. Do not filter hidden rows:
        # undo/hide after launch must not rewrite inspection for the old run.
        source_sql = "run_rows rr JOIN rows r ON r.id=rr.row_id"
        base_where.append("rr.run_id=?")
        base_params.append(run_id)
        order_sql = "rr.position"
    else:
        # A full-sheet run recorded no per-row scope: inspect the sheet's
        # current visible rows.
        base_where.append("r.sheet_id=?")
        base_params.append(run["sheet_id"])
        base_where.append("r.hidden=0")

    if base_where:
        where = [*base_where]
        query_params: list[Any] = [*source_params, *base_params]
        if status == "error":
            error_parts = [
                "EXISTS ("
                "SELECT 1 FROM results res "
                "WHERE res.run_id=? AND res.row_id=r.id "
                f"AND res.outcome IN ({outcome_sql_list(FAILURE_OUTCOMES)})"
                ")"
            ]
            query_params.append(run_id)
            where.append(f"({' OR '.join(error_parts)})")
        where_sql = " AND ".join(where)
        total = project.db.execute(
            f"SELECT COUNT(*) FROM {source_sql} WHERE {where_sql}", query_params
        ).fetchone()[0]
        rows = project.db.execute(
            f"SELECT r.id, r.position FROM {source_sql} WHERE {where_sql} "
            f"ORDER BY {order_sql} LIMIT ? OFFSET ?",
            [*query_params, limit, offset],
        ).fetchall()
    page_ids = [row["id"] for row in rows]
    retries_by_row = read_trace_row_retries(project.path, run_id, page_ids)
    by_row: dict[int, list[dict[str, Any]]] = {row_id: [] for row_id in page_ids}
    if page_ids:
        ph = ",".join("?" * len(page_ids))
        for row in project.db.execute(
            "SELECT res.row_id, res.column_id, c.name AS column_name, "
            "res.value, res.error, res.tokens_in, res.tokens_out, "
            "res.confidence, res.justification, res.review_state "
            "FROM results res JOIN columns c ON c.id=res.column_id "
            f"WHERE res.run_id=? AND res.row_id IN ({ph}) "
            "ORDER BY res.row_id, c.position, c.id",
            [run_id, *page_ids],
        ):
            by_row[row["row_id"]].append(
                {
                    "column_id": row["column_id"],
                    "column_name": row["column_name"],
                    "value": _json_loads(row["value"]),
                    "error": row["error"],
                    "tokens_in": row["tokens_in"],
                    "tokens_out": row["tokens_out"],
                    "cost": None,
                    "confidence": row["confidence"],
                    "justification": row["justification"],
                    "review_state": row["review_state"],
                }
            )

    out_rows = []
    for row in rows:
        row_id = row["id"]
        cells = by_row.get(row_id, [])
        retries = retries_by_row.get(row_id, [])
        errors = [cell["error"] for cell in cells if cell.get("error")]
        out_rows.append(
            {
                "row_id": row_id,
                "row_index": row["position"],
                "status": _run_row_status(cells, run),
                "error": errors[0] if errors else None,
                "tokens_in": sum(cell["tokens_in"] or 0 for cell in cells) or None,
                "tokens_out": sum(cell["tokens_out"] or 0 for cell in cells) or None,
                "cost": None,
                "retry_count": len(retries),
                "retries": retries,
                "cells": cells,
            }
        )

    return {
        "run": {
            "id": run_id,
            **action_metadata_for_action_kind(run_row_action_kind(run)),
            "status": run["status"],
            "total_rows": run["total_rows"],
            "completed_rows": run["completed_rows"],
            "failed_rows": run["failed_rows"],
        },
        **offset_page_meta(
            offset=offset,
            limit=limit,
            total=total,
            item_count=len(out_rows),
        ),
        "rows": out_rows,
    }


def public_column_run_payload(
    project: Project,
    row: Any,
    *,
    current_run_id: int | None,
    review_scores: dict[int, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    action_metadata = action_metadata_for_action_kind(run_row_action_kind(row))
    spec = _json_loads(row["params"] or "{}")
    spec = spec if isinstance(spec, dict) else {}
    spec.pop(QUEUED_ACTION_RUN_MARKER_PARAM, None)
    scores = (review_scores or {}).get(
        int(row["id"]), {"human_score": {"passed": 0, "graded": 0}, "judge_scores": []}
    )
    return {
        "run_id": row["id"],
        "action_kind": action_metadata["action_kind"],
        "action_name": action_metadata["action_name"],
        "model": row["model"],
        "status": row["status"],
        "spec": spec,
        "total_rows": row["total_rows"],
        "completed_rows": row["completed_rows"],
        "failed_rows": row["failed_rows"],
        "cost_actual": row["cost_actual"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "duration_ms": run_duration_ms(project, row["id"]),
        "tokens_in": run_tokens(project, row["id"], "tokens_in"),
        "tokens_out": run_tokens(project, row["id"], "tokens_out"),
        "current": row["id"] == current_run_id,
        **scores,
    }


def _human_scores(
    project: Project, column_id: int, run_ids: list[int]
) -> dict[int, dict[str, int]]:
    scores = {run_id: {"passed": 0, "graded": 0} for run_id in run_ids}
    if not run_ids:
        return scores
    placeholders = ",".join("?" for _ in run_ids)
    for row in project.db.execute(
        "SELECT run_id, "
        "SUM(CASE WHEN review_decision='accept' THEN 1 ELSE 0 END) AS passed, "
        "COUNT(*) AS graded FROM results "
        f"WHERE column_id=? AND run_id IN ({placeholders}) "
        "AND review_decision IS NOT NULL GROUP BY run_id",
        (column_id, *run_ids),
    ):
        scores[int(row["run_id"])] = {
            "passed": int(row["passed"] or 0),
            "graded": int(row["graded"] or 0),
        }
    return scores


def _review_decisions_by_row(
    project: Project, column_id: int, run_id: int
) -> dict[int, bool]:
    return {
        int(row["row_id"]): row["review_decision"] == "accept"
        for row in project.db.execute(
            "SELECT row_id, review_decision FROM results "
            "WHERE column_id=? AND run_id=? AND review_decision IS NOT NULL",
            (column_id, run_id),
        )
    }


def _bool_result(value: Any) -> bool | None:
    loaded = _json_loads(value)
    return loaded if isinstance(loaded, bool) else None


def _judge_scores(
    project: Project, column_id: int, source_run_ids: list[int]
) -> dict[int, list[dict[str, Any]]]:
    """Scores from judge receipts whose pinned subject is this exact run/column."""
    out = {run_id: [] for run_id in source_run_ids}
    if not source_run_ids:
        return out
    source_ids = set(source_run_ids)
    human_by_run = {
        run_id: _review_decisions_by_row(project, column_id, run_id)
        for run_id in source_run_ids
    }
    receipts = project.db.execute(
        "SELECT rec.id, rec.run_id, rec.body, r.model, r.started_at "
        "FROM receipts rec JOIN runs r ON r.id=rec.run_id "
        "WHERE rec.action_kind='map.judge' "
        "AND rec.status='completed' AND r.status='completed' "
        "ORDER BY rec.run_id DESC, rec.id DESC"
    ).fetchall()
    linked: dict[int, dict[str, Any]] = {}
    for stored in receipts:
        judge_run_id = int(stored["run_id"])
        try:
            receipt = json.loads(stored["body"] or "{}")
        except (TypeError, ValueError):
            continue
        inputs = receipt.get("inputs") if isinstance(receipt, dict) else None
        outputs = receipt.get("outputs") if isinstance(receipt, dict) else None
        if not isinstance(inputs, list) or not isinstance(outputs, list):
            continue
        subject = next(
            (
                item.get("ref")
                for item in inputs
                if isinstance(item, dict)
                and isinstance(item.get("ref"), dict)
                and item["ref"].get("role") == "judged_output"
                and item["ref"].get("column_id") == column_id
                and item["ref"].get("source_run_id") in source_ids
            ),
            None,
        )
        verdict = next(
            (
                item.get("ref")
                for item in outputs
                if isinstance(item, dict)
                and isinstance(item.get("ref"), dict)
                and item["ref"].get("role") == "judge_verdict"
            ),
            None,
        )
        if not isinstance(subject, dict) or not isinstance(verdict, dict):
            continue
        source_run_id = int(subject["source_run_id"])
        verdict_column_id = verdict.get("column_id")
        if not isinstance(verdict_column_id, int):
            continue
        value_refs = subject.get("value_refs")
        if not isinstance(value_refs, list):
            continue
        exact_rows = {
            int(ref["row_id"])
            for ref in value_refs
            if isinstance(ref, dict)
            and ref.get("kind") == "run_result"
            and ref.get("run_id") == source_run_id
            and ref.get("column_id") == column_id
            and isinstance(ref.get("row_id"), int)
        }
        if not exact_rows:
            continue
        existing = linked.get(judge_run_id)
        if existing is None:
            linked[judge_run_id] = {
                "source_run_id": source_run_id,
                "verdict_column_id": verdict_column_id,
                "model": stored["model"],
                "started_at": stored["started_at"],
                "exact_rows": set(exact_rows),
            }
        elif (
            existing["source_run_id"] == source_run_id
            and existing["verdict_column_id"] == verdict_column_id
        ):
            existing["exact_rows"].update(exact_rows)

    for judge_run_id, link in linked.items():
        source_run_id = int(link["source_run_id"])
        verdict_column_id = int(link["verdict_column_id"])
        exact_rows = link["exact_rows"]
        judged: dict[int, bool] = {}
        for result in project.db.execute(
            "SELECT row_id, value FROM results "
            "WHERE run_id=? AND column_id=? AND error IS NULL",
            (judge_run_id, verdict_column_id),
        ):
            if int(result["row_id"]) not in exact_rows:
                continue
            parsed = _bool_result(result["value"])
            if parsed is not None:
                judged[int(result["row_id"])] = parsed
        compared_rows = set(judged) & set(human_by_run[source_run_id])
        disagreements = sum(
            judged[row_id] != human_by_run[source_run_id][row_id]
            for row_id in compared_rows
        )
        out[source_run_id].append(
            {
                "run_id": judge_run_id,
                "model": link["model"],
                "started_at": link["started_at"],
                "verdict_column_id": verdict_column_id,
                "passed": sum(judged.values()),
                "graded": len(judged),
                "compared": len(compared_rows),
                "disagreement_count": disagreements if compared_rows else None,
            }
        )
    return out


def _column_review_scores(
    project: Project, column_id: int, run_ids: list[int]
) -> dict[int, dict[str, Any]]:
    unique_run_ids = list(dict.fromkeys(run_ids))
    human = _human_scores(project, column_id, unique_run_ids)
    judges = _judge_scores(project, column_id, unique_run_ids)
    return {
        run_id: {"human_score": human[run_id], "judge_scores": judges[run_id]}
        for run_id in unique_run_ids
    }


def column_run_provenance_payload(
    project: Project,
    column_id: int,
    *,
    offset: int = 0,
    limit: int = 20,
) -> dict[str, Any]:
    column = project.db.execute(
        "SELECT * FROM columns WHERE id=?", (column_id,)
    ).fetchone()
    if column is None:
        raise KeyError("no such column")
    total = int(
        project.db.execute(
            "SELECT COUNT(DISTINCT r.id) FROM runs r JOIN results res "
            "ON res.run_id = r.id AND res.column_id = ?",
            (column_id,),
        ).fetchone()[0]
        or 0
    )
    rows = project.db.execute(
        "SELECT DISTINCT r.* FROM runs r JOIN results res "
        "ON res.run_id = r.id AND res.column_id = ? "
        "ORDER BY r.id DESC LIMIT ? OFFSET ?",
        (column_id, limit, offset),
    ).fetchall()
    generations = ResultGenerationStore(project)
    generation_managed = generations.is_generation_managed(column_id)
    current_run = None
    if generation_managed:
        current_run_id = None
    else:
        current_run_id = column["current_run_id"]
    if current_run_id is not None:
        current_run = project.db.execute(
            "SELECT r.* FROM runs r JOIN results res "
            "ON res.run_id = r.id AND res.column_id = ? "
            "WHERE r.id = ? LIMIT 1",
            (column_id, current_run_id),
        ).fetchone()
    latest_run_id = (
        generations.latest_applied_run_id(column_id)
        if generation_managed
        else current_run_id
    )
    latest_run = current_run if latest_run_id == current_run_id else None
    if latest_run is None and latest_run_id is not None:
        latest_run = project.db.execute(
            "SELECT * FROM runs WHERE id=?",
            (latest_run_id,),
        ).fetchone()
    score_run_ids = [int(row["id"]) for row in rows]
    if current_run is not None:
        score_run_ids.append(int(current_run["id"]))
    if latest_run is not None:
        score_run_ids.append(int(latest_run["id"]))
    review_scores = _column_review_scores(project, column_id, score_run_ids)
    return {
        "column": {
            "id": column["id"],
            "name": column["name"],
            "type": column["type"],
            "ai_generated": bool(column["ai_generated"]),
            "current_run_id": current_run_id,
            "latest_run_id": latest_run_id,
            "mixed_origins": (
                generations.has_mixed_origins(column_id)
                if generation_managed
                else False
            ),
        },
        **offset_page_meta(
            offset=offset,
            limit=limit,
            total=total,
            item_count=len(rows),
        ),
        "current_run": (
            public_column_run_payload(
                project,
                current_run,
                current_run_id=current_run_id,
                review_scores=review_scores,
            )
            if current_run is not None
            else None
        ),
        "current_run_loaded": any(row["id"] == current_run_id for row in rows),
        "latest_run": (
            public_column_run_payload(
                project,
                latest_run,
                current_run_id=current_run_id,
                review_scores=review_scores,
            )
            if latest_run is not None
            else None
        ),
        "latest_run_loaded": any(row["id"] == latest_run_id for row in rows),
        "runs": [
            public_column_run_payload(
                project,
                row,
                current_run_id=current_run_id,
                review_scores=review_scores,
            )
            for row in rows
        ],
    }
