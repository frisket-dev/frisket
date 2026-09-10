"""Terminal run bookkeeping for the map runner (extracted from
``MapRunner._finalize_run``/``MapRunner._zero_success_created_columns_for_run``):
the one-transaction status write, atomic-family carry-forward, and zero-success
column visibility rollback that close out a run. Operates on an explicit
``project``/``run_store`` rather than an
implicit ``self`` so leaf callers need not import the engine."""

from __future__ import annotations

import json
from collections.abc import Collection

from frisket.engine.runner.column_retirement import (
    hide_zero_success_created_columns,
    reveal_recovered_columns,
)
from frisket.engine.store import Project
from frisket.engine.store.output_claims import OutputColumnClaimStore
from frisket.engine.store.result_generations import ResultGenerationStore
from frisket.engine.store.runs import EXPECTED_ROW_ERROR, RunResultStore
from frisket.execution.attempt import set_attempt_state


def _finalize_run_in_transaction(
    project: Project,
    run_store: RunResultStore,
    *,
    op_id: int,
    run_id: int,
    status: str,
    out_cols: dict[str, int],
    carry_forward_atomic_family: bool = False,
    keep_zero_success_output_columns: bool = False,
    halted_code: str | None = None,
    halted_reason: str | None = None,
    auto_verify: bool = False,
    terminal_attempt_id: str | None = None,
    terminal_attempt_state: str | None = None,
    defer_terminal_status: bool = False,
    defer_generation_seal: bool = False,
    generation_claim_token: str | None = None,
    never_point_column_ids: Collection[int] = (),
) -> None:
    """Terminal run bookkeeping after the caller has opened its transaction.

    The public entry points below decide whether this is a pre-dispatch close
    (no effect authority exists yet) or a dispatched close (the current-writer
    fence has already succeeded in this same transaction).

    The transaction includes an atomic family's untargeted result carry-forward,
    ``finish_run``'s status write, generation sealing, and the zero-success
    column rollback atomically. Either the whole finalize lands or none of it
    does, and the run stays ``running`` for the next attempt.

    The zero-success rollback is purely a VISIBILITY fix layered on top:
    for each output column THIS op created fresh (``ops.undo_info.
    created_columns`` — the same list undo/redo already hide/reveal), hide
    it only when the finished run has zero successful result cells for
    that column. Successful sibling outputs stay visible even if every row
    has some other field error. Columns this op only REUSED
    (``overwrite=True`` on an existing AI column) are left untouched —
    only genuinely new columns roll back, so a fixed-and-retried action
    later revives the same hidden column id via ``Project.add_column``'s
    hidden-revival path instead of leaving a duplicate. A cancelled run
    never reaches the rollback, so its sealed partial publication remains
    visible until a fresh scoped action replaces selected heads.
    """
    managed_column_ids = {
        int(row["column_id"])
        for row in project.db.execute(
            "SELECT column_id FROM run_output_generations WHERE run_id=?",
            (run_id,),
        ).fetchall()
    }
    if carry_forward_atomic_family:
        run_store.carry_forward_results_outside_scope(
            run_id,
            [
                column_id
                for column_id in out_cols.values()
                if int(column_id) not in managed_column_ids
            ],
            commit=False,
        )
    if auto_verify:
        run_store.mark_run_results_verified(run_id, commit=False)
    run_store.set_run_halt(
        run_id,
        halted_code,
        halted_reason,
        commit=False,
    )
    # A queued cooperative cancel first lands recipe-specific projection work
    # (carry-forward, column pointers, visibility) while retaining the live
    # writer tuple.  Its paired receipt owner then writes the actual terminal
    # run status together with receipt + attempt + claim in the terminalizer
    # kernel.  Every other path keeps the established one-step run close.
    if not defer_terminal_status:
        run_store.finish_run(run_id, status, commit=False)
    if managed_column_ids and not defer_generation_seal:
        if not isinstance(generation_claim_token, str) or not generation_claim_token:
            raise RuntimeError(
                "managed result generations require their exact claim at seal"
            )
        ResultGenerationStore(project).seal(
            run_id,
            sorted(managed_column_ids),
            claim_token=generation_claim_token,
            terminal_disposition=status,
            commit=False,
        )
    never_point = {int(column_id) for column_id in never_point_column_ids}
    if never_point:
        op = project.db.execute(
            "SELECT undo_info FROM ops WHERE id=?", (int(op_id),)
        ).fetchone()
        info = json.loads(op["undo_info"] or "{}") if op is not None else {}
        prior_pointers = info.get("column_pointers", {})
        after_pointers = info.get("column_pointers_after", {})
        changed_pointer_journal = False
        for cid in sorted(never_point):
            key = str(cid)
            if key not in prior_pointers:
                continue
            prior = prior_pointers[key]
            project.db.execute(
                "UPDATE columns SET current_run_id=? WHERE id=?",
                (prior, cid),
            )
            after_pointers[key] = prior
            changed_pointer_journal = True
        if changed_pointer_journal:
            info["column_pointers_after"] = after_pointers
            project.db.execute(
                "UPDATE ops SET undo_info=? WHERE id=?",
                (json.dumps(info), int(op_id)),
            )
    if status != "cancelled":
        zero_success_columns = (
            set()
            if keep_zero_success_output_columns
            else zero_success_created_columns_for_run(
                project,
                run_store,
                run_id,
                op_id,
                # Managed columns stay eligible on purpose (doc 14 owner
                # decision: fresh-column visibility keeps the CURRENT product
                # zero-success policy in this slice). Hiding is
                # visibility-only — heads, generations, and receipts are
                # untouched — and only freshly created columns can qualify
                # because candidates intersect the op's created_columns.
                out_cols,
            )
        )
        if zero_success_columns:
            hide_zero_success_created_columns(
                project,
                op_id,
                out_cols,
                only_columns=zero_success_columns,
            )
        # A successfully continued open run may recover a column this op hid.
        # Reveal recovered siblings without resurrecting fields that remain
        # zero-success.
        reveal_recovered_columns(
            project,
            op_id,
            out_cols,
            still_zero_success=zero_success_columns,
        )
    # else: still incomplete/resumable — leave visibility as-is.
    if terminal_attempt_id is not None:
        if terminal_attempt_state is None:
            raise ValueError("terminal attempt state is required with an attempt id")
        set_attempt_state(
            project,
            terminal_attempt_id,
            terminal_attempt_state,
            commit=False,
        )


