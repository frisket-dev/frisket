from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from action_test_helpers import run_typed_map_request, typed_map_request
from frisket.contracts.action import Receipt
from frisket.engine.executor import queued_actions as queued_actions_module
from frisket.engine.executor.actions import run_action_spec as run_legacy_action_spec
from frisket.engine.executor.queued_actions import (
    queued_v1_finalize_action_result,
)
from frisket.engine.jobs.queue import QUEUE_DB_NAME, SqliteJobQueue
from frisket.engine.store import Project
from frisket.engine.store.output_claims import OutputColumnClaimStore
from frisket.engine.store.receipts import ReceiptStore
from frisket.engine.store.runs import RunResultStore
from frisket.execution.attempt import StaleAttemptWriter


def _seed_queued_receipt(
    project: Project,
    *,
    receipt_id: str,
    run_id: int | None = None,
) -> None:
    """Insert the minimal queued producer receipt needed by these claim tests."""
    ReceiptStore(project).insert_queued(
        Receipt(
            receipt_id=receipt_id,
            project_id=PROJECT_ID,
            action_id=f"act_{receipt_id}",
            action_kind="map.extract",
            run_id=run_id,
            status="queued",
        )
    )


PROJECT_ID = "project-queued-run-claim-release"


def _seed_project(tmp_path: Path) -> tuple[Project, int, dict[str, int]]:
    project = Project.create(
        tmp_path / "queued-claim-release.frisket", name="Queued Claim Release"
    )
    sheet_id = project.add_sheet("Notes")
    columns = {
        "name": project.add_column(sheet_id, "name", type="text"),
        "note": project.add_column(sheet_id, "note", type="text"),
    }
    project.add_rows(
        sheet_id,
        [{"name": "Ada", "note": "met the mayor"}],
        columns,
    )
    return project, sheet_id, columns


def _map_template_action(sheet_id: int, *, key: str) -> dict[str, Any]:
    return typed_map_request(
        "map.template",
        sheet_id,
        params={"template": {"text": "{{name}}: {{note}}"}},
        output_names={"rendered": "rendered_note"},
        idempotency_key=key,
    )


def run_action_spec(
    project: Project,
    action: dict[str, Any],
    *,
    project_id: str,
):
    if "action_id" in action:
        return run_typed_map_request(project, action, project_id=project_id)
    return run_legacy_action_spec(project, action, project_id=project_id)


def _undo_action(*, expected_op_id: int, key: str) -> dict[str, Any]:
    return {
        "action_id": "operation.undo",
        "scope": {"kind": "project"},
        "params": {"expected_op_id": expected_op_id},
        "idempotency_key": key,
    }


def _claim_status(project: Project, claim_token: str) -> tuple[str, str]:
    row = project.db.execute(
        "SELECT status, details FROM output_column_claims WHERE claim_token=?",
        (claim_token,),
    ).fetchone()
    assert row is not None
    return row["status"], row["details"]


# --- 2 & 3. claim-conflict-time recovery ------------------------------------


def _prepare_undo_with_synthetic_claim(
    tmp_path: Path,
) -> tuple[Project, int, str, SqliteJobQueue]:
    project, sheet_id, _columns = _seed_project(tmp_path)
    generated = run_action_spec(
        project,
        _map_template_action(sheet_id, key="map_template_for_undo@sha256:v1"),
        project_id=PROJECT_ID,
    )
    assert generated.status == "completed"
    assert generated.op_ids
    map_op_id = generated.op_ids[0]

    queue = SqliteJobQueue(tmp_path / QUEUE_DB_NAME)
    job_id = queue.enqueue("project.run", {"project_id": PROJECT_ID, "run_id": 1})
    claimed = queue.claim("recovery-test-worker", lease_seconds=60.0)
    assert claimed is not None
    assert claimed.id == job_id

    claim_token = "claim:stale-recovery-test"
    _claims, conflict = OutputColumnClaimStore(project).acquire(
        sheet_id=sheet_id,
        output_names=["rendered_note"],
        action_kind="map.extract",
        claim_token=claim_token,
        job_id=job_id,
        lease_seconds=None,
        details={"fixture": "synthetic-stale-claim"},
    )
    assert conflict is None
    status, _details = _claim_status(project, claim_token)
    assert status == "active"
    return project, map_op_id, claim_token, queue


