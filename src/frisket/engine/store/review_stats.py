"""Compact, rebuildable review summaries and current-run eligibility."""

from __future__ import annotations

import re
import sqlite3

from frisket.review_predicate import is_support_column

from .runs import REVIEWABLE_OUTCOMES


_SQL_ALIAS = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_INCORRECT_DECISIONS = frozenset({"edit", "reject", "reject_clear"})


def _require_alias(alias: str) -> str:
    if not _SQL_ALIAS.fullmatch(alias):
        raise ValueError("review run SQL alias must be an identifier")
    return alias


def current_review_run_predicate(
    run_alias: str = "runs", *, terminal_only: bool = True
) -> str:
    """SQL predicate for a run with at least one still-current visible output.

    Managed outputs are current only through exact result heads. The legacy
    column pointer fallback explicitly excludes every managed column, so a
    stale pointer cannot retain a fully replaced managed run.
    """

    run = _require_alias(run_alias)
    terminal = f"{run}.status <> 'running' AND " if terminal_only else ""
    return (
        f"{terminal}"
        f"EXISTS (SELECT 1 FROM ops review_op WHERE review_op.id={run}.op_id "
        "AND review_op.status='applied') AND ("
        "EXISTS (SELECT 1 FROM cell_result_heads review_head "
        "JOIN columns review_column ON review_column.id=review_head.column_id "
        "JOIN sheets review_sheet ON review_sheet.id=review_column.sheet_id "
        f"WHERE review_head.run_id={run}.id AND review_column.active=1 "
        "AND review_column.hidden=0 AND review_sheet.hidden=0) OR "
        "EXISTS (SELECT 1 FROM columns review_column "
        "JOIN sheets review_sheet ON review_sheet.id=review_column.sheet_id "
        f"WHERE review_column.current_run_id={run}.id "
        "AND review_column.active=1 AND review_column.hidden=0 "
        "AND review_sheet.hidden=0 AND NOT EXISTS ("
        "SELECT 1 FROM run_output_generations review_generation "
        "WHERE review_generation.column_id=review_column.id)))"
    )


def mark_run_review_stats_dirty(db: sqlite3.Connection, run_id: int) -> None:
    db.execute(
        "UPDATE runs SET review_stats_ready=0 WHERE id=?",
        (int(run_id),),
    )


def _is_resolved(state: object, decision: object) -> bool:
    return state != "unreviewed" or decision is not None


