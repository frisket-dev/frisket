from __future__ import annotations

from pathlib import Path

from frisket.contracts.action import Receipt
from frisket.engine.store import Project
from frisket.engine.store.output_claims import OutputColumnClaimStore
from frisket.engine.store.receipts import ReceiptStore
from frisket.engine.store.runs import RunResultStore


def _seed_claimed_writer(tmp_path: Path) -> tuple[Project, int, int, int, str]:
    project = Project.create(tmp_path / "renewal-probe.frisket")
    sheet_id = project.add_sheet("data")
    source_column_id = project.add_column(sheet_id, "source")
    output_column_id = project.add_column(
        sheet_id,
        "result",
        ai_generated=True,
    )
    row_id = project.add_rows(
        sheet_id,
        [{"source": "one"}],
        {"source": source_column_id},
    )[0]
    op_id = project.append_op("map", {"recipe": "python"})
    run_id = RunResultStore(project).start_run(
        op_id,
        sheet_id,
        "map.python",
        row_ids=[row_id],
    )
    receipt_id = "receipt_in_transaction_renewal"
    claim_token = f"output-claim:{receipt_id}"
    ReceiptStore(project).insert(
        Receipt(
            receipt_id=receipt_id,
            project_id="project",
            action_id="action_in_transaction_renewal",
            action_kind="map.python",
            status="running",
            run_id=run_id,
        )
    )
    claims, conflict = OutputColumnClaimStore(project).acquire(
        sheet_id=sheet_id,
        output_names=["result"],
        action_kind="map.python",
        receipt_id=receipt_id,
        run_id=run_id,
        claim_token=claim_token,
    )
    assert conflict is None
    assert len(claims) == 1
    project.db.execute(
        "INSERT INTO execution_attempts "
        "(id, run_id, seq, state, action_identity_hash, scope_json, created_at) "
        "VALUES ('attempt_in_transaction_renewal', ?, 0, 'dispatching', "
        "'test-action', '[]', datetime('now'))",
        (run_id,),
    )
    project.db.execute(
        "UPDATE runs SET current_attempt_id='attempt_in_transaction_renewal' "
        "WHERE id=?",
        (run_id,),
    )
    project.db.commit()
    return project, run_id, row_id, output_column_id, claim_token


def test_result_write_renews_claim_inside_callers_transaction(
    tmp_path: Path,
) -> None:
    """The write and lease renewal are one caller-owned transaction."""

    project, run_id, row_id, output_column_id, claim_token = _seed_claimed_writer(
        tmp_path
    )
    old_time = "2000-01-01T00:00:00+00:00"
    try:
        project.db.execute(
            "UPDATE output_column_claims SET renewed_at=?, lease_expires_at=? "
            "WHERE claim_token=?",
            (old_time, old_time, claim_token),
        )
        project.db.commit()

        project.db.execute("BEGIN IMMEDIATE")
        RunResultStore(project).write_results(
            run_id,
            [{"row_id": row_id, "column_id": output_column_id, "value": "one"}],
            writer_attempt_id="attempt_in_transaction_renewal",
            claim_token=claim_token,
            commit=False,
        )

        lease = project.db.execute(
            "SELECT renewed_at, lease_expires_at FROM output_column_claims "
            "WHERE claim_token=?",
            (claim_token,),
        ).fetchone()
        assert project.db.in_transaction
        assert lease is not None
        assert lease["renewed_at"] != old_time
        assert lease["lease_expires_at"] != old_time

        project.db.rollback()
        rolled_back = project.db.execute(
            "SELECT renewed_at, lease_expires_at FROM output_column_claims "
            "WHERE claim_token=?",
            (claim_token,),
        ).fetchone()
        assert rolled_back is not None
        assert tuple(rolled_back) == (old_time, old_time)
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM results WHERE run_id=?",
                (run_id,),
            ).fetchone()[0]
            == 0
        )
    finally:
        project.close()
