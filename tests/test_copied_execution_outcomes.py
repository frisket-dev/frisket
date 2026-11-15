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
    with project.transaction_scope():
        target = project.add_column(sheet, "Copy", commit=False)
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


def test_completion_never_stitches_successful_siblings_from_different_attempts(work):
    from frisket.engine.store.runs import RunResultStore
    from frisket.engine.store.result_generations import ResultGenerationStore
    from frisket.engine.store.output_claims import OutputColumnClaimStore
    from frisket.engine.store.execution_groups import completed_execution_rows
    from helpers import run_writer_authority_fixture

    project, sheet, _, rows, *_ = work
    columns = [project.add_column(sheet, name) for name in ("A", "B")]
    runs = RunResultStore(project)
    generations = ResultGenerationStore(project)
    root = None
    run_ids = []
    for successful in (0, 1):
        op = project.append_op("map.extract")
        run_id = runs.start_run(
            op,
            sheet,
            "map.extract",
            row_ids=[rows[0]],
            total_rows=1,
            params={} if root is None else {"execution_root_run_id": root},
        )
        root = root or run_id
        run_ids.append(run_id)
        auth = run_writer_authority_fixture(
            project, run_id, output_column_ids=set(columns)
        )
        for index, column in enumerate(columns):
            generations.declare(
                run_id,
                column,
                output_role=("a", "b")[index],
                compatibility_key="sha256:test-family",
                write_mode="create" if len(run_ids) == 1 else "replace_scope",
                claim_token=auth.claim_token,
            )
        runs.write_results(
            run_id,
            [
                {
                    "row_id": rows[0],
                    "column_id": column,
                    "value": f"answer {index}" if index == successful else None,
                    "outcome": "ok" if index == successful else "model_error",
                    "error": None if index == successful else "failed sibling",
                    "publication_effect": "publish_value"
                    if index == successful
                    else "publish_error",
                }
                for index, column in enumerate(columns)
            ],
            **auth.kwargs(),
        )
        generations.seal(
            run_id,
            columns,
            claim_token=auth.claim_token,
            terminal_disposition="partial",
        )
        runs.finish_run(run_id, status="failed")
        OutputColumnClaimStore(project).release(claim_token=auth.claim_token)
    assert completed_execution_rows(project, root, run_ids) == set()
    with (
        project.transaction_scope(),
        combined_execution_outcomes(project, root, sheet_id=sheet, row_ids=[rows[0]]),
    ):
        staged = project.db.execute(
            "SELECT source_run_id,source_column_id FROM reapply_combined_outcomes ORDER BY output_role"
        ).fetchall()
        assert [tuple(row) for row in staged] == [
            (run_ids[-1], columns[0]),
            (run_ids[-1], columns[1]),
        ]


def test_copied_values_and_provenance_reach_bounded_and_full_readers(work):
    from frisket.engine.store.current_value_windows import read_current_column

    project, sheet, _, rows, _, adapter, run = work
    adapter.fail = "second"
    original = start(run)
    source_column = next(
        c["id"] for c in project.columns(sheet) if c["name"] == "Answer"
    )
    op, target, _ = _copy(work, original.run_id)
    values, refs = project.get_values_with_refs(sheet, target, row_ids=rows)
    bounded = read_current_column(
        project.db, sheet_id=sheet, column_id=target, row_ids=rows, bounded=True
    )
    for row in rows:
        expected = {
            "run_id": original.run_id,
            "row_id": row,
            "column_id": source_column,
        }
        assert refs[row]["kind"] == "copied_result"
        assert refs[row]["op_id"] == op
        assert refs[row]["retained_result"] == expected
        assert bounded[row].value == values[row]
        assert bounded[row].value_ref["retained_result"] == expected
        assert bounded[row].outcome == refs[row]["outcome"]
    assert bounded[rows[0]].value == "answer 1"
    assert bounded[rows[1]].value_present is False


def test_queued_retry_blocks_copy_before_a_successor_run_exists(work):
    from types import SimpleNamespace
    from frisket.contracts.action import Receipt
    from frisket.engine.executor.action_reservations import (
        _acquire_output_claims_for_runner,
    )
    from frisket.engine.store.receipts import ReceiptStore
    from frisket.engine.store.output_claims import OutputColumnClaimStore

    project, _, _, _, _, _, run = work
    original = start(run)
    receipt = Receipt(
        receipt_id="receipt_queued_retry",
        project_id="project",
        action_id="act_retry",
        action_kind="run.retry",
        idempotency_key="queued-retry",
        params_hash="retry",
        status="queued",
    )
    ReceiptStore(project).insert_queued(receipt)
    assert (
        _acquire_output_claims_for_runner(
            project,
            SimpleNamespace(kind="run.retry", idempotency_key="queued-retry"),
            {"sheet_id": work[1], "execution_root_run_id": original.run_id},
            receipt_id=receipt.receipt_id,
            output_fields=[{"name": "Answer", "column_type": "text"}],
        )
        is None
    )
    assert project.db.execute("SELECT count(*) FROM runs").fetchone()[0] == 1
    with pytest.raises(ValueError, match="Stop the execution"):
        _copy(work, original.run_id)
    claim = project.db.execute(
        "SELECT claim_token FROM output_column_claims WHERE receipt_id=?",
        (receipt.receipt_id,),
    ).fetchone()[0]
    OutputColumnClaimStore(project).release(claim_token=claim)
    project.db.execute(
        "UPDATE receipts SET status='cancelled' WHERE id=?", (receipt.receipt_id,)
    )
    project.db.commit()
    # No successor recorded any result; the original saved outcome still wins.
    _, target, _ = _copy(work, original.run_id)
    assert project.get_values(work[1], target)[work[3][0]] == "answer 1"
