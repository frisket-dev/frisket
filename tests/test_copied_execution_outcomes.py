"""Copies preserve stopped execution outcomes, without another model call."""

import pytest

import test_execution_lifecycle_recovery as recovery

from frisket.engine.store.copied_results import (
    combined_execution_outcomes,
    copy_staged_outcomes,
)


work = recovery.work
start = recovery.start

def _copy(work, source_run, selected=None):
    project, sheet, _, rows, *_ = work
    target = project.add_column(sheet, "Copy")
    with project.transaction_scope():
        op = project.append_op("run.reapply", commit=False)
        with combined_execution_outcomes(
            project, source_run, sheet_id=sheet, row_ids=selected
        ) as (_, unavailable):
            role = project.db.execute(
                "SELECT output_role FROM run_output_generations WHERE run_id=?",
                (source_run,),
            ).fetchone()[0]
            copy_staged_outcomes(project.db, op_id=op, targets={role: target})
    return op, target, unavailable


def test_copy_keeps_error_and_success_without_charging_or_calling(work):
    project, sheet, _, rows, calls, adapter, run = work
    adapter.fail = "second"
    original = start(run)
    assert original.run_id
    count = len(calls)
    op, target, unavailable = _copy(work, original.run_id)
    assert len(calls) == count
    assert unavailable == 0
    values = project.get_values(sheet, target, row_ids=rows)
    assert values[rows[0]] == "answer 1"
    assert values[rows[1]] is None
    copied = project.db.execute(
        "SELECT row_id,outcome,error FROM copied_result_values WHERE op_id=? ORDER BY row_id",
        (op,),
    ).fetchall()
    assert copied[0]["outcome"] == "completed"
    assert copied[1]["outcome"] == "failed"
    assert "Known rejection" in copied[1]["error"]
    assert project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1


def test_copied_review_is_independent_and_rejection_does_not_select_an_old_answer(work):
    project, sheet, _, rows, _, adapter, run = work
    original = start(run)
    column = next(c["id"] for c in project.columns(sheet) if c["name"] == "Answer")
    project.db.execute(
        "UPDATE results SET review_state='rejected',review_decision='reject',review_note='Source judgment' WHERE run_id=? AND row_id=? AND column_id=?",
        (original.run_id, rows[0], column),
    )
    project.db.commit()
    op, target, _ = _copy(work, original.run_id)
    copied = project.db.execute(
        "SELECT review_decision,review_note FROM copied_result_outcomes WHERE op_id=? AND row_id=?",
        (op, rows[0]),
    ).fetchone()
    assert tuple(copied) == ("reject", "Source judgment")
    project.db.execute(
        "UPDATE results SET review_decision='accept',review_note='Changed later' WHERE run_id=?",
        (original.run_id,),
    )
    project.db.commit()
    assert (
        project.db.execute(
            "SELECT review_decision FROM copied_result_outcomes WHERE op_id=? AND row_id=?",
            (op, rows[0]),
        ).fetchone()[0]
        == "reject"
    )
    assert project.get_values(sheet, target, row_ids=rows)[rows[0]] == "answer 1"


def test_active_execution_cannot_be_copied(work):
    project, sheet, _, rows, _, adapter, run = work
    original = start(run)
    project.db.execute(
        "UPDATE runs SET status='running' WHERE id=?", (original.run_id,)
    )
    project.db.commit()
    with pytest.raises(ValueError, match="Stop the execution"):
        _copy(work, original.run_id)


def test_missing_outcome_is_not_completed_and_later_source_results_do_not_change_copy(
    work,
):
    project, sheet, _, rows, calls, adapter, run = work
    from frisket.engine.store.runs import RunResultStore
    from frisket.engine.store.result_generations import ResultGenerationStore
    from frisket.engine.store.output_claims import OutputColumnClaimStore
    from helpers import run_writer_authority_fixture

    column = project.add_column(sheet, "Never completed")
    source_op = project.append_op("map.ask")
    store = RunResultStore(project)
    source_run = store.start_run(
        source_op, sheet, "map.ask", row_ids=rows, total_rows=len(rows)
    )
    auth = run_writer_authority_fixture(project, source_run, output_column_ids={column})
    generations = ResultGenerationStore(project)
    generations.declare(
        source_run,
        column,
        output_role="answer",
        compatibility_key="sha256:test",
        write_mode="create",
        claim_token=auth.claim_token,
    )
    generations.seal(
        source_run,
        [column],
        claim_token=auth.claim_token,
        terminal_disposition="cancelled",
    )
    store.finish_run(source_run, status="cancelled")
    OutputColumnClaimStore(project).release(claim_token=auth.claim_token)
    op, target, _ = _copy(work, source_run)
    assert (
        project.db.execute(
            "SELECT outcome FROM current_cell_values WHERE column_id=? AND row_id=?",
            (target, rows[1]),
        ).fetchone()[0]
        == "not_completed"
    )
    project.apply_edits([{"row_id": rows[1], "column_id": target, "value": "manual"}])
    assert project.get_values(sheet, target, row_ids=rows)[rows[1]] == "manual"
    project.undo()
    assert (
        project.db.execute(
            "SELECT outcome FROM current_cell_values WHERE column_id=? AND row_id=?",
            (target, rows[1]),
        ).fetchone()[0]
        == "not_completed"
    )