def finalize_pre_dispatch_run(
    project: Project,
    run_store: RunResultStore,
    **kwargs: object,
) -> None:
    """Close a run that has not claimed dispatch authority."""

    try:
        project.db.execute("BEGIN IMMEDIATE")
        _finalize_run_in_transaction(project, run_store, **kwargs)
        project.db.commit()
    except Exception:
        project.db.rollback()
        raise
    project.refresh_pending_review_summary()


def finalize_dispatched_run(
    project: Project,
    run_store: RunResultStore,
    *,
    run_id: int,
    writer_attempt_id: str,
    claim_token: str | None,
    claimless_direct_effect: bool,
    terminal_attempt_state: str | None,
    **kwargs: object,
) -> None:
    """Fence and close terminal run state in one serialized transaction.

    ``terminal_attempt_state=None`` deliberately leaves the exact attempt
    dispatching for a caller-owned terminal receipt/checkpoint transaction.
    """

    try:
        project.db.execute("BEGIN IMMEDIATE")
        OutputColumnClaimStore.require_current_writer(
            project.db,
            run_id=run_id,
            writer_attempt_id=writer_attempt_id,
            claim_token=claim_token,
            output_column_ids=frozenset(
                int(column_id)
                for column_id in dict(kwargs.get("out_cols") or {}).values()
            ),
            claimless_direct_effect=claimless_direct_effect,
        )
        _finalize_run_in_transaction(
            project,
            run_store,
            run_id=run_id,
            generation_claim_token=claim_token,
            terminal_attempt_id=(
                writer_attempt_id if terminal_attempt_state is not None else None
            ),
            terminal_attempt_state=terminal_attempt_state,
            **kwargs,
        )
        project.db.commit()
    except Exception:
        project.db.rollback()
        raise
    project.refresh_pending_review_summary()


def zero_success_created_columns_for_run(
    project: Project,
    run_store: RunResultStore,
    run_id: int,
    op_id: int,
    out_cols: dict[str, int],
) -> set[int]:
    run = run_store.get_run(run_id)
    if run is None:
        return set()
    total_rows = int(run["total_rows"] or 0)
    if total_rows <= 0:
        return set()
    op = project.db.execute("SELECT undo_info FROM ops WHERE id=?", (op_id,)).fetchone()
    if op is None:
        return set()
    info = json.loads(op["undo_info"] or "{}")
    created = {int(c) for c in info.get("created_columns", [])}
    candidates = created & set(out_cols.values())
    if not candidates:
        return set()
    placeholders = ",".join("?" for _ in candidates)
    # An expected row error is an intentionally published diagnostic. Keep its
    # fresh column visible even when none of the source rows produced a value.
    visible_result_counts = {
        int(row["column_id"]): int(row["visible_results"])
        for row in project.db.execute(
            "SELECT column_id, COUNT(*) AS visible_results FROM results "
            f"WHERE run_id=? AND column_id IN ({placeholders}) "
            "AND (error IS NULL OR outcome=?) GROUP BY column_id",
            [run_id, *sorted(candidates), EXPECTED_ROW_ERROR],
        ).fetchall()
    }
    return {cid for cid in candidates if visible_result_counts.get(cid, 0) == 0}
