"""Retain the immutable answer identity of pre-cutover Reapply edits."""

from __future__ import annotations

import sqlite3


def migrate_reapplied_results(db: sqlite3.Connection) -> None:
    """Adopt exact, still-visible copies without inferring from equal values.

    Old Reapply stored an edit plus explicit source coordinates. A malformed,
    ambiguous, missing, or value-mismatched reference remains an ordinary edit;
    migration must never replace its visible value with a guessed old result.
    The caller owns the cutover transaction and captured winning references.
    """
    if not db.in_transaction:
        raise RuntimeError("Reapply cutover requires its caller's transaction")
    db.execute(
        """
        WITH operations AS MATERIALIZED (
          SELECT DISTINCT op.id,
                 CASE WHEN json_valid(op.spec) THEN op.spec ELSE '{}' END AS spec
          FROM domain_baseline_capture baseline JOIN ops op ON op.id=baseline.origin_op_id
          WHERE baseline.origin_kind='manual_edit'
        ), entries AS (
          SELECT op.id AS op_id,
                 CASE WHEN item.type='object' THEN item.value ELSE '{}' END AS ref
          FROM operations op JOIN json_each(
            CASE WHEN json_type(op.spec,'$.result_refs')='array'
                 THEN json_extract(op.spec,'$.result_refs') ELSE '[]' END
          ) item
          WHERE json_extract(op.spec,'$.action_kind')='run.reapply'
        ), references_typed AS (
          SELECT op_id,json_extract(ref,'$.run_id') AS source_run_id,
                 json_extract(ref,'$.row_id') AS row_id,
                 json_extract(ref,'$.column_id') AS source_column_id,
                 json_extract(ref,'$.target_column_id') AS column_id,
                 COUNT(*) OVER (PARTITION BY op_id,json_extract(ref,'$.row_id'),
                                             json_extract(ref,'$.target_column_id')) AS matches
          FROM entries
          WHERE json_type(ref,'$.run_id')='integer'
            AND json_type(ref,'$.row_id')='integer'
            AND json_type(ref,'$.column_id')='integer'
            AND json_type(ref,'$.target_column_id')='integer'
        )
        INSERT INTO copied_result_outcomes
          (op_id,row_id,column_id,source_run_id,source_row_id,source_column_id,
           review_state,review_decision,review_note)
        SELECT ref.op_id,ref.row_id,ref.column_id,ref.source_run_id,ref.row_id,
               ref.source_column_id,result.review_state,result.review_decision,result.review_note
        FROM references_typed ref
        JOIN domain_baseline_capture baseline
          ON baseline.origin_kind='manual_edit' AND baseline.origin_op_id=ref.op_id
         AND baseline.row_id=ref.row_id AND baseline.column_id=ref.column_id
        JOIN edits edit ON edit.op_id=ref.op_id AND edit.row_id=ref.row_id
                       AND edit.column_id=ref.column_id
        JOIN results result ON result.run_id=ref.source_run_id
                           AND result.row_id=ref.row_id AND result.column_id=ref.source_column_id
        LEFT JOIN prepared_content_ref_values prepared
          ON result.value_kind='prepared_content_ref' AND prepared.ref_id=result.value
        WHERE ref.matches=1
          AND (result.value_kind!='prepared_content_ref' OR prepared.ref_id IS NOT NULL)
          AND result.publication_effect IN ('publish_value','publish_null')
          AND edit.value_kind IS CASE WHEN result.publication_effect='publish_null'
                                      THEN 'null' WHEN result.value_kind='prepared_content_ref'
                                      THEN CASE WHEN prepared.value IS NULL THEN 'null' ELSE 'text' END
                                      ELSE result.value_kind END
          AND edit.value IS CASE WHEN result.publication_effect='publish_value'
                                 THEN CASE WHEN result.value_kind='prepared_content_ref'
                                           THEN prepared.value ELSE result.value END END
        """
    )
    # Copies replace the underlying saved answer too; the old latest-head
    # reference is not the reapplied answer and must not survive as a tied head.
    db.execute(
        "DELETE FROM domain_baseline_capture WHERE origin_kind!='manual_edit' "
        "AND (row_id,column_id) IN (SELECT row_id,column_id FROM copied_result_outcomes)"
    )
    for table in ("domain_baseline_capture", "current_cells"):
        db.execute(
            f"UPDATE {table} SET origin_kind='copied_result',origin_run_id=("
            "SELECT source_run_id FROM copied_result_outcomes copy "
            f"WHERE copy.op_id={table}.origin_op_id AND copy.row_id={table}.row_id "
            f"AND copy.column_id={table}.column_id),base_producer_id=NULL "
            "WHERE origin_kind='manual_edit' AND (origin_op_id,row_id,column_id) IN "
            "(SELECT op_id,row_id,column_id FROM copied_result_outcomes)"
        )
    db.execute(
        "UPDATE results SET published=1 WHERE (run_id,row_id,column_id) IN "
        "(SELECT source_run_id,source_row_id,source_column_id FROM copied_result_outcomes)"
    )
