"""Stopped-writer migration closes real project stores without losing results."""

import pytest

from frisket.contracts.action import Receipt
from frisket.engine.executor.execution_cutover import (
    ProjectCutoverRefused,
    retire_legacy_project_work,
)
from frisket.engine.store.effect_checkpoints import EffectCheckpointStore
from frisket.engine.store.project_qa import ProjectQAStore
from frisket.engine.store.receipts import ReceiptStore
from frisket.engine.store.result_generations import ResultGenerationStore
from frisket.engine.store.runs import RunResultStore
from frisket.execution.attempt import set_attempt_state
from test_result_generation_store import (
    _declare,
    _publish_initial_generation,
    _seed_project,
    _start_claimed_run,
    _write,
)


def _receipt(project, receipt_id, run_id=None):
    receipt = Receipt(
        receipt_id=receipt_id,
        project_id="cutover",
        action_id="action_" + receipt_id,
        action_kind="map.regex_extract" if run_id else "import.csv",
        run_id=run_id,
        idempotency_key="key_" + receipt_id,
        params_hash="sha256:cutover",
        status="running",
    )
    ReceiptStore(project).insert_running(receipt)
    if run_id is not None:
        project.db.execute(
            "UPDATE output_column_claims SET receipt_id=? WHERE run_id=?",
            (receipt_id, run_id),
        )
        project.db.commit()
    return receipt


def _claimed(tmp_path):
    project, sheet_id, column_id, rows = _seed_project(tmp_path)
    run = _start_claimed_run(
        project,
        sheet_id=sheet_id,
        output_column_id=column_id,
        row_ids=rows,
        label="incomplete legacy work",
    )
    _receipt(project, "receipt_live", run.run_id)
    generations = ResultGenerationStore(project)
    _declare(generations, run, column_id, write_mode="create")
    _write(
        project,
        run,
        [
            {
                "row_id": rows[0],
                "column_id": column_id,
                "value": "saved",
                "publication_effect": "publish_value",
            }
        ],
    )
    return project, run, column_id, rows


def test_cutover_keeps_saved_result_and_closes_generation_claim_and_receipt(tmp_path):
    project, run, column_id, rows = _claimed(tmp_path)
    try:
        before = [tuple(r) for r in project.db.execute("SELECT * FROM results")]
        operations = [tuple(r) for r in project.db.execute("SELECT * FROM ops")]
        result = retire_legacy_project_work(project)
        assert result == dict(
            runs=1, receipts=1, attempts=1, claims=1, checkpoints=0, turns=0
        )
        assert [tuple(r) for r in project.db.execute("SELECT * FROM results")] == before
        assert [tuple(r) for r in project.db.execute("SELECT * FROM ops")] == operations
        assert RunResultStore(project).get_run(run.run_id)["status"] == "cancelled"
        assert ReceiptStore(project).parsed_by_id("receipt_live").status == "cancelled"
        generation = ResultGenerationStore(project).get_binding(run.run_id, column_id)
        assert generation.state == "sealed"
        assert generation.terminal_disposition == "cancelled"
        assert (
            ResultGenerationStore(project).read_cell_heads(column_id)[rows[0]].value
            == "saved"
        )
        assert not project.db.execute(
            "SELECT 1 FROM output_column_claims WHERE status='active'"
        ).fetchone()
        assert RunResultStore(project).get_run(run.run_id)["current_attempt_id"] is None
        assert not any(retire_legacy_project_work(project).values())
    finally:
        project.close()


def test_cutover_retires_runless_attempt_and_chat_and_receiptless_local_run(tmp_path):
    project, sheet_id, _, rows = _seed_project(tmp_path)
    try:
        receipt = _receipt(project, "receipt_import")
        project.db.execute(
            "INSERT INTO execution_attempts (id,receipt_id,seq,state,action_identity_hash,scope_json,created_at) "
            "VALUES ('attempt_import',?,0,'admitted','import','[]',datetime('now'))",
            (receipt.receipt_id,),
        )
        project.db.commit()
        op_id = project.append_op("map.regex_extract", {})
        run_id = RunResultStore(project).start_run(
            op_id, sheet_id, "map.regex_extract", row_ids=rows
        )
        qa = ProjectQAStore(project)
        thread = qa.create_thread(title="Before upgrade")
        turn = qa.submit_turn(
            thread["id"],
            request_id="old-turn",
            question="Question",
            scope={"kind": "project"},
            model="openrouter/test",
            web=False,
            suggest_actions=False,
            submitted_by="reporter",
        )
        result = retire_legacy_project_work(project)
        assert (
            result["turns"]
            == result["attempts"]
            == result["receipts"]
            == result["runs"]
            == 1
        )
        assert qa.get_turn(turn["id"])["status"] == "interrupted"
        assert (
            ReceiptStore(project).parsed_by_id(receipt.receipt_id).status == "cancelled"
        )
        assert (
            project.db.execute(
                "SELECT state FROM execution_attempts WHERE id='attempt_import'"
            ).fetchone()[0]
            == "abandoned"
        )
        assert RunResultStore(project).get_run(run_id)["status"] == "cancelled"
    finally:
        project.close()