def rebuild_run_review_stats(db: sqlite3.Connection, run_id: int) -> None:
    """Rebuild one run's stable aggregate without reclassifying known fields."""

    run_id = int(run_id)
    run = db.execute("SELECT action_kind FROM runs WHERE id=?", (run_id,)).fetchone()
    if run is None:
        raise ValueError(f"review stats run not found: {run_id}")
    action_kind = str(run["action_kind"])

    # Generations provide the declared output roster. The results union keeps
    # legacy/unmanaged runs and old direct writers representable.
    candidates = db.execute(
        "SELECT candidate.column_id,candidate.name,candidate.type,candidate.position "
        "FROM ("
        "SELECT generation.column_id,column_meta.name,column_meta.type,"
        "column_meta.position FROM run_output_generations generation "
        "JOIN columns column_meta ON column_meta.id=generation.column_id "
        "WHERE generation.run_id=? UNION "
        "SELECT result.column_id,column_meta.name,column_meta.type,"
        "column_meta.position FROM results result "
        "JOIN columns column_meta ON column_meta.id=result.column_id "
        "WHERE result.run_id=?"
        ") candidate ORDER BY candidate.position,candidate.column_id",
        (run_id, run_id),
    ).fetchall()
    known = {
        int(row["column_id"])
        for row in db.execute(
            "SELECT column_id FROM run_review_fields WHERE run_id=?", (run_id,)
        )
    }
    for candidate in candidates:
        column_id = int(candidate["column_id"])
        if column_id in known:
            continue
        name = str(candidate["name"])
        db.execute(
            "INSERT INTO run_review_fields "
            "(run_id,column_id,column_name,column_type,column_position,is_primary) "
            "VALUES (?,?,?,?,?,?)",
            (
                run_id,
                column_id,
                name,
                str(candidate["type"]),
                int(candidate["position"]),
                int(not is_support_column(name, action_kind=action_kind)),
            ),
        )

    db.execute(
        "UPDATE run_review_fields SET eligible_count=0,reviewed_count=0,"
        "accepted_count=0,incorrect_count=0,resolved_count=0,confidence_count=0 "
        "WHERE run_id=?",
        (run_id,),
    )
    outcomes = ",".join("?" for _ in REVIEWABLE_OUTCOMES)
    aggregates = db.execute(
        "SELECT result.column_id,COUNT(*) AS eligible_count,"
        "SUM(result.review_decision IS NOT NULL) AS reviewed_count,"
        "SUM(CASE WHEN result.review_decision='accept' THEN 1 ELSE 0 END) "
        "AS accepted_count,"
        "SUM(CASE WHEN result.review_decision IN "
        "('edit','reject','reject_clear') THEN 1 ELSE 0 END) AS incorrect_count,"
        "SUM(NOT (result.review_state='unreviewed' "
        "AND result.review_decision IS NULL)) AS resolved_count,"
        "SUM(result.confidence IS NOT NULL) AS confidence_count "
        "FROM results result JOIN run_review_fields field "
        "ON field.run_id=result.run_id AND field.column_id=result.column_id "
        f"WHERE result.run_id=? AND field.is_primary=1 AND result.outcome IN ({outcomes}) "
        "GROUP BY result.column_id",
        (run_id, *REVIEWABLE_OUTCOMES),
    ).fetchall()
    for aggregate in aggregates:
        db.execute(
            "UPDATE run_review_fields SET eligible_count=?,reviewed_count=?,"
            "accepted_count=?,incorrect_count=?,resolved_count=?,confidence_count=? "
            "WHERE run_id=? AND column_id=?",
            (
                int(aggregate["eligible_count"]),
                int(aggregate["reviewed_count"]),
                int(aggregate["accepted_count"]),
                int(aggregate["incorrect_count"]),
                int(aggregate["resolved_count"]),
                int(aggregate["confidence_count"]),
                run_id,
                int(aggregate["column_id"]),
            ),
        )

    bundles = db.execute(
        "SELECT COUNT(*) AS bundle_count,"
        "COALESCE(SUM(bundle.pending_count=0),0) AS resolved_bundle_count FROM ("
        "SELECT result.row_id,"
        "SUM(result.review_state='unreviewed' "
        "AND result.review_decision IS NULL) AS pending_count "
        "FROM results result JOIN run_review_fields field "
        "ON field.run_id=result.run_id AND field.column_id=result.column_id "
        f"WHERE result.run_id=? AND field.is_primary=1 AND result.outcome IN ({outcomes}) "
        "GROUP BY result.row_id) bundle",
        (run_id, *REVIEWABLE_OUTCOMES),
    ).fetchone()
    db.execute(
        "UPDATE runs SET review_stats_ready=1,review_bundle_count=?,"
        "review_resolved_bundle_count=? WHERE id=?",
        (
            int(bundles["bundle_count"] if bundles is not None else 0),
            int(bundles["resolved_bundle_count"] if bundles is not None else 0),
            run_id,
        ),
    )


def ensure_run_review_stats(db: sqlite3.Connection, run_id: int) -> None:
    """Heal a missing/dirty terminal summary; live runs remain writer-owned."""

    row = db.execute(
        "SELECT status,review_stats_ready FROM runs WHERE id=?", (int(run_id),)
    ).fetchone()
    if row is None:
        return
    if row["status"] != "running" and not bool(row["review_stats_ready"]):
        started_transaction = not db.in_transaction
        try:
            rebuild_run_review_stats(db, int(run_id))
            if started_transaction:
                db.commit()
        except BaseException:
            if started_transaction:
                db.rollback()
            raise


