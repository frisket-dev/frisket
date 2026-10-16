from __future__ import annotations

from pathlib import Path

from frisket.contracts.action import ActionResult, Receipt
from frisket.engine.executor.action_jobs import (
    ActionJobEnvelope,
    launch_queued_action_job,
)
from frisket.engine.jobs.queue import ACTION_RUN_KIND, SEARCH_INDEX_KIND, open_queue
from frisket.engine.jobs.runs import register_action_run_handler
from frisket.engine.jobs.search_index import (
    index_work_marker,
    register_search_index_handler,
)
from frisket.engine.jobs.worker import HandlerRegistry, Worker
from frisket.engine.store import Project
from frisket.engine.store.receipts import ReceiptStore
from frisket.search import drain_index, search_project
from frisket.server.services.action_runs import ActionRunService
from frisket.server.workspace import Workspace
from http_test_helpers import queued_python_run_spec, v1_action_from_canonical_run_spec


def _reserve_action(
    project_path: Path,
    *,
    project_id: str,
    action_kind: str,
) -> ActionJobEnvelope:
    receipt_id = f"receipt-{project_id}"
    project = Project.create(project_path, name="Queued derived search")
    try:
        ReceiptStore(project).insert_running(
            Receipt(
                receipt_id=receipt_id,
                project_id=project_id,
                action_id="act-derived-search",
                action_kind=action_kind,
                idempotency_key=f"{action_kind}@sha256:{project_id}",
                params_hash="sha256:derived-search",
                status="running",
            )
        )
    finally:
        project.close()
    return ActionJobEnvelope(
        action_kind=action_kind,
        action_id="act-derived-search",
        receipt_id=receipt_id,
        params_hash="sha256:derived-search",
        idempotency_key=f"{action_kind}@sha256:{project_id}",
        project_id=project_id,
        action={"kind": action_kind, "params": {}},
    )


def _completed_child_result(
    project: Project, envelope: ActionJobEnvelope
) -> ActionResult:
    sheet_id = project.add_sheet("People")
    value_column = project.add_column(sheet_id, "value", "text")
    project.add_rows(
        sheet_id,
        [{"value": "Devon Larratt — veteran"}],
        {"value": value_column},
    )
    return ActionResult(
        action={"kind": envelope.action_kind, "action_id": envelope.action_id},
        status="completed",
        project_id=envelope.project_id,
        receipt_id=envelope.receipt_id,
    )


def test_queued_completion_schedules_and_publishes_child_search(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    project_id = "queued-derived-search"
    project_path = root / f"{project_id}.frisket"
    action_kind = "test.derive_child"
    envelope = _reserve_action(
        project_path,
        project_id=project_id,
        action_kind=action_kind,
    )
    queue = open_queue(workspace=root)
    launched = launch_queued_action_job(
        envelope=envelope,
        queue=queue,
        job_kind=ACTION_RUN_KIND,
    )

    registry = HandlerRegistry()
    registry.register_action_executor(action_kind, _completed_child_result)
    register_action_run_handler(registry, workspace_root=root, queue=queue)
    register_search_index_handler(registry, workspace_root=root, queue=queue)
    worker = Worker(queue, registry, worker_id="queued-derived-search")

    assert worker.run_once() is True
    assert queue.get(launched.job_id).status == "done"
    scheduled = queue.list_project_jobs(project_id, kind=SEARCH_INDEX_KIND)
    assert len(scheduled) == 1
    assert scheduled[0].status == "queued"

    for _ in range(16):
        if not worker.run_once():
            break
    else:
        raise AssertionError("bounded retrieval maintenance did not become idle")

    project = Project(project_path)
    try:
        hits = search_project(project, "Devon", rerank="off")
        hit = next(hit for hit in hits if hit["column_name"] == "value")
        assert hit["column_name"] == "value"
        assert "Devon" in hit["snip"]
    finally:
        project.close()
        queue.close()


def test_queued_project_run_publishes_current_output_to_search(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    project_id = "queued-project-search"
    project_path = root / f"{project_id}.frisket"
    project = Project.create(project_path, name="Queued project search")
    sheet_id = project.add_sheet("People")
    last = project.add_column(sheet_id, "last", "text")
    project.add_rows(sheet_id, [{"last": "Lovelace"}], {"last": last})
    drain_index(project)
    project.close()

    workspace = Workspace(root, enable_local_model_pull=False)
    response = ActionRunService(workspace).run_action(
        project_id,
        v1_action_from_canonical_run_spec(
            queued_python_run_spec(sheet_id, "last", "display"),
            idempotency_key="queued-project-search@sha256:v1",
        ),
    )
    assert response.status_code == 200, response.payload
    assert response.payload["status"] == "queued"

    worker = Worker(
        workspace.queue,
        workspace.registry,
        worker_id="queued-project-search",
    )
    assert worker.run_once() is True
    scheduled = workspace.queue.list_project_jobs(
        project_id,
        kind=SEARCH_INDEX_KIND,
    )
    assert len(scheduled) == 1

    for _ in range(16):
        if not worker.run_once():
            break
    else:
        raise AssertionError("bounded retrieval maintenance did not become idle")

    project = Project(project_path)
    try:
        hits = search_project(project, "Lovelace", rerank="off")
        hit = next(hit for hit in hits if hit["column_name"] == "display")
        assert "Lovelace" in hit["snip"]
    finally:
        project.close()
        workspace.queue.close()


def test_cancelled_claim_does_not_enqueue_search_after_effect(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    project_id = "cancelled-derived-search"
    project_path = root / f"{project_id}.frisket"
    action_kind = "test.cancelled_derive_child"
    envelope = _reserve_action(
        project_path,
        project_id=project_id,
        action_kind=action_kind,
    )
    queue = open_queue(workspace=root)
    launched = launch_queued_action_job(
        envelope=envelope,
        queue=queue,
        job_kind=ACTION_RUN_KIND,
    )

    def publish_then_cancel(
        project: Project, claimed: ActionJobEnvelope
    ) -> ActionResult:
        result = _completed_child_result(project, claimed)
        assert queue.cancel(launched.job_id) is True
        return result

    registry = HandlerRegistry()
    registry.register_action_executor(action_kind, publish_then_cancel)
    register_action_run_handler(registry, workspace_root=root, queue=queue)

    assert Worker(queue, registry, worker_id="cancelled-derived-search").run_once()
    assert queue.get(launched.job_id).status == "cancelled"
    assert queue.list_project_jobs(project_id, kind=SEARCH_INDEX_KIND) == []

    project = Project(project_path)
    try:
        assert index_work_marker(project) is not None
    finally:
        project.close()
        queue.close()
