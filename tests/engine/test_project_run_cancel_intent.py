from __future__ import annotations

import json
from pathlib import Path
from threading import Event, Thread
from typing import Any

from fastapi.testclient import TestClient
import pytest

from frisket.ai.llm import ModelRouter
from frisket.contracts.action import Receipt, ReceiptEvidence
from frisket.engine.executor.project_run_terminalization import (
    ProjectRunTerminalizationResult,
)
from frisket.engine.executor.queued_actions import queued_v1_terminal_receipt_result
from frisket.engine.executor.python_evaluator import AdmittedPythonEvaluator
from frisket.engine.jobs.queue import SqliteJobQueue
from frisket.engine.jobs.runs import RUN_PROJECT_KIND, register_project_run_handler
from frisket.engine.jobs.worker import HandlerRegistry, Worker
from frisket.engine.runner.map_runner import MapRunner
from frisket.engine.store import Project
from frisket.engine.store.output_claims import OutputColumnClaimStore
from frisket.engine.store.receipts import ReceiptStore
from frisket.engine.store.runs import RunResultStore
from frisket.server import run_status as run_status_module
from frisket.server.app import create_app
from frisket.server.services import action_run_cancel as cancel_service_module
from frisket.server.services.action_run_cancel import (
    ActionRunCancelRouteError,
    ActionRunCancelService,
)
from frisket.server.services.action_runs import ActionRunService
from frisket.server.workspace import Workspace
from http_test_helpers import (
    post_canonical_run_spec_as_v1_action,
    queued_python_run_spec,
    v1_action_from_canonical_run_spec,
)


CSV = "note\nfirst 101\nsecond 202\nthird 303\nfourth 404\n"


class _LiveJob:
    id = 71
    status = "running"
    payload: dict[str, object] = {}
    storage_org_id = None

    def __init__(self, *, receipt_id: str, action_kind: str) -> None:
        self.receipt_id = receipt_id
        self.action_kind = action_kind


class _LiveQueue:
    def __init__(self, job: _LiveJob) -> None:
        self.job = job

    def get(self, job_id: int):
        return self.job if job_id == self.job.id else None

    def get_project_run_job(self, *_args, **_kwargs):
        return self.job

    def cancel(self, _job_id: int) -> bool:
        raise AssertionError("a project.run live writer is not queue-cancellable")


class _CancelWorkspace:
    def __init__(self, project_id: str, project: Project, job: _LiveJob) -> None:
        self.project_id = project_id
        self.project = project
        self.queue = _LiveQueue(job)
        self.active_runs: dict[tuple[str, int], object] = {}
        self.run_jobs: dict[tuple[str, int], int] = {}
        self.queue_storage_org_id = None

    def get(self, project_id: str) -> Project:
        assert project_id == self.project_id
        return self.project


def test_reconciliation_response_names_operator_resolver_and_cancel_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = "cancel-reconciliation-response"
    project = Project.create(tmp_path / f"{project_id}.frisket")
    try:
        sheet_id = project.add_sheet("Source")
        source_column = project.add_column(sheet_id, "source")
        [row_id] = project.add_rows(
            sheet_id,
            [{"source": "alpha"}],
            {"source": source_column},
        )
        op_id = project.append_op(
            "map.regex_extract", {"action_kind": "map.regex_extract"}
        )
        run_id = RunResultStore(project).start_run(
            op_id,
            sheet_id,
            "map.regex_extract",
            row_ids=[row_id],
        )
        job = _LiveJob(
            receipt_id="receipt_reconciliation_response",
            action_kind="map.regex_extract",
        )
        workspace = _CancelWorkspace(project_id, project, job)
        terminalization = ProjectRunTerminalizationResult(
            disposition="reconciliation_required",
            receipt_disposition="write_refused",
            run_status="running",
            receipt_status="queued",
            reserved_checkpoint_ids=("checkpoint_operator_decision",),
            reason="reserved_effect_checkpoint",
        )
        run_row = RunResultStore(project).get_run(run_id)
        assert run_row is not None
        monkeypatch.setattr(
            cancel_service_module,
            "cancel_project_run",
            lambda **_kwargs: run_status_module.ProjectRunCancelResult(
                row=run_row,
                job=job,
                queue_cancelled=False,
                disposition="reconciliation_required",
                cancel_requested=True,
                terminalization=terminalization,
            ),
        )

        with pytest.raises(ActionRunCancelRouteError) as excinfo:
            ActionRunCancelService(workspace).cancel_run(  # type: ignore[arg-type]
                project_id,
                run_id,
            )

        assert excinfo.value.status_code == 409
        assert excinfo.value.content["status"] == "reconciliation_required"
        assert excinfo.value.content["reserved_checkpoint_ids"] == [
            "checkpoint_operator_decision"
        ]
        resolver = excinfo.value.content["resolver"]
        assert "frisket reconcile discard" in resolver
        assert "frisket reconcile accept-charged" in resolver
        assert "retry this cancel request" in resolver
    finally:
        project.close()