def test_stale_active_claim_with_terminal_job_is_recovered(tmp_path: Path) -> None:
    project, map_op_id, claim_token, queue = _prepare_undo_with_synthetic_claim(
        tmp_path
    )
    try:
        job_id_row = project.db.execute(
            "SELECT job_id FROM output_column_claims WHERE claim_token=?",
            (claim_token,),
        ).fetchone()
        job_id = int(job_id_row["job_id"])
        assert queue.complete(job_id, "recovery-test-worker", result={"ok": True})
        assert queue.get(job_id).status == "done"

        undo = run_action_spec(
            project,
            _undo_action(expected_op_id=map_op_id, key="undo_recovers_stale@sha256:v1"),
            project_id=PROJECT_ID,
        )
        assert undo.status == "completed", undo.errors

        status, details_json = _claim_status(project, claim_token)
        assert status == "released"
        details = json.loads(details_json)
        assert details.get("recovered_by") == (
            "stale_claim_recovery: owning job terminal"
        )

        op = project.db.execute(
            "SELECT status FROM ops WHERE id=?", (map_op_id,)
        ).fetchone()
        assert op is not None
        assert op["status"] == "undone"
    finally:
        project.close()
        queue.close()


def test_running_job_claim_still_blocks(tmp_path: Path) -> None:
    project, map_op_id, claim_token, queue = _prepare_undo_with_synthetic_claim(
        tmp_path
    )
    try:
        # The job stays claimed ("running") -- never completed/failed/cancelled.
        undo = run_action_spec(
            project,
            _undo_action(
                expected_op_id=map_op_id, key="undo_blocked_by_running@sha256:v1"
            ),
            project_id=PROJECT_ID,
        )
        assert undo.status == "failed"
        assert undo.errors[0].code == "output_column_busy"
        assert undo.errors[0].details["requires_recovery"] is True

        status, _details = _claim_status(project, claim_token)
        assert status == "active"

        op = project.db.execute(
            "SELECT status FROM ops WHERE id=?", (map_op_id,)
        ).fetchone()
        assert op is not None
        assert op["status"] == "applied"
    finally:
        project.close()
        queue.close()


# --- 4. the finalize-seam fallback itself (white-box) -----------------------


def _seed_terminal_fallback_tuple(
    tmp_path: Path,
    *,
    suffix: str,
) -> tuple[Project, int, str, str]:
    project, sheet_id, _columns = _seed_project(tmp_path)
    generated = run_action_spec(
        project,
        _map_template_action(
            sheet_id,
            key=f"map_template_for_fallback_{suffix}@sha256:v1",
        ),
        project_id=PROJECT_ID,
    )
    assert generated.status == "completed"
    run_id = RunResultStore(project).start_run(
        generated.op_ids[0],
        sheet_id,
        "test.queued_run",
        total_rows=0,
    )
    RunResultStore(project).finish_run(run_id, "completed")

    receipt_id = f"receipt_fallback_{suffix}_0001"
    claim_token = f"claim:fallback-{suffix}"
    _seed_queued_receipt(project, receipt_id=receipt_id, run_id=run_id)
    claims, conflict = OutputColumnClaimStore(project).acquire(
        sheet_id=sheet_id,
        output_names=[f"fallback_output_{suffix}"],
        action_kind="map.extract",
        claim_token=claim_token,
        receipt_id=receipt_id,
        run_id=run_id,
        lease_seconds=None,
        details={"fixture": f"fallback-{suffix}"},
    )
    assert conflict is None
    assert len(claims) == 1
    return project, run_id, receipt_id, claim_token


