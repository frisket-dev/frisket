"""Add and backfill compact per-run review summaries."""

from __future__ import annotations

import sqlite3

from frisket.review_predicate import primary_params, primary_where

from .runs import REVIEWABLE_OUTCOMES
from .schema import SCHEMA_DIGEST_META_KEY
from .typed_storage_migration import TYPED_VALUES_TO_DIGEST


REVIEW_STATS_FROM_DIGEST = TYPED_VALUES_TO_DIGEST
REVIEW_STATS_TO_DIGEST = "frisket.schema.v1:d6feced0e13e973220a1eb62a5f0c912"


def migrate_review_stats(db: sqlite3.Connection) -> None:
    """Install and backfill review summaries from the exact typed schema."""

    query = "SELECT value FROM meta WHERE key=?"
    try:
        row = db.execute(query, (SCHEMA_DIGEST_META_KEY,)).fetchone()
    except sqlite3.DatabaseError:
        return
    if row is None or row[0] != REVIEW_STATS_FROM_DIGEST:
        return

    db.execute("BEGIN IMMEDIATE")
    try:
        row = db.execute(query, (SCHEMA_DIGEST_META_KEY,)).fetchone()
        if row is not None and row[0] == REVIEW_STATS_FROM_DIGEST:
            db.execute(
                "ALTER TABLE runs ADD COLUMN review_stats_ready INTEGER NOT NULL "
                "DEFAULT 0 CHECK (review_stats_ready IN (0,1))"
            )
            db.execute(
                "ALTER TABLE runs ADD COLUMN review_bundle_count INTEGER NOT NULL "
                "DEFAULT 0 CHECK (review_bundle_count >= 0)"
            )
            db.execute(
                "ALTER TABLE runs ADD COLUMN review_resolved_bundle_count INTEGER "
                "NOT NULL DEFAULT 0 CHECK (review_resolved_bundle_count >= 0 "
                "AND review_resolved_bundle_count <= review_bundle_count)"
            )
            db.execute(
                "CREATE INDEX idx_columns_current_run ON columns(current_run_id,id) "
                "WHERE current_run_id IS NOT NULL"
            )
            db.execute(
                "CREATE TABLE run_review_fields ("
                "run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,"
                "column_id INTEGER NOT NULL REFERENCES columns(id) ON DELETE RESTRICT,"
                "column_name TEXT NOT NULL,column_type TEXT NOT NULL,"
                "column_position INTEGER NOT NULL,"
                "is_primary INTEGER NOT NULL CHECK (is_primary IN (0,1)),"
                "eligible_count INTEGER NOT NULL DEFAULT 0 CHECK (eligible_count >= 0),"
                "reviewed_count INTEGER NOT NULL DEFAULT 0 CHECK (reviewed_count >= 0),"
                "accepted_count INTEGER NOT NULL DEFAULT 0 CHECK (accepted_count >= 0),"
                "incorrect_count INTEGER NOT NULL DEFAULT 0 CHECK (incorrect_count >= 0),"
                "resolved_count INTEGER NOT NULL DEFAULT 0 CHECK (resolved_count >= 0),"
                "confidence_count INTEGER NOT NULL DEFAULT 0 CHECK (confidence_count >= 0),"
                "PRIMARY KEY (run_id,column_id),"
                "CHECK (reviewed_count <= eligible_count),"
                "CHECK (accepted_count + incorrect_count <= reviewed_count),"
                "CHECK (resolved_count <= eligible_count),"
                "CHECK (confidence_count <= eligible_count)"
                ") WITHOUT ROWID"
            )
            db.execute(
                "CREATE INDEX idx_cell_result_heads_run "
                "ON cell_result_heads(run_id,row_id,column_id)"
            )

            outcomes = ",".join("?" for _ in REVIEWABLE_OUTCOMES)
            primary = primary_where("column_meta", run_alias="run")
            db.execute(
                "WITH output_columns AS ("
                "SELECT generation.run_id,generation.column_id "
                "FROM run_output_generations generation UNION "
                "SELECT result.run_id,result.column_id FROM results result"
                "), classified AS ("
                "SELECT output.run_id,output.column_id,column_meta.name,"
                "column_meta.type,column_meta.position,"
                f"CASE WHEN {primary} THEN 1 ELSE 0 END AS is_primary "
                "FROM output_columns output JOIN runs run ON run.id=output.run_id "
                "JOIN columns column_meta ON column_meta.id=output.column_id"
                "), aggregate AS ("
                "SELECT result.run_id,result.column_id,COUNT(*) AS eligible_count,"
                "SUM(result.review_decision IS NOT NULL) AS reviewed_count,"
                "SUM(CASE WHEN result.review_decision='accept' THEN 1 ELSE 0 END) "
                "AS accepted_count,"
                "SUM(CASE WHEN result.review_decision IN "
                "('edit','reject','reject_clear') THEN 1 ELSE 0 END) "
                "AS incorrect_count,"
                "SUM(NOT (result.review_state='unreviewed' "
                "AND result.review_decision IS NULL)) AS resolved_count,"
                "SUM(result.confidence IS NOT NULL) AS confidence_count "
                "FROM results result JOIN classified field "
                "ON field.run_id=result.run_id AND field.column_id=result.column_id "
                "JOIN columns aggregate_column ON aggregate_column.id=result.column_id "
                "JOIN rows aggregate_row ON aggregate_row.id=result.row_id "
                "AND aggregate_row.sheet_id=aggregate_column.sheet_id "
                f"WHERE field.is_primary=1 AND result.outcome IN ({outcomes}) "
                "GROUP BY result.run_id,result.column_id"
                ") INSERT INTO run_review_fields "
                "(run_id,column_id,column_name,column_type,column_position,is_primary,"
                "eligible_count,reviewed_count,accepted_count,incorrect_count,"
                "resolved_count,confidence_count) "
                "SELECT field.run_id,field.column_id,field.name,field.type,"
                "field.position,field.is_primary,"
                "COALESCE(aggregate.eligible_count,0),"
                "COALESCE(aggregate.reviewed_count,0),"
                "COALESCE(aggregate.accepted_count,0),"
                "COALESCE(aggregate.incorrect_count,0),"
                "COALESCE(aggregate.resolved_count,0),"
                "COALESCE(aggregate.confidence_count,0) "
                "FROM classified field LEFT JOIN aggregate "
                "ON aggregate.run_id=field.run_id "
                "AND aggregate.column_id=field.column_id",
                (*primary_params(), *REVIEWABLE_OUTCOMES),
            )
            db.execute(
                "WITH bundles AS ("
                "SELECT result.run_id,result.row_id,"
                "SUM(result.review_state='unreviewed' "
                "AND result.review_decision IS NULL) AS pending_count "
                "FROM results result JOIN run_review_fields field "
                "ON field.run_id=result.run_id AND field.column_id=result.column_id "
                "JOIN columns bundle_column ON bundle_column.id=result.column_id "
                "JOIN rows bundle_row ON bundle_row.id=result.row_id "
                "AND bundle_row.sheet_id=bundle_column.sheet_id "
                f"WHERE field.is_primary=1 AND result.outcome IN ({outcomes}) "
                "GROUP BY result.run_id,result.row_id"
                "), totals AS ("
                "SELECT run_id,COUNT(*) AS bundle_count,"
                "SUM(pending_count=0) AS resolved_bundle_count "
                "FROM bundles GROUP BY run_id"
                ") UPDATE runs SET review_bundle_count=COALESCE(("
                "SELECT bundle_count FROM totals WHERE totals.run_id=runs.id),0),"
                "review_resolved_bundle_count=COALESCE(("
                "SELECT resolved_bundle_count FROM totals WHERE totals.run_id=runs.id),0),"
                "review_stats_ready=CASE WHEN status='running' THEN 0 ELSE 1 END",
                REVIEWABLE_OUTCOMES,
            )
            db.execute(
                "UPDATE meta SET value=? WHERE key=?",
                (REVIEW_STATS_TO_DIGEST, SCHEMA_DIGEST_META_KEY),
            )
        db.commit()
    except BaseException:
        db.rollback()
        raise


__all__ = [
    "REVIEW_STATS_FROM_DIGEST",
    "REVIEW_STATS_TO_DIGEST",
    "migrate_review_stats",
]