def test_live_cancel_service_returns_pending_then_owner_converges(
    tmp_path: Path,
) -> None:
    """The route service reports conflict, never an unexpected 500-shaped error."""

    project_id = "live-cancel-service"
    project = Project.create(tmp_path / f"{project_id}.frisket")
    try:
        sheet_id = project.add_sheet("Source")
        source_column = project.add_column(sheet_id, "note")
        project.add_column(sheet_id, "number", ai_generated=True)
        [row_id] = project.add_rows(
            sheet_id,
            [{"note": "first 101"}],
            {"note": source_column},
        )
        op_id = project.append_op(
            "map.regex_extract", {"action_kind": "map.regex_extract"}
        )
        run_id = RunResultStore(project).start_run(
            op_id,
            sheet_id,
            "map.regex_extract",
            row_ids=[row_id],
        )
        receipt_id = "receipt_live_cancel_service"
        attempt_id = "attempt_live_cancel_service"
        ReceiptStore(project).insert_queued(
            Receipt(
                receipt_id=receipt_id,
                project_id=project_id,
                action_id="action_live_cancel_service",
                action_kind="map.regex_extract",
                run_id=run_id,
                status="queued",
                evidence=[
                    ReceiptEvidence(
                        ref={
                            "kind": "queued_action_run_prepared",
                            "queue_kind": "project.run",
                            "run_id": run_id,
                            "attempt_id": attempt_id,
                        }
                    )
                ],
            )
        )
        claim_token = "claim:custom-live-cancel"
        claims, conflict = OutputColumnClaimStore(project).acquire(
            sheet_id=sheet_id,
            output_names=["number"],
            action_kind="map.regex_extract",
            receipt_id=receipt_id,
            run_id=run_id,
            op_id=op_id,
            claim_token=claim_token,
            lease_seconds=6 * 60 * 60,
        )
        assert conflict is None and len(claims) == 1
        project.db.execute(
            "INSERT INTO execution_attempts "
            "(id, run_id, seq, state, action_identity_hash, scope_json, created_at) "
            "VALUES (?, ?, 0, 'dispatching', 'live-cancel-service', ?, "
            "datetime('now'))",
            (attempt_id, run_id, f"[{row_id}]"),
        )
        project.db.execute(
            "UPDATE runs SET current_attempt_id=? WHERE id=?",
            (attempt_id, run_id),
        )
        project.db.commit()

        job = _LiveJob(receipt_id=receipt_id, action_kind="map.regex_extract")
        workspace = _CancelWorkspace(project_id, project, job)
        workspace.run_jobs[(project_id, run_id)] = job.id
        service = ActionRunCancelService(workspace)  # type: ignore[arg-type]

        with pytest.raises(ActionRunCancelRouteError) as excinfo:
            service.cancel_run(project_id, run_id)
        assert excinfo.value.status_code == 409
        assert excinfo.value.content["status"] == "cancel_pending"
        assert excinfo.value.content["cancel_requested"] is True
        live = project.db.execute(
            "SELECT status, finished_at, current_attempt_id, cancel_requested_at "
            "FROM runs WHERE id=?",
            (run_id,),
        ).fetchone()
        assert live is not None
        assert live["status"] == "running"
        assert live["finished_at"] is None
        assert live["current_attempt_id"] == attempt_id
        assert live["cancel_requested_at"] is not None

        queued_v1_terminal_receipt_result(
            project,
            {},
            project_id=project_id,
            run_id=run_id,
            status="cancelled",
            receipt_id=receipt_id,
            writer_attempt_id=attempt_id,
            claim_token=claim_token,
        )
        terminal_run = project.db.execute(
            "SELECT status, finished_at, current_attempt_id, cancel_requested_at "
            "FROM runs WHERE id=?",
            (run_id,),
        ).fetchone()
        terminal_receipt = project.db.execute(
            "SELECT status, body FROM receipts WHERE id=?",
            (receipt_id,),
        ).fetchone()
        terminal_attempt = project.db.execute(
            "SELECT state FROM execution_attempts WHERE id=?",
            (attempt_id,),
        ).fetchone()
        terminal_claim = project.db.execute(
            "SELECT status, released_at FROM output_column_claims WHERE run_id=?",
            (run_id,),
        ).fetchone()
        assert terminal_run is not None
        assert terminal_receipt is not None
        assert terminal_attempt is not None
        assert terminal_claim is not None
        assert terminal_run["status"] == "cancelled"
        assert terminal_run["finished_at"] is not None
        assert terminal_run["current_attempt_id"] is None
        assert terminal_run["cancel_requested_at"] is not None
        assert terminal_receipt["status"] == "cancelled"
        assert json.loads(terminal_receipt["body"])["status"] == "cancelled"
        assert terminal_attempt["state"] == "effected"
        assert terminal_claim["status"] == "cancelled"
        assert terminal_claim["released_at"] is not None
        before_repeat = (
            tuple(terminal_run),
            tuple(terminal_receipt),
            tuple(terminal_attempt),
            tuple(terminal_claim),
        )

        repeated = service.cancel_run(project_id, run_id)
        assert repeated["status"] == "cancelled"
        after_repeat = (
            tuple(
                project.db.execute(
                    "SELECT status, finished_at, current_attempt_id, "
                    "cancel_requested_at FROM runs WHERE id=?",
                    (run_id,),
                ).fetchone()
            ),
            tuple(
                project.db.execute(
                    "SELECT status, body FROM receipts WHERE id=?",
                    (receipt_id,),
                ).fetchone()
            ),
            tuple(
                project.db.execute(
                    "SELECT state FROM execution_attempts WHERE id=?",
                    (attempt_id,),
                ).fetchone()
            ),
            tuple(
                project.db.execute(
                    "SELECT status, released_at FROM output_column_claims "
                    "WHERE run_id=?",
                    (run_id,),
                ).fetchone()
            ),
        )
        assert after_repeat == before_repeat
    finally:
        project.close()


