"""Terminal receipt publication includes review summaries in the transaction."""

from __future__ import annotations

import pytest

from frisket.contracts.action import Receipt
from frisket.engine.executor.project_run_terminalization import (
    UnclaimedRunTerminalAuthority,
    terminalize_project_run,
)
from frisket.engine.store import Project
from frisket.engine.store.runs import RunResultStore


@pytest.mark.parametrize("status", ["completed", "cancelled"])
def test_terminalizer_publishes_review_totals_without_lazy_read(tmp_path, status):
    project = Project.create(tmp_path / "terminal-review.frisket")
    try:
        sheet = project.add_sheet("Source")
        column = project.add_column(sheet, "answer", ai_generated=True)
        [row] = project.add_rows(sheet, [{}], {})
        op = project.append_op("map.extract")
        run = RunResultStore(project).start_run(
            op, sheet, "map.extract", total_rows=1, row_ids=[row]
        )
        # One returned cached result, before the receipt owner closes the run.
        project.db.execute(
            "INSERT INTO results(run_id,row_id,column_id,value_kind,value) "
            "VALUES (?,?,?,'text','answer')",
            (run, row, column),
        )
        project.db.commit()
        receipt = Receipt(
            receipt_id="receipt_terminal_review",
            project_id="terminal-review",
            action_id="action_terminal_review",
            action_kind="map.extract",
            run_id=run,
            idempotency_key="terminal-review:stable",
            params_hash="sha256:terminal-review",
            status=status,
        )
        result = terminalize_project_run(
            project,
            run_id=run,
            receipt_id=receipt.receipt_id,
            status=status,
            authority=UnclaimedRunTerminalAuthority(claimless_direct_effect=True),
            terminal_receipt=receipt,
            receipt_source_statuses=None,
        )
        assert result.disposition == "terminalized"
        stored = project.db.execute(
            "SELECT review_stats_ready,review_bundle_count,review_resolved_bundle_count "
            "FROM runs WHERE id=?",
            (run,),
        ).fetchone()
        assert tuple(stored) == (1, 1, 0)
        assert (
            project.db.execute(
                "SELECT eligible_count FROM run_review_fields WHERE run_id=? AND column_id=?",
                (run, column),
            ).fetchone()[0]
            == 1
        )
    finally:
        project.close()
