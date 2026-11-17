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


def require_terminal_writer(
    project: Any,
    *,
    run_id: int,
    writer_attempt_id: str,
    claim_token: str | None,
    terminal_status: str,
    claimless_direct_effect: bool = False,
    output_column_ids=(),
) -> bool:
    """Close only the current attempt; closed Changes grant no publication rights.

    Undo releases output claims immediately. Its original writer still owns
    cancellation finalization, proved by the immutable generation declaration.
    Returns whether this is a closed-Change cancellation. This authority never
    permits new effects or writing result cells.
    """
    from frisket.execution.attempt import StaleAttemptWriter
    from .output_claims import OutputColumnClaimStore

    row = project.db.execute(
        "SELECT run.current_attempt_id,run.cancel_requested_at,op.publication_closed,attempt.state "
        "FROM runs run JOIN ops op ON op.id=run.op_id "
        "LEFT JOIN execution_attempts attempt ON attempt.id=? AND attempt.run_id=run.id WHERE run.id=?",
        (writer_attempt_id, run_id),
    ).fetchone()
    if row is None or row[0] != writer_attempt_id or row[3] != "dispatching":
        raise StaleAttemptWriter(
            "terminalization requires the current dispatching attempt"
        )
    closed_cancellation = (
        terminal_status == "cancelled" and row[1] is not None and bool(row[2])
    )
    try:
        OutputColumnClaimStore.require_current_writer(
            project.db,
            run_id=run_id,
            writer_attempt_id=writer_attempt_id,
            claim_token=claim_token,
            output_column_ids=frozenset(output_column_ids),
            claimless_direct_effect=claimless_direct_effect,
        )
    except StaleAttemptWriter:
        if not closed_cancellation:
            raise
        declarations = project.db.execute(
            "SELECT column_id,claim_token FROM run_output_generations WHERE run_id=?",
            (run_id,),
        ).fetchall()
        if claimless_direct_effect:
            if declarations or claim_token is not None or output_column_ids:
                raise StaleAttemptWriter(
                    "claimed cancellation cannot use claimless authority"
                )
        elif (
            not declarations
            or not claim_token
            or any(declaration[1] != claim_token for declaration in declarations)
            or not set(output_column_ids).issubset({item[0] for item in declarations})
        ):
            raise StaleAttemptWriter(
                "cancelled finalization requires its original output declaration"
            )
    return closed_cancellation