def test_legacy_cancelled_split_never_returns_false_success(tmp_path: Path) -> None:
    project_id = "legacy-cancelled-split"
    project = Project.create(tmp_path / f"{project_id}.frisket")
    try:
        sheet_id = project.add_sheet("Source")
        source_column = project.add_column(sheet_id, "note")
        project.add_column(sheet_id, "number", ai_generated=True)
        [row_id] = project.add_rows(
            sheet_id,
            [{"note": "first 101"}],
            {"note": source_column},
        )
        op_id = project.append_op(
            "map.regex_extract", {"action_kind": "map.regex_extract"}
        )
        run_id = RunResultStore(project).start_run(
            op_id,
            sheet_id,
            "map.regex_extract",
            row_ids=[row_id],
        )
        receipt_id = "receipt_legacy_cancelled_split"
        attempt_id = "attempt_legacy_cancelled_split"
        ReceiptStore(project).insert_queued(
            Receipt(
                receipt_id=receipt_id,
                project_id=project_id,
                action_id="action_legacy_cancelled_split",
                action_kind="map.regex_extract",
                run_id=run_id,
                status="queued",
                evidence=[
                    ReceiptEvidence(
                        ref={
                            "kind": "queued_action_run_prepared",
                            "queue_kind": "project.run",
                            "run_id": run_id,
                            "attempt_id": attempt_id,
                        }
                    )
                ],
            )
        )
        claim_token = "claim:legacy-cancelled-split"
        claims, conflict = OutputColumnClaimStore(project).acquire(
            sheet_id=sheet_id,
            output_names=["number"],
            action_kind="map.regex_extract",
            receipt_id=receipt_id,
            run_id=run_id,
            op_id=op_id,
            claim_token=claim_token,
            lease_seconds=6 * 60 * 60,
        )
        assert conflict is None and len(claims) == 1
        project.db.execute(
            "INSERT INTO execution_attempts "
            "(id, run_id, seq, state, action_identity_hash, scope_json, created_at) "
            "VALUES (?, ?, 0, 'dispatching', 'legacy-split', ?, datetime('now'))",
            (attempt_id, run_id, f"[{row_id}]"),
        )
        project.db.execute(
            "UPDATE runs SET status='cancelled', finished_at=datetime('now'), "
            "current_attempt_id=? WHERE id=?",
            (attempt_id, run_id),
        )
        project.db.commit()

        job = _LiveJob(receipt_id=receipt_id, action_kind="map.regex_extract")
        workspace = _CancelWorkspace(project_id, project, job)
        workspace.run_jobs[(project_id, run_id)] = job.id
        service = ActionRunCancelService(workspace)  # type: ignore[arg-type]

        with pytest.raises(ActionRunCancelRouteError) as excinfo:
            service.cancel_run(project_id, run_id)
        assert excinfo.value.status_code == 409
        assert excinfo.value.content["status"] == "cancel_pending"
        split = project.db.execute(
            "SELECT r.status, r.current_attempt_id, rc.status AS receipt_status, "
            "c.status AS claim_status FROM runs r "
            "JOIN receipts rc ON rc.id=? "
            "JOIN output_column_claims c ON c.run_id=r.id "
            "WHERE r.id=?",
            (receipt_id, run_id),
        ).fetchone()
        assert split is not None
        assert tuple(split) == ("cancelled", attempt_id, "queued", "active")

        queued_v1_terminal_receipt_result(
            project,
            {},
            project_id=project_id,
            run_id=run_id,
            status="cancelled",
            receipt_id=receipt_id,
            writer_attempt_id=attempt_id,
            claim_token=claim_token,
        )
        before_repeat = (
            tuple(
                project.db.execute(
                    "SELECT status, finished_at, current_attempt_id FROM runs WHERE id=?",
                    (run_id,),
                ).fetchone()
            ),
            tuple(
                project.db.execute(
                    "SELECT status, body FROM receipts WHERE id=?",
                    (receipt_id,),
                ).fetchone()
            ),
            tuple(
                project.db.execute(
                    "SELECT state FROM execution_attempts WHERE id=?",
                    (attempt_id,),
                ).fetchone()
            ),
            tuple(
                project.db.execute(
                    "SELECT status, released_at FROM output_column_claims "
                    "WHERE run_id=?",
                    (run_id,),
                ).fetchone()
            ),
        )
        repeated = service.cancel_run(project_id, run_id)
        assert repeated["status"] == "cancelled"
        after_repeat = (
            tuple(
                project.db.execute(
                    "SELECT status, finished_at, current_attempt_id FROM runs WHERE id=?",
                    (run_id,),
                ).fetchone()
            ),
            tuple(
                project.db.execute(
                    "SELECT status, body FROM receipts WHERE id=?",
                    (receipt_id,),
                ).fetchone()
            ),
            tuple(
                project.db.execute(
                    "SELECT state FROM execution_attempts WHERE id=?",
                    (attempt_id,),
                ).fetchone()
            ),
            tuple(
                project.db.execute(
                    "SELECT status, released_at FROM output_column_claims "
                    "WHERE run_id=?",
                    (run_id,),
                ).fetchone()
            ),
        )
        assert after_repeat == before_repeat
    finally:
        project.close()


