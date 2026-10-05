"""Bounded key selection for run review pages."""

from __future__ import annotations

import heapq
import sqlite3
from typing import TYPE_CHECKING, Any, TypedDict

from frisket.engine.store.runs import REVIEWABLE_OUTCOMES_SQL

if TYPE_CHECKING:
    from frisket.engine.store import Project


class ReviewBundleKey(TypedDict):
    run_id: int
    row_id: int
    sheet_id: int
    sheet_name: str
    action_kind: str
    model: str | None
    confidence: float | None


def _heap_key(
    confidence: float | None, row_id: int, column_id: int
) -> tuple[bool, float, int, int]:
    return (
        confidence is None,
        confidence if confidence is not None else 0.0,
        row_id,
        column_id,
    )


def select_confidence_bundle_keys(
    project: Project,
    *,
    run_id: int,
    sheet_id: int | None,
    limit: int,
    offset: int,
    include_reviewed: bool,
    field_id: int | None,
) -> list[ReviewBundleKey]:
    """Return one run's exact confidence page from indexed field streams.

    Each stream is ordered by cell confidence. The first occurrence of a row
    across their k-way merge is therefore that row's minimum confidence, which
    is the bundle rank used by the previous grouped query. Cursors stay lazy,
    so the merge stops after ``offset + limit`` distinct rows instead of
    grouping and sorting every result in the run.
    """

    if limit <= 0:
        return []
    run = project.db.execute(
        "SELECT runs.id,runs.sheet_id,runs.action_kind,runs.model FROM runs "
        "WHERE runs.id=? AND runs.status<>'running' "
        "AND EXISTS (SELECT 1 FROM ops review_op "
        "WHERE review_op.id=runs.op_id AND review_op.status='applied')",
        (run_id,),
    ).fetchone()
    if run is None or (sheet_id is not None and int(run["sheet_id"]) != sheet_id):
        return []

    field_where = (
        "review_field.run_id=? AND review_field.is_primary=1 "
        "AND review_field.eligible_count>0"
    )
    field_params: list[Any] = [run_id]
    if field_id is not None:
        field_where += " AND review_field.column_id=?"
        field_params.append(field_id)
    fields = project.db.execute(
        "SELECT review_field.column_id,columns.sheet_id,"
        "sheets.name AS sheet_name FROM run_review_fields review_field "
        "JOIN columns ON columns.id=review_field.column_id "
        "JOIN sheets ON sheets.id=columns.sheet_id "
        f"WHERE {field_where} "
        "ORDER BY review_field.column_position,review_field.column_id",
        field_params,
    ).fetchall()
    if not fields:
        return []

    review_where = (
        "1=1"
        if include_reviewed
        else "res.review_state='unreviewed' AND res.review_decision IS NULL"
    )
    candidate_sql = f"""
        SELECT res.row_id,res.confidence
        FROM results res
        JOIN columns c ON c.id=res.column_id
        JOIN rows rr ON rr.id=res.row_id AND rr.sheet_id=c.sheet_id
        WHERE res.run_id=? AND res.column_id=? AND c.sheet_id=?
          AND {review_where}
          AND res.outcome IN ({REVIEWABLE_OUTCOMES_SQL})
        ORDER BY res.confidence ASC NULLS LAST,res.row_id
        """

    cursors: list[sqlite3.Cursor] = []
    heap: list[tuple[bool, float, int, int, int, float | None]] = []
    for stream_index, field in enumerate(fields):
        column_id = int(field["column_id"])
        cursor = project.db.execute(
            candidate_sql, (run_id, column_id, int(field["sheet_id"]))
        )
        cursors.append(cursor)
        row = cursor.fetchone()
        if row is not None:
            confidence = (
                float(row["confidence"]) if row["confidence"] is not None else None
            )
            row_id = int(row["row_id"])
            null_rank, value_rank, _, column_rank = _heap_key(
                confidence, row_id, column_id
            )
            heapq.heappush(
                heap,
                (
                    null_rank,
                    value_rank,
                    row_id,
                    column_rank,
                    stream_index,
                    confidence,
                ),
            )

    target = offset + limit
    seen: set[int] = set()
    ranked: list[tuple[int, float | None, int]] = []
    while heap and len(ranked) < target:
        _null_rank, _value_rank, row_id, _column_id, stream_index, confidence = (
            heapq.heappop(heap)
        )
        if row_id not in seen:
            seen.add(row_id)
            ranked.append((row_id, confidence, stream_index))

        next_row = cursors[stream_index].fetchone()
        if next_row is not None:
            next_confidence = (
                float(next_row["confidence"])
                if next_row["confidence"] is not None
                else None
            )
            next_row_id = int(next_row["row_id"])
            column_id = int(fields[stream_index]["column_id"])
            null_rank, value_rank, _, column_rank = _heap_key(
                next_confidence, next_row_id, column_id
            )
            heapq.heappush(
                heap,
                (
                    null_rank,
                    value_rank,
                    next_row_id,
                    column_rank,
                    stream_index,
                    next_confidence,
                ),
            )

    keys: list[ReviewBundleKey] = []
    for row_id, confidence, stream_index in ranked[offset:target]:
        field = fields[stream_index]
        keys.append(
            ReviewBundleKey(
                run_id=run_id,
                row_id=row_id,
                sheet_id=int(field["sheet_id"]),
                sheet_name=str(field["sheet_name"]),
                action_kind=str(run["action_kind"]),
                model=str(run["model"]) if run["model"] is not None else None,
                confidence=confidence,
            )
        )
    return keys


__all__ = ["ReviewBundleKey", "select_confidence_bundle_keys"]