def adjust_review_stats_for_result_transition(
    db: sqlite3.Connection,
    *,
    run_id: int,
    row_id: int,
    column_id: int,
    before_state: object,
    before_decision: object,
    after_state: object,
    after_decision: object,
) -> None:
    """Apply one exact review transition to an already-ready run summary."""

    result = db.execute(
        "SELECT result.outcome,field.is_primary,runs.review_stats_ready "
        "FROM results result JOIN runs ON runs.id=result.run_id "
        "LEFT JOIN run_review_fields field ON field.run_id=result.run_id "
        "AND field.column_id=result.column_id "
        "WHERE result.run_id=? AND result.row_id=? AND result.column_id=?",
        (int(run_id), int(row_id), int(column_id)),
    ).fetchone()
    if (
        result is None
        or not bool(result["review_stats_ready"])
        or not bool(result["is_primary"])
        or str(result["outcome"]) not in REVIEWABLE_OUTCOMES
    ):
        return

    before_reviewed = before_decision is not None
    after_reviewed = after_decision is not None
    before_resolved = _is_resolved(before_state, before_decision)
    after_resolved = _is_resolved(after_state, after_decision)
    deltas = (
        int(after_reviewed) - int(before_reviewed),
        int(after_decision == "accept") - int(before_decision == "accept"),
        int(after_decision in _INCORRECT_DECISIONS)
        - int(before_decision in _INCORRECT_DECISIONS),
        int(after_resolved) - int(before_resolved),
    )
    if any(deltas):
        db.execute(
            "UPDATE run_review_fields SET reviewed_count=reviewed_count+?,"
            "accepted_count=accepted_count+?,incorrect_count=incorrect_count+?,"
            "resolved_count=resolved_count+? WHERE run_id=? AND column_id=?",
            (*deltas, int(run_id), int(column_id)),
        )

    outcomes = ",".join("?" for _ in REVIEWABLE_OUTCOMES)
    other_pending = db.execute(
        "SELECT 1 FROM results sibling JOIN run_review_fields field "
        "ON field.run_id=sibling.run_id AND field.column_id=sibling.column_id "
        "WHERE sibling.run_id=? AND sibling.row_id=? AND field.is_primary=1 "
        "AND sibling.column_id<>? "
        f"AND sibling.outcome IN ({outcomes}) "
        "AND sibling.review_state='unreviewed' "
        "AND sibling.review_decision IS NULL LIMIT 1",
        (int(run_id), int(row_id), int(column_id), *REVIEWABLE_OUTCOMES),
    ).fetchone()
    if other_pending is None:
        before_bundle_resolved = before_resolved
        after_bundle_resolved = after_resolved
        bundle_delta = int(after_bundle_resolved) - int(before_bundle_resolved)
        if bundle_delta:
            db.execute(
                "UPDATE runs SET review_resolved_bundle_count="
                "review_resolved_bundle_count+? WHERE id=?",
                (bundle_delta, int(run_id)),
            )


def pending_review_bundle_count(db: sqlite3.Connection) -> int:
    """Return stable pending bundles across current terminal runs."""

    predicate = current_review_run_predicate("runs")
    dirty = db.execute(
        f"SELECT runs.id FROM runs WHERE runs.review_stats_ready=0 AND {predicate}"
    ).fetchall()
    for row in dirty:
        ensure_run_review_stats(db, int(row["id"]))
    total = db.execute(
        "SELECT COALESCE(SUM(runs.review_bundle_count-"
        "runs.review_resolved_bundle_count),0) FROM runs "
        f"WHERE runs.review_stats_ready=1 AND {predicate}"
    ).fetchone()
    return int(total[0] if total is not None else 0)


__all__ = [
    "adjust_review_stats_for_result_transition",
    "current_review_run_predicate",
    "ensure_run_review_stats",
    "mark_run_review_stats_dirty",
    "pending_review_bundle_count",
    "rebuild_run_review_stats",
]