@pytest.mark.parametrize(
    "intent_timing",
    ("before_attempt_load", "between_attempt_load_and_claim"),
)
def test_cancel_intent_before_dispatch_converges_through_unclaimed_kernel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    intent_timing: str,
) -> None:
    workspace_root = tmp_path / "pre-dispatch-cancel-workspace"
    workspace_root.mkdir()
    project_id = "pre-dispatch-cancel"
    seeded = Project.create(workspace_root / f"{project_id}.frisket")
    sheet_id = seeded.add_sheet("Source")
    source_column = seeded.add_column(sheet_id, "note")
    seeded.add_rows(
        sheet_id,
        [{"note": "first 101"}, {"note": "second 202"}],
        {"note": source_column},
    )
    seeded.close()

    queue = SqliteJobQueue(workspace_root / ".queue.db")
    registry = HandlerRegistry()
    workspace = Workspace(
        workspace_root,
        queue=queue,
        registry=registry,
        enable_local_model_pull=False,
    )
    project: Project | None = None
    try:
        launched_response = ActionRunService(workspace).run_action(
            project_id,
            v1_action_from_canonical_run_spec(
                queued_python_run_spec(sheet_id, "note", "number"),
                idempotency_key="pre-dispatch-cancel@sha256:stable",
            ),
        )
        assert launched_response.status_code == 200
        launched = launched_response.payload
        assert launched["status"] == "queued"
        run_id = int(launched["run_id"])
        receipt_id = str(launched["receipt_id"])
        job_id = int(launched["job_id"])
        project = workspace.get(project_id)
        if intent_timing == "before_attempt_load":
            assert RunResultStore(project).request_cancel(run_id)
        else:
            real_cancellation_requested = RunResultStore.cancellation_requested
            cancel_checks = 0

            def request_between_load_and_claim(
                store: RunResultStore,
                checked_run_id: int,
            ) -> bool:
                nonlocal cancel_checks
                cancel_checks += 1
                if cancel_checks == 1:
                    return False
                assert store.request_cancel(checked_run_id)
                return real_cancellation_requested(store, checked_run_id)

            monkeypatch.setattr(
                RunResultStore,
                "cancellation_requested",
                request_between_load_and_claim,
            )

        replacement_registry = HandlerRegistry()
        register_project_run_handler(
            replacement_registry,
            workspace_root=workspace_root,
            router=ModelRouter(cache=None, cache_mode="off"),
        )
        replacement = replacement_registry.get(RUN_PROJECT_KIND)
        assert replacement is not None
        registry.register(RUN_PROJECT_KIND, replacement)
        assert Worker(
            queue,
            registry,
            worker_id="pre-dispatch-cancel",
        ).run_once()

        job = queue.get(job_id)
        assert job is not None
        assert job.status == "done"
        assert job.result is not None
        assert job.result["status"] == "cancelled"
        run = project.db.execute(
            "SELECT status, finished_at, current_attempt_id, cancel_requested_at "
            "FROM runs WHERE id=?",
            (run_id,),
        ).fetchone()
        receipt = project.db.execute(
            "SELECT status, body FROM receipts WHERE id=?",
            (receipt_id,),
        ).fetchone()
        attempts = project.db.execute(
            "SELECT state FROM execution_attempts WHERE run_id=?",
            (run_id,),
        ).fetchall()
        claims = project.db.execute(
            "SELECT status, released_at FROM output_column_claims "
            "WHERE run_id=? ORDER BY id",
            (run_id,),
        ).fetchall()
        assert run is not None
        assert receipt is not None
        assert claims
        assert tuple(run)[:3] == ("cancelled", run["finished_at"], None)
        assert run["finished_at"] is not None
        assert run["cancel_requested_at"] is not None
        assert receipt["status"] == "cancelled"
        assert json.loads(receipt["body"])["status"] == "cancelled"
        assert {row["state"] for row in attempts} <= {"superseded"}
        if intent_timing == "between_attempt_load_and_claim":
            assert attempts
        assert {row["status"] for row in claims} == {"cancelled"}
        assert all(row["released_at"] is not None for row in claims)
    finally:
        if project is not None:
            project.close()
        queue.close()


