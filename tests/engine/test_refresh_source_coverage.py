"""Source identity and completion of a refresh are independent facts."""

import json

import pytest

from frisket.engine.executor.actions import run_action_spec
from frisket.engine.store.staleness import sheet_freshness
from frisket.engine.store.op_log import step_operation
from test_sheet_refresh import _build, _refresh


@pytest.mark.parametrize(
    "status",
    [
        "running",
        "cancelled",
        "failed",
        "partial",
        "missing",
        "wrong_owner",
        "missing_marker",
    ],
)
def test_refresh_needs_its_exact_completed_receipt_for_whole_sheet_freshness(
    tmp_path, status
):
    project, _, _, child, _ = _build(tmp_path)
    try:
        result = run_action_spec(
            project,
            _refresh(sheet_id=child, key="coverage-refresh"),
            project_id="coverage",
        )
        assert result.status == "completed", result.errors
        owner = result.op_ids[0]
        source = project.db.execute(
            "SELECT capture_json FROM consumed_source_bundles WHERE owner_op_id=?",
            (owner,),
        ).fetchone()[0]
        spec = json.loads(
            project.db.execute("SELECT spec FROM ops WHERE id=?", (owner,)).fetchone()[
                0
            ]
        )
        spec["refresh_receipt_id"] = result.receipt_id
        project.db.execute(
            "UPDATE ops SET spec=? WHERE id=?", (json.dumps(spec), owner)
        )
        project.db.commit()
        assert sheet_freshness(project, child) == "synced"
        if status == "missing_marker":
            spec.pop("refresh_receipt_id")
            project.db.execute(
                "UPDATE ops SET kind='sheet.refresh',spec=? WHERE id=?",
                (json.dumps(spec), owner),
            )
        elif status == "missing":
            spec["refresh_receipt_id"] = "missing-receipt"
            project.db.execute(
                "UPDATE ops SET spec=? WHERE id=?", (json.dumps(spec), owner)
            )
        elif status == "wrong_owner":
            body = json.loads(
                project.db.execute(
                    "SELECT body FROM receipts WHERE id=?", (result.receipt_id,)
                ).fetchone()[0]
            )
            body["op_ids"] = []
            project.db.execute(
                "UPDATE receipts SET body=? WHERE id=?",
                (json.dumps(body), result.receipt_id),
            )
        else:
            project.db.execute(
                "UPDATE receipts SET status=? WHERE id=?", (status, result.receipt_id)
            )
        project.db.commit()
        assert sheet_freshness(project, child) == "unknown"
        assert (
            project.db.execute(
                "SELECT capture_json FROM consumed_source_bundles WHERE owner_op_id=?",
                (owner,),
            ).fetchone()[0]
            == source
        )
        with project.transaction_scope():
            step_operation(project, "revert", change_id=owner)
        assert sheet_freshness(project, child) == "synced"
    finally:
        project.close()
