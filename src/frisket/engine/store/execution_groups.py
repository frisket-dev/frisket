"""Thin execution grouping over immutable output-generation runs.

The initial run is the execution identity; successor runs keep their individual
attempts, output claims and results. Legacy runs are singleton executions.
"""

import json


def execution_runs(project, run_id: int) -> tuple[int, list[int]]:
    source = project.db.execute(
        "SELECT params FROM runs WHERE id=?", (run_id,)
    ).fetchone()
    if source is None:
        raise ValueError("Execution run was not found")
    root = int(
        json.loads(source["params"] or "{}").get("execution_root_run_id", run_id)
    )
    ids = [
        row[0]
        for row in project.db.execute(
            "SELECT id FROM runs WHERE id=? OR json_extract(params,'$.execution_root_run_id')=? ORDER BY id",
            (root, root),
        )
    ]
    return root, ids


def completed_execution_rows(project, root: int, run_ids: list[int]) -> set[int]:
    """Completion belongs to one recorded row invocation, never stitched fields.

    Review judgments do not turn a successful answer into unfinished work. A
    scheduled retry with no result also does not erase an older recorded outcome.
    """
    slots = ",".join("?" for _ in run_ids)
    return {
        int(row[0])
        for row in project.db.execute(
            f"WITH latest AS (SELECT row_id,MAX(run_id) AS run_id FROM results WHERE run_id IN ({slots}) GROUP BY row_id) "
            "SELECT latest.row_id FROM latest CROSS JOIN run_output_generations original "
            "LEFT JOIN run_output_generations selected ON selected.run_id=latest.run_id "
            "AND selected.output_role=original.output_role "
            "LEFT JOIN results outcome ON outcome.run_id=selected.run_id "
            "AND outcome.column_id=selected.column_id AND outcome.row_id=latest.row_id "
            "WHERE original.run_id=? GROUP BY latest.row_id "
            "HAVING MIN(CASE WHEN outcome.outcome IN ('ok','empty') THEN 1 ELSE 0 END)=1",
            (*run_ids, root),
        )
    }
