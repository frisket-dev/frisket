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
