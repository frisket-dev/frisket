"""Transaction-owned closure effects shared by run completion paths."""

from __future__ import annotations

from typing import Any


def close_run_change(project: Any, run_id: int) -> None:
    """Close a run's Change and invalidate its declared result scope.

    Run status, Change publication authority, and the current-cell repair
    worklist are one SQLite transition. Callers must already own the write
    transaction so recovery and ordinary completion have identical effects.
    """

    db = project.db
    if not db.in_transaction:
        raise RuntimeError("run Change closure requires a caller-owned transaction")
    run = db.execute("SELECT op_id FROM runs WHERE id=?", (int(run_id),)).fetchone()
    if run is None:
        raise ValueError(f"unknown run {int(run_id)}")

    from frisket.engine.store.cell_projection import invalidate_run_scopes
    from frisket.engine.store.changes import ChangeStore

    ChangeStore(project).close(int(run["op_id"]))
    columns = db.execute(
        "SELECT column_id FROM run_output_generations WHERE run_id=? ORDER BY column_id",
        (int(run_id),),
    ).fetchall()
    invalidate_run_scopes(
        db,
        int(run_id),
        [int(row["column_id"]) for row in columns],
    )
