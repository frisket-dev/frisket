from contextlib import closing

from frisket.contracts.action import Receipt
from frisket.engine.executor.project_run_terminalization import (
    CurrentWriterTerminalAuthority,
    terminalize_project_run,
)
from frisket.engine.store import Project
from frisket.engine.store.receipts import ReceiptStore
from frisket.engine.store.runs import RunResultStore
from frisket.execution.attempt import open_attempt


def test_current_writer_success_terminalization_maps_durable_stop_to_cancelled(
    tmp_path,
):
    with closing(Project.create(tmp_path / "stop-before-success.frisket")) as project:
        sheet_id = project.add_sheet("source")
        op_id = project.append_change("map.template", {"action_kind": "map.template"})
        run_id = RunResultStore(project).start_run(
            op_id,
            sheet_id,
            "map.template",
            total_rows=0,
            params={},
        )
        receipt_id = "receipt_stop_before_success"
        stored = Receipt(
            receipt_id=receipt_id,
            project_id="stop-before-success",
            action_id="action_stop_before_success",
            action_kind="map.template",
            run_id=run_id,
            idempotency_key="stop-before-success",
            params_hash="sha256:stop-before-success",
            status="running",
        )
        ReceiptStore(project).insert_running(stored)
        attempt_id, _seq = open_attempt(
            project,
            run_id=run_id,
            identity="stop-before-success",
            scope=(),
        )
        project.db.execute(
            "UPDATE execution_attempts SET state='dispatching' WHERE id=?",
            (attempt_id,),
        )
        project.db.execute(
            "UPDATE runs SET current_attempt_id=? WHERE id=?",
            (attempt_id, run_id),
        )
        project.db.commit()

        assert RunResultStore(project).request_cancel(run_id)
        terminal = stored.model_copy(update={"status": "completed"})
        result = terminalize_project_run(
            project,
            run_id=run_id,
            receipt_id=receipt_id,
            status="completed",
            authority=CurrentWriterTerminalAuthority(
                writer_attempt_id=attempt_id,
                claim_token=None,
                claimless_direct_effect=True,
            ),
            terminal_receipt=terminal,
        )

        assert result.disposition == "terminalized", result
        assert result.run_status == "cancelled"
        assert result.receipt_status == "cancelled"
        assert RunResultStore(project).get_run(run_id)["status"] == "cancelled"
        receipt = ReceiptStore(project).find_by_id(receipt_id)
        assert receipt is not None
        assert receipt.status == "cancelled"
        assert receipt.parsed().errors[0].code == "cancelled"
