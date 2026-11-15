"""Independent copies of saved outcomes; values and evidence stay referenced.

A copy is a Change, never a new provider execution or a mutable alias of the
source review judgment. Its source result is immutable and retained while any
copy owns it. Missing outcomes explicitly remain not completed.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import sqlite3

COPIED_RESULTS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS copied_result_outcomes (
  op_id INTEGER NOT NULL REFERENCES ops(id) ON DELETE RESTRICT,
  row_id INTEGER NOT NULL REFERENCES rows(id) ON DELETE CASCADE,
  column_id INTEGER NOT NULL REFERENCES columns(id) ON DELETE CASCADE,
  source_run_id INTEGER,
  source_row_id INTEGER,
  source_column_id INTEGER,
  review_state TEXT NOT NULL DEFAULT 'unreviewed',
  review_decision TEXT CHECK(review_decision IN ('accept','reject','reject_clear','edit')),
  review_note TEXT,
  PRIMARY KEY(op_id,row_id,column_id),
  FOREIGN KEY(source_run_id,source_row_id,source_column_id)
    REFERENCES results(run_id,row_id,column_id) ON DELETE RESTRICT,
  CHECK ((source_run_id IS NULL AND source_row_id IS NULL AND source_column_id IS NULL)
    OR (source_run_id IS NOT NULL AND source_row_id IS NOT NULL AND source_column_id IS NOT NULL))
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_copied_results_source
  ON copied_result_outcomes(source_run_id,source_row_id,source_column_id);
CREATE VIEW IF NOT EXISTS copied_result_values AS
SELECT copy.*,result.value_kind,result.value,result.publication_effect,
       result.outcome AS result_outcome,result.error,result.error_code,
       result.confidence,result.justification,result.warnings,
       CASE WHEN copy.source_run_id IS NULL THEN 'not_completed'
            WHEN result.publication_effect IN ('publish_value','publish_null') THEN 'completed'
            ELSE 'failed' END AS outcome
FROM copied_result_outcomes copy LEFT JOIN results result
  ON result.run_id=copy.source_run_id AND result.row_id=copy.source_row_id
 AND result.column_id=copy.source_column_id;
"""


@contextmanager
def combined_execution_outcomes(
    project, source_run_id: int, *, sheet_id: int, row_ids: list[int] | None
) -> Iterator[tuple[int, int]]:
    """Stage coherent latest recorded row outcomes, including absent outcomes.

    The caller owns the write transaction so a concurrent Retry cannot move the
    stopped execution while it is copied. Temp tables keep full-cohort selection
    off the Python heap. Yield (root run, unavailable selected row count).
    """
    from .execution_groups import execution_runs

    db = project.db
    if not db.in_transaction:
        raise RuntimeError("saved-result copying requires a caller transaction")
    root, group = execution_runs(project, source_run_id)
    source = db.execute("SELECT sheet_id FROM runs WHERE id=?", (root,)).fetchone()
    if source is None or source[0] != sheet_id:
        raise ValueError("Saved execution does not belong to the selected sheet")
    placeholders = ",".join("?" for _ in group)
    if db.execute(
        f"SELECT 1 FROM runs WHERE id IN ({placeholders}) AND status='running' LIMIT 1",
        group,
    ).fetchone():
        raise ValueError("Stop the execution before reapplying its results")
    db.execute("CREATE TEMP TABLE reapply_selected_rows(row_id INTEGER PRIMARY KEY)")
    db.execute(
        "CREATE TEMP TABLE reapply_combined_outcomes(row_id INTEGER,output_role TEXT,source_run_id INTEGER,source_row_id INTEGER,source_column_id INTEGER,PRIMARY KEY(row_id,output_role)) WITHOUT ROWID"
    )
    try:
        if row_ids is None:
            db.execute(
                "INSERT INTO reapply_selected_rows SELECT row_id FROM run_rows WHERE run_id=?",
                (root,),
            )
        else:
            db.executemany(
                "INSERT OR IGNORE INTO reapply_selected_rows VALUES (?)",
                ((int(row),) for row in row_ids),
            )
            if db.execute(
                "SELECT 1 FROM reapply_selected_rows chosen WHERE NOT EXISTS (SELECT 1 FROM run_rows scope WHERE scope.run_id=? AND scope.row_id=chosen.row_id) LIMIT 1",
                (root,),
            ).fetchone():
                raise ValueError(
                    "Reapply cannot include rows outside the original execution"
                )
        unavailable = db.execute(
            "SELECT count(*) FROM reapply_selected_rows chosen WHERE NOT EXISTS ("
            "SELECT 1 FROM rows row JOIN sheets sheet ON sheet.id=row.sheet_id "
            "WHERE row.id=chosen.row_id AND row.sheet_id=? AND row.hidden=0 AND sheet.hidden=0)",
            (sheet_id,),
        ).fetchone()[0]
        db.execute(
            "DELETE FROM reapply_selected_rows WHERE row_id NOT IN (SELECT id FROM rows WHERE sheet_id=? AND hidden=0)",
            (sheet_id,),
        )
        db.execute(
            "INSERT INTO reapply_combined_outcomes "
            "WITH latest AS (SELECT chosen.row_id,MAX(result.run_id) AS run_id "
            "FROM reapply_selected_rows chosen LEFT JOIN results result "
            f"ON result.row_id=chosen.row_id AND result.run_id IN ({placeholders}) "
            "GROUP BY chosen.row_id) "
            "SELECT latest.row_id,original.output_role,result.run_id,result.row_id,result.column_id "
            "FROM latest CROSS JOIN run_output_generations original "
            "LEFT JOIN run_output_generations selected ON selected.run_id=latest.run_id "
            "AND selected.output_role=original.output_role "
            "LEFT JOIN results result ON result.run_id=selected.run_id AND result.column_id=selected.column_id "
            "AND result.row_id=latest.row_id WHERE original.run_id=?",
            (*group, root),
        )
        yield root, int(unavailable)
    finally:
        db.execute("DROP TABLE reapply_combined_outcomes")
        db.execute("DROP TABLE reapply_selected_rows")


def copy_staged_outcomes(
    db: sqlite3.Connection, *, op_id: int, targets: dict[str, int]
) -> None:
    """Publish a complete selected family with copied, independently mutable judgments."""
    from .changes import start_change
    from .current_cells import refresh_current_cells

    if not db.in_transaction:
        raise RuntimeError("saved-result publication requires a caller transaction")
    start_change(db, op_id)
    for role, target in targets.items():
        db.execute(
            "INSERT INTO copied_result_outcomes(op_id,row_id,column_id,source_run_id,source_row_id,source_column_id,review_state,review_decision,review_note) "
            "SELECT ?,selected.row_id,?,selected.source_run_id,selected.source_row_id,selected.source_column_id,"
            "COALESCE(original.review_state,'unreviewed'),original.review_decision,original.review_note "
            "FROM reapply_combined_outcomes selected LEFT JOIN results original "
            "ON original.run_id=selected.source_run_id AND original.row_id=selected.source_row_id "
            "AND original.column_id=selected.source_column_id WHERE selected.output_role=?",
            (op_id, target, role),
        )
    refresh_current_cells(db, column_ids=list(targets.values()))