def test_finalize_fallback_closes_run_bound_tuple_when_envelope_is_unreconstructable(
    tmp_path: Path,
) -> None:
    """Terminal-only schema drift closes the run-bound receipt and claim."""
    project, run_id, receipt_id, claim_token = _seed_terminal_fallback_tuple(
        tmp_path,
        suffix="envelope_loss",
    )
    try:
        status, _details = _claim_status(project, claim_token)
        assert status == "active"

        broken_payload = {
            "action_kind": "map.extract",
            "v1_receipt_id": receipt_id,
        }
        result = queued_v1_finalize_action_result(
            project, broken_payload, project_id=PROJECT_ID, run_id=run_id
        )
        assert result is not None
        assert result.status == "failed"
        assert result.receipt_id == receipt_id
        assert result.errors and result.errors[0].code == "map_run_failed"

        receipt_row = project.db.execute(
            "SELECT status FROM receipts WHERE id=?", (receipt_id,)
        ).fetchone()
        assert receipt_row is not None
        assert receipt_row["status"] == "failed"

        status, _details_json = _claim_status(project, claim_token)
        assert status == "failed"  # released terminally, no longer active
    finally:
        project.close()


def test_finalize_fallback_refuses_run_reopened_before_terminal_fence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, run_id, receipt_id, claim_token = _seed_terminal_fallback_tuple(
        tmp_path,
        suffix="reopen_race",
    )
    try:
        terminalize = queued_actions_module.queued_v1_terminal_receipt_result

        def reopen_then_terminalize(*args: Any, **kwargs: Any) -> Any:
            admission = RunResultStore(project).begin_run_resume(run_id)
            assert admission.admitted
            return terminalize(*args, **kwargs)

        monkeypatch.setattr(
            queued_actions_module,
            "queued_v1_terminal_receipt_result",
            reopen_then_terminalize,
        )

        with pytest.raises(
            StaleAttemptWriter,
            match="terminal_run_receipt_repair_has_writer",
        ):
            queued_v1_finalize_action_result(
                project,
                {
                    "action_kind": "map.extract",
                    "v1_receipt_id": receipt_id,
                },
                project_id=PROJECT_ID,
                run_id=run_id,
            )

        reopened = project.db.execute(
            "SELECT status, finished_at FROM runs WHERE id=?",
            (run_id,),
        ).fetchone()
        assert reopened is not None
        assert tuple(reopened) == ("running", None)
        receipt = ReceiptStore(project).find_by_id(receipt_id)
        assert receipt is not None
        assert receipt.status == "queued"
        status, _details = _claim_status(project, claim_token)
        assert status == "active"
    finally:
        project.close()


def test_finalize_fallback_is_a_noop_while_run_is_still_running(
    tmp_path: Path,
) -> None:
    project, sheet_id, _columns = _seed_project(tmp_path)
    try:
        generated = run_action_spec(
            project,
            _map_template_action(
                sheet_id, key="map_template_for_fallback_running@sha256:v1"
            ),
            project_id=PROJECT_ID,
        )
        op_id = generated.op_ids[0]
        run_id = RunResultStore(project).start_run(
            op_id, sheet_id, "test.queued_run", total_rows=0
        )
        # No finish_run: the run is still 'running'.

        claim_token = "claim:fallback-running-test"
        _seed_queued_receipt(project, receipt_id="receipt_fallback_running_0001")
        OutputColumnClaimStore(project).acquire(
            sheet_id=sheet_id,
            output_names=["fallback_output_running"],
            action_kind="map.extract",
            claim_token=claim_token,
            receipt_id="receipt_fallback_running_0001",
            lease_seconds=None,
            details={"fixture": "fallback-still-running"},
        )
        result = queued_v1_finalize_action_result(
            project,
            {
                "action_kind": "map.extract",
                "v1_receipt_id": "receipt_fallback_running_0001",
            },
            project_id=PROJECT_ID,
            run_id=run_id,
        )
        assert result is None  # still genuinely running: nothing to recover
        status, _details = _claim_status(project, claim_token)
        assert status == "active"
        receipt_row = project.db.execute(
            "SELECT status FROM receipts WHERE id=?",
            ("receipt_fallback_running_0001",),
        ).fetchone()
        assert receipt_row["status"] == "queued"
    finally:
        project.close()