def test_live_writer_cancel_records_intent_then_converges_within_one_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live writer owns terminalization; admin cancel only records intent."""

    client = TestClient(
        create_app(tmp_path / "workspace", run_status_grace_seconds=3600.0)
    )
    project_id = client.post(
        "/api/projects",
        json={"name": "Live writer cancel intent"},
    ).json()["id"]
    imported = client.post(
        f"/api/projects/{project_id}/import/csv",
        files={"file": ("rows.csv", CSV, "text/csv")},
    )
    assert imported.status_code == 200, imported.text
    sheet_id = int(imported.json()["sheet_id"])
    launched = post_canonical_run_spec_as_v1_action(
        client,
        project_id,
        queued_python_run_spec(sheet_id, "note", "number"),
        confirmed=True,
    )
    assert launched.status_code == 200, launched.text
    launch = launched.json()
    assert launch["status"] == "queued"
    run_id = int(launch["run_id"])
    receipt_id = str(launch["receipt_id"])

    project = client.app.state.workspace.get(project_id)
    entered = Event()
    release = Event()
    executed_rows: list[dict[str, object]] = []
    observed_attempt_ids: list[str] = []
    gate_errors: list[BaseException] = []
    original_evaluate = AdmittedPythonEvaluator.evaluate

    monkeypatch.setattr(
        MapRunner,
        "_row_worker_count",
        lambda _self, _recipe, _spec: 1,
    )

    async def gated_evaluate(
        self: AdmittedPythonEvaluator,
        *,
        code: str,
        row: dict[str, Any],
    ) -> Any:
        executed_rows.append(dict(row))
        if len(executed_rows) == 1:
            try:
                live = project.db.execute(
                    "SELECT r.current_attempt_id, a.state "
                    "FROM runs r JOIN execution_attempts a "
                    "ON a.id=r.current_attempt_id "
                    "WHERE r.id=?",
                    (run_id,),
                ).fetchone()
                assert live is not None
                assert live["state"] == "dispatching"
                observed_attempt_ids.append(str(live["current_attempt_id"]))
            except BaseException as exc:
                gate_errors.append(exc)
                entered.set()
                raise
            entered.set()
            if not release.wait(timeout=10):
                raise AssertionError("timed out waiting to release the live row")
        return await original_evaluate(self, code=code, row=row)

    monkeypatch.setattr(AdmittedPythonEvaluator, "evaluate", gated_evaluate)

    workspace = client.app.state.workspace
    worker = Worker(
        workspace.queue,
        workspace.registry,
        worker_id="cancel-intent-live-writer",
        lease_seconds=30.0,
    )
    worker_results: list[bool] = []
    worker_errors: list[BaseException] = []

    def run_worker() -> None:
        try:
            worker_results.append(worker.run_once())
        except BaseException as exc:  # capture a bounded background failure
            worker_errors.append(exc)

    worker_thread = Thread(target=run_worker, name="cancel-intent-live-writer")
    worker_thread.start()
    try:
        assert entered.wait(timeout=10), "worker never entered its first row"
        assert gate_errors == []
        assert len(observed_attempt_ids) == 1
        attempt_id = observed_attempt_ids[0]

        pending = client.post(
            f"/api/projects/{project_id}/actions/runs/{run_id}/cancel"
        )
        assert pending.status_code == 409, pending.text
        detail = pending.json()["detail"]
        assert detail["status"] == "cancel_pending"
        assert detail["cancel_requested"] is True

        live_run = project.db.execute(
            "SELECT status, finished_at, current_attempt_id, cancel_requested_at "
            "FROM runs WHERE id=?",
            (run_id,),
        ).fetchone()
        assert live_run is not None
        assert live_run["status"] == "running"
        assert live_run["finished_at"] is None
        assert live_run["current_attempt_id"] == attempt_id
        assert live_run["cancel_requested_at"] is not None
        assert (
            project.db.execute(
                "SELECT state FROM execution_attempts WHERE id=?",
                (attempt_id,),
            ).fetchone()["state"]
            == "dispatching"
        )
        assert {
            row["status"]
            for row in project.db.execute(
                "SELECT status FROM output_column_claims WHERE run_id=?",
                (run_id,),
            ).fetchall()
        } == {"active"}
    finally:
        release.set()
        worker_thread.join(timeout=15)

    assert not worker_thread.is_alive(), "worker did not converge after cancel intent"
    assert worker_errors == []
    assert worker_results == [True]
    assert len(executed_rows) == 1

    terminal_run = project.db.execute(
        "SELECT status, finished_at, completed_rows, current_attempt_id, "
        "cancel_requested_at FROM runs WHERE id=?",
        (run_id,),
    ).fetchone()
    assert terminal_run is not None
    assert terminal_run["status"] == "cancelled"
    assert terminal_run["finished_at"] is not None
    assert int(terminal_run["completed_rows"]) <= 1
    assert terminal_run["current_attempt_id"] is None
    assert terminal_run["cancel_requested_at"] is not None

    receipt = project.db.execute(
        "SELECT status, body FROM receipts WHERE id=?",
        (receipt_id,),
    ).fetchone()
    assert receipt is not None
    assert receipt["status"] == "cancelled"
    assert json.loads(receipt["body"])["status"] == "cancelled"
    assert (
        project.db.execute(
            "SELECT state FROM execution_attempts WHERE id=?",
            (attempt_id,),
        ).fetchone()["state"]
        == "effected"
    )
    terminal_claims = [
        tuple(row)
        for row in project.db.execute(
            "SELECT status, released_at FROM output_column_claims "
            "WHERE run_id=? ORDER BY id",
            (run_id,),
        ).fetchall()
    ]
    assert terminal_claims
    assert {status for status, _released_at in terminal_claims} == {"cancelled"}
    assert all(released_at is not None for _status, released_at in terminal_claims)

    before_repeat = (
        tuple(terminal_run),
        tuple(receipt),
        tuple(terminal_claims),
    )
    repeated = client.post(f"/api/projects/{project_id}/actions/runs/{run_id}/cancel")
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["status"] == "cancelled"
    after_repeat_run = project.db.execute(
        "SELECT status, finished_at, completed_rows, current_attempt_id, "
        "cancel_requested_at FROM runs WHERE id=?",
        (run_id,),
    ).fetchone()
    after_repeat_receipt = project.db.execute(
        "SELECT status, body FROM receipts WHERE id=?",
        (receipt_id,),
    ).fetchone()
    after_repeat_claims = [
        tuple(row)
        for row in project.db.execute(
            "SELECT status, released_at FROM output_column_claims "
            "WHERE run_id=? ORDER BY id",
            (run_id,),
        ).fetchall()
    ]
    assert after_repeat_run is not None
    assert after_repeat_receipt is not None
    assert (
        tuple(after_repeat_run),
        tuple(after_repeat_receipt),
        tuple(after_repeat_claims),
    ) == before_repeat