def test_cutover_absorbs_reserved_checkpoint_but_keeps_returned_evidence(tmp_path):
    project, run, _, rows = _claimed(tmp_path)
    try:
        checkpoints = EffectCheckpointStore(project.db)
        checkpoints.reserve(
            "uncertain",
            family="row_effect",
            group_key=f"run:{run.run_id}",
            unit_key=f"row:{rows[1]}",
            action_kind="map.regex_extract",
            identity="old-call",
            authorized_attempt_id=run.authority.writer_attempt_id,
            run_id=run.run_id,
            writer_attempt_id=run.authority.writer_attempt_id,
            claim_token=run.authority.claim_token,
            payload={"uncertain": True},
        )
        project.db.execute(
            "INSERT INTO effect_checkpoints (id,family,group_key,unit_key,action_kind,identity,authorized_attempt_id,state,payload) "
            "VALUES ('returned','row_effect',?,'retained','map.regex_extract','returned-call',?,'returned','{\"result\":\"retained\"}')",
            (f"run:{run.run_id}", run.authority.writer_attempt_id),
        )
        project.db.commit()
        returned = tuple(
            project.db.execute(
                "SELECT * FROM effect_checkpoints WHERE id='returned'"
            ).fetchone()
        )
        result = retire_legacy_project_work(project)
        assert result["checkpoints"] == 1
        assert checkpoints.get("uncertain") is None
        assert (
            tuple(
                project.db.execute(
                    "SELECT * FROM effect_checkpoints WHERE id='returned'"
                ).fetchone()
            )
            == returned
        )
    finally:
        project.close()


def test_cutover_preserves_completed_run_with_unfinished_receipt(tmp_path):
    project, run, _, _ = _claimed(tmp_path)
    try:
        RunResultStore(project).finish_run(run.run_id, "completed")
        retire_legacy_project_work(project)
        assert RunResultStore(project).get_run(run.run_id)["status"] == "completed"
        assert ReceiptStore(project).parsed_by_id("receipt_live").status == "cancelled"
        assert (
            project.db.execute(
                "SELECT value FROM results WHERE run_id=?", (run.run_id,)
            ).fetchone()[0]
            == "saved"
        )
    finally:
        project.close()


def test_cutover_retains_staged_results_without_replacing_published_values(tmp_path):
    project, sheet_id, column_id, rows = _seed_project(tmp_path)
    try:
        generations, first = _publish_initial_generation(
            project, sheet_id, column_id, rows
        )
        RunResultStore(project).finish_run(first.run_id)
        set_attempt_state(project, first.authority.writer_attempt_id, "effected")
        old_heads = generations.read_cell_heads(column_id)
        replacement = _start_claimed_run(
            project,
            sheet_id=sheet_id,
            output_column_id=column_id,
            row_ids=rows,
            label="unfinished replacement",
            replace_existing=True,
        )
        _receipt(project, "receipt_replacement", replacement.run_id)
        _declare(generations, replacement, column_id, write_mode="replace_scope")
        _write(
            project,
            replacement,
            [
                {
                    "row_id": rows[0],
                    "column_id": column_id,
                    "value": "staged result",
                    "publication_effect": "publish_value",
                }
            ],
        )
        result = retire_legacy_project_work(project)
        assert result["runs"] == 1
        assert generations.read_cell_heads(column_id) == old_heads
        assert RunResultStore(project).get_run(first.run_id)["status"] == "completed"
        assert (
            project.db.execute(
                "SELECT value FROM results WHERE run_id=?", (replacement.run_id,)
            ).fetchone()[0]
            == "staged result"
        )
        assert generations.get_binding(replacement.run_id, column_id).state == "sealed"
        assert not any(retire_legacy_project_work(project).values())
    finally:
        project.close()


def test_unsupported_output_owner_rolls_back_all_project_retirement(tmp_path):
    project, run, _, _ = _claimed(tmp_path)
    try:
        project.db.execute("UPDATE output_column_claims SET receipt_id=NULL")
        project.db.execute("DELETE FROM receipts")
        project.db.commit()
        before = list(project.db.iterdump())
        with pytest.raises(ProjectCutoverRefused, match="outputs without a receipt"):
            retire_legacy_project_work(project)
        assert list(project.db.iterdump()) == before
    finally:
        project.close()
