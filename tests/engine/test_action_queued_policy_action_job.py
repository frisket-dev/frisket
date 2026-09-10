from __future__ import annotations

import dataclasses

import pytest


# --- the generic action.run job kind ---------------------------------------


def test_action_run_job_kind_exists_and_is_distinct_from_project_run() -> None:
    from frisket.engine.jobs.queue import ACTION_RUN_KIND, PROJECT_RUN_KIND

    assert ACTION_RUN_KIND == "action.run"
    assert ACTION_RUN_KIND != PROJECT_RUN_KIND


# --- ActionJobEnvelope contract --------------------------------------------


def test_action_job_envelope_carries_required_fields() -> None:
    from frisket.engine.executor.action_jobs import ActionJobEnvelope

    fields = {f.name for f in dataclasses.fields(ActionJobEnvelope)}
    required = {
        "schema_version",
        "action_kind",
        "action_id",
        "receipt_id",
        "params_hash",
        "idempotency_key",
        "project_id",
        # the action spec body, so the worker can validate/resolve at worker time
        "action",
        "actor",
        "capabilities",
        # exactly one of a frozen resolved snapshot or a worker-time resolve marker
        "resolved_snapshot",
        "resolve_phase",
    }
    missing = required - fields
    assert not missing, f"ActionJobEnvelope missing fields: {sorted(missing)}"


def test_action_job_envelope_is_json_serializable_and_redacted() -> None:
    import json

    from frisket.engine.executor.action_jobs import ActionJobEnvelope

    env = ActionJobEnvelope(
        action_kind="embedding.index_refresh",
        action_id="act_x",
        receipt_id="receipt_x",
        params_hash="sha256:abc",
        idempotency_key="emb@sha256:abc",
        project_id="p1",
        actor="user-1",
        capabilities=("project:write", "model:embed"),
        resolved_snapshot={"index_id": "idx_1", "row_ids": [1, 2]},
        resolve_phase="launch",
    )
    payload = env.to_json()
    assert payload["action_kind"] == "embedding.index_refresh"
    assert payload["receipt_id"] == "receipt_x"
    assert payload["resolve_phase"] == "launch"
    # strict JSON round-trip (deterministic, no non-standard floats)
    assert json.loads(json.dumps(payload, sort_keys=True, allow_nan=False)) == payload


def test_action_job_envelope_roundtrips_from_payload() -> None:
    from frisket.engine.executor.action_jobs import ActionJobEnvelope

    env = ActionJobEnvelope(
        action_kind="embedding.index_refresh",
        action_id="act_x",
        receipt_id="receipt_x",
        params_hash="sha256:abc",
        idempotency_key="emb@sha256:abc",
        project_id="p1",
        actor="user-1",
        capabilities=("project:write",),
        resolved_snapshot={"index_id": "idx_1"},
        resolve_phase="launch",
    )
    restored = ActionJobEnvelope.from_json(env.to_json())
    assert restored == env


# --- worker terminalization for generic action jobs ------------------------


def test_action_job_worker_terminalizes_on_all_outcomes() -> None:
    # The generic action.run worker must turn every terminal outcome into a
    # terminal receipt, never leaving a reserved receipt 'running' forever.
    from frisket.engine.executor import action_jobs

    for fn_name in (
        "action_job_success_result",
        "action_job_failure_result",
        "action_job_missing_handler_result",
        "action_job_cancelled_result",
        "action_job_exhausted_lease_result",
    ):
        assert hasattr(action_jobs, fn_name), fn_name


# --- behavioral: launch -> worker -> terminalize ---------------------------


def _reserve_action_job_receipt(project_path, project_id, *, kind, receipt_id):
    from frisket.contracts.action import Receipt
    from frisket.engine.store import Project
    from frisket.engine.store.receipts import ReceiptStore

    project = Project.create(project_path, name="ActionJob")
    try:
        ReceiptStore(project).insert_running(
            Receipt(
                receipt_id=receipt_id,
                project_id=project_id,
                action_id="act_x",
                action_kind=kind,
                idempotency_key=f"{kind}@sha256:x",
                params_hash="sha256:x",
                status="running",
            )
        )
    finally:
        project.close()


def _envelope(project_id, *, kind, receipt_id):
    from frisket.engine.executor.action_jobs import ActionJobEnvelope

    return ActionJobEnvelope(
        action_kind=kind,
        action_id="act_x",
        receipt_id=receipt_id,
        params_hash="sha256:x",
        idempotency_key=f"{kind}@sha256:x",
        project_id=project_id,
        action={"kind": kind, "params": {}},
    )


def test_action_job_launch_rejects_identity_overrides_before_enqueue() -> None:
    """Metadata may annotate a job, but it cannot re-parent its receipt.

    The worker executes the nested envelope while queue terminal hooks consume
    the flat receipt reference.  Letting ``payload_extra`` replace only the
    latter splits one job into two ownership identities.
    """
    from frisket.engine.executor.action_jobs import launch_queued_action_job
    from frisket.engine.jobs.queue import ACTION_RUN_KIND

    class RecordingQueue:
        called = False

        def enqueue(self, *_args, **_kwargs):
            self.called = True
            return 1

    queue = RecordingQueue()
    with pytest.raises(ValueError, match="v1_receipt_id"):
        launch_queued_action_job(
            envelope=_envelope(
                "project-a",
                kind="test.action_job_identity",
                receipt_id="receipt-a",
            ),
            queue=queue,
            job_kind=ACTION_RUN_KIND,
            payload_extra={"v1_receipt_id": "receipt-b"},
        )
    assert queue.called is False


def test_concurrent_action_job_reservation_rechecks_params_after_lock(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The transaction loser must not inherit a different normalized action."""
    import threading

    from frisket.contracts.action import ActionResult, ActionSpec
    from frisket.engine.executor.action_jobs import reserve_queued_action_job_receipt
    from frisket.engine.store import Project
    from frisket.engine.store.receipts import ReceiptStore

    project = Project.create(tmp_path / "reservation-race.frisket")
    key = "action-job-reservation-race@sha256:stable"
    actions = [
        ActionSpec(
            kind="test.action_job_reservation_race",
            params={"variant": variant},
            idempotency_key=key,
        )
        for variant in ("first", "second")
    ]
    barrier = threading.Barrier(2)
    local = threading.local()
    original_find = ReceiptStore.find_by_idempotency_key

    def synchronized_first_lookup(self, idempotency_key):
        existing = original_find(self, idempotency_key)
        if not getattr(local, "waited", False):
            local.waited = True
            barrier.wait(timeout=5)
        return existing

    monkeypatch.setattr(
        ReceiptStore,
        "find_by_idempotency_key",
        synchronized_first_lookup,
    )
    results: list[dict[str, str] | ActionResult] = []
    failures: list[BaseException] = []
    results_lock = threading.Lock()

    def reserve(action: ActionSpec) -> None:
        try:
            result = reserve_queued_action_job_receipt(
                project,
                action,
                project_id="project-reservation-race",
            )
            with results_lock:
                results.append(result)
        except BaseException as exc:  # pragma: no cover - asserted below
            with results_lock:
                failures.append(exc)

    # Two real SQLite connections must overlap to exercise the serialized
    # first-reservation race.
    # realtime: joined thread completion, not a clock window, is the assertion.
    threads = [threading.Thread(target=reserve, args=(action,)) for action in actions]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        assert all(not thread.is_alive() for thread in threads)
        assert failures == []
        assert len(results) == 2
        reservations = [result for result in results if isinstance(result, dict)]
        conflicts = [
            result
            for result in results
            if isinstance(result, ActionResult)
            and result.status == "failed"
            and result.errors
            and result.errors[0].code == "idempotency_conflict"
        ]
        assert len(reservations) == 1
        assert len(conflicts) == 1
    finally:
        project.close()


def test_action_job_replay_resumes_after_pre_enqueue_failure(tmp_path) -> None:
    from frisket.contracts.action import ActionSpec
    from frisket.engine.executor.action_jobs import (
        ActionJobEnvelope,
        launch_queued_action_job,
        reserve_queued_action_job_receipt,
    )
    from frisket.engine.jobs.queue import ACTION_RUN_KIND, open_queue
    from frisket.engine.store import Project
    from frisket.engine.store.receipts import ReceiptStore

    class FailingQueue:
        def enqueue(self, *_args, **_kwargs):
            raise RuntimeError("injected pre-enqueue failure")

    project_id = "project-pre-enqueue-resume"
    project = Project.create(tmp_path / f"{project_id}.frisket")
    queue = open_queue(workspace=tmp_path)
    action = ActionSpec(
        kind="derive.temporal_segments",
        params={"value": "stable"},
        idempotency_key="action-job-pre-enqueue-resume@sha256:stable",
    )
    try:
        reservation = reserve_queued_action_job_receipt(
            project,
            action,
            project_id=project_id,
        )
        assert isinstance(reservation, dict)
        envelope = ActionJobEnvelope(
            action_kind=action.kind,
            action_id=reservation["action_id"],
            receipt_id=reservation["receipt_id"],
            params_hash=reservation["params_hash"],
            idempotency_key=action.idempotency_key or "",
            project_id=project_id,
            action=action.model_dump(mode="json", exclude_none=True),
        )

        with pytest.raises(RuntimeError, match="pre-enqueue failure"):
            launch_queued_action_job(
                envelope=envelope,
                queue=FailingQueue(),
                job_kind=ACTION_RUN_KIND,
                project=project,
            )

        resumed = reserve_queued_action_job_receipt(
            project,
            action,
            project_id=project_id,
        )
        assert isinstance(resumed, dict)
        assert resumed == reservation

        result = launch_queued_action_job(
            envelope=envelope,
            queue=queue,
            job_kind=ACTION_RUN_KIND,
            project=project,
        )
        assert result.status == "queued"
        assert result.job_id is not None
        job = queue.get(result.job_id)
        assert job is not None
        assert job.dedupe_key == reservation["receipt_id"]
        receipt = ReceiptStore(project).parsed_by_id(reservation["receipt_id"])
        assert receipt is not None
        replay = reserve_queued_action_job_receipt(
            project,
            action,
            project_id=project_id,
        )
        assert not isinstance(replay, dict)
        assert replay.job_id == result.job_id
    finally:
        queue.close()
        project.close()


def test_dynamic_plugin_prepared_receipt_does_not_enter_static_resume_path(
    tmp_path,
) -> None:
    from frisket.contracts.action import ActionResult, ActionSpec
    from frisket.engine.executor.action_jobs import (
        ActionJobEnvelope,
        launch_queued_action_job,
        reserve_queued_action_job_receipt,
    )
    from frisket.engine.executor.action_specs import declared_queued_action_job_kinds
    from frisket.engine.store import Project

    class RecordingQueue:
        payload: dict | None = None

        def enqueue(self, _kind, payload, **_kwargs):
            self.payload = payload
            return 17

    action = ActionSpec(
        kind="probe.dynamic.queued_action",
        params={"value": "stable"},
        idempotency_key="dynamic-plugin-prepared-replay@sha256:stable",
    )
    assert action.kind not in declared_queued_action_job_kinds()
    project = Project.create(tmp_path / "dynamic-plugin-prepared-replay.frisket")
    try:
        reservation = reserve_queued_action_job_receipt(
            project,
            action,
            project_id="project-dynamic-plugin-replay",
        )
        assert isinstance(reservation, dict)
        queue = RecordingQueue()
        launched = launch_queued_action_job(
            envelope=ActionJobEnvelope(
                action_kind=action.kind,
                action_id=reservation["action_id"],
                receipt_id=reservation["receipt_id"],
                params_hash=reservation["params_hash"],
                idempotency_key=action.idempotency_key or "",
                project_id="project-dynamic-plugin-replay",
                action=action.model_dump(mode="json", exclude_none=True),
            ),
            queue=queue,
            job_kind="action.run",
        )
        assert launched.job_id == 17
        assert queue.payload is not None
        assert "dedupe_key" not in queue.payload

        replay = reserve_queued_action_job_receipt(
            project,
            action,
            project_id="project-dynamic-plugin-replay",
        )
        assert isinstance(replay, ActionResult)
        assert replay.status == "queued"
        assert replay.job_id is None
        assert replay.receipt_id == reservation["receipt_id"]
    finally:
        project.close()


def test_action_job_replay_repairs_post_enqueue_evidence_without_duplicate(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from frisket.contracts.action import ActionSpec
    from frisket.engine.executor import action_jobs
    from frisket.engine.executor.action_jobs import (
        ActionJobEnvelope,
        launch_queued_action_job,
        reserve_queued_action_job_receipt,
    )
    from frisket.engine.jobs.queue import ACTION_RUN_KIND, open_queue
    from frisket.engine.store import Project
    from frisket.engine.store.receipts import ReceiptStore

    project_id = "project-post-enqueue-resume"
    project = Project.create(tmp_path / f"{project_id}.frisket")
    queue = open_queue(workspace=tmp_path)
    action = ActionSpec(
        kind="derive.temporal_segments",
        params={"value": "stable"},
        idempotency_key="action-job-post-enqueue-resume@sha256:stable",
    )
    try:
        reservation = reserve_queued_action_job_receipt(
            project,
            action,
            project_id=project_id,
        )
        assert isinstance(reservation, dict)
        envelope = ActionJobEnvelope(
            action_kind=action.kind,
            action_id=reservation["action_id"],
            receipt_id=reservation["receipt_id"],
            params_hash=reservation["params_hash"],
            idempotency_key=action.idempotency_key or "",
            project_id=project_id,
            action=action.model_dump(mode="json", exclude_none=True),
        )
        original_mark = action_jobs.mark_action_job_enqueued

        def fail_mark(*_args, **_kwargs):
            raise RuntimeError("injected post-enqueue failure")

        monkeypatch.setattr(action_jobs, "mark_action_job_enqueued", fail_mark)
        with pytest.raises(RuntimeError, match="post-enqueue failure"):
            launch_queued_action_job(
                envelope=envelope,
                queue=queue,
                job_kind=ACTION_RUN_KIND,
                project=project,
            )
        [published] = queue.list_project_jobs(project_id, kind=ACTION_RUN_KIND)
        claimed = queue.claim("post-enqueue-publication-race")
        assert claimed is not None
        assert claimed.id == published.id
        assert (
            action_jobs._mark_action_job_receipt_running(
                project,
                project_id=project_id,
                receipt_id=reservation["receipt_id"],
                job_id=published.id,
            )
            is None
        )

        resumed = reserve_queued_action_job_receipt(
            project,
            action,
            project_id=project_id,
        )
        assert isinstance(resumed, dict)
        assert resumed == reservation

        monkeypatch.setattr(action_jobs, "mark_action_job_enqueued", original_mark)
        result = launch_queued_action_job(
            envelope=envelope,
            queue=queue,
            job_kind=ACTION_RUN_KIND,
            project=project,
        )
        assert result.job_id == published.id
        jobs = queue.list_project_jobs(project_id, kind=ACTION_RUN_KIND)
        assert [job.id for job in jobs] == [published.id]
        assert jobs[0].status == "running"
        receipt = ReceiptStore(project).parsed_by_id(reservation["receipt_id"])
        assert receipt is not None
        assert receipt.status == "running"
        assert action_jobs._action_job_receipt_job_id(receipt) == published.id
    finally:
        queue.close()
        project.close()


def _receipt_status(project_path, receipt_id):
    from frisket.engine.store import Project
    from frisket.engine.store.receipts import ReceiptStore

    project = Project(project_path)
    try:
        stored = ReceiptStore(project).find_by_id(receipt_id)
        return stored.parsed() if stored is not None else None
    finally:
        project.close()


def test_exhausted_lease_reconciliation_matches_only_the_canonical_error(
    tmp_path,
) -> None:
    from frisket.engine.executor.action_jobs import (
        launch_queued_action_job,
        reconcile_exhausted_action_run_jobs,
    )
    from frisket.engine.jobs.queue import ACTION_RUN_KIND, jobs_table, open_queue

    error_forms = (
        (
            "worker_lease_expired: worker lease expired (attempts exhausted)",
            True,
        ),
        ("other_failure: worker lease expired (attempts exhausted)", False),
    )
    for index, (source_error, should_reconcile) in enumerate(error_forms):
        workspace_root = tmp_path / f"lease-case-{index}"
        workspace_root.mkdir()
        project_id = f"proj-actionjob-lease-{index}"
        project_path = workspace_root / f"{project_id}.frisket"
        kind = "test.action_job_lease_compat"
        receipt_id = f"receipt_action_job_lease_{index}"
        _reserve_action_job_receipt(
            project_path,
            project_id,
            kind=kind,
            receipt_id=receipt_id,
        )
        queue = open_queue(workspace=workspace_root)
        try:
            queued = launch_queued_action_job(
                envelope=_envelope(project_id, kind=kind, receipt_id=receipt_id),
                queue=queue,
                job_kind=ACTION_RUN_KIND,
            )
            with queue.engine.begin() as cx:
                cx.execute(
                    jobs_table.update()
                    .where(jobs_table.c.id == queued.job_id)
                    .values(status="failed", error=source_error)
                )

            reconciled = reconcile_exhausted_action_run_jobs(
                queue,
                workspace_root=workspace_root,
            )
            assert reconciled == int(should_reconcile)
            assert (
                reconcile_exhausted_action_run_jobs(
                    queue,
                    workspace_root=workspace_root,
                )
                == 0
            )
            stored = _receipt_status(project_path, receipt_id)
            assert stored is not None
            if should_reconcile:
                assert stored.status == "failed"
                assert stored.errors
                assert stored.errors[0].code == "action_job_lease_exhausted"
            else:
                assert stored.status == "running"
                assert not stored.errors
        finally:
            queue.close()


def _insert_action_job_receipt(project_path, project_id, *, kind, receipt_id, status):
    from frisket.contracts.action import Receipt
    from frisket.engine.store import Project
    from frisket.engine.store.receipts import ReceiptStore

    project = Project.create(project_path, name="ActionJob")
    try:
        ReceiptStore(project).insert(
            Receipt(
                receipt_id=receipt_id,
                project_id=project_id,
                action_id="act_x",
                action_kind=kind,
                idempotency_key=f"{kind}@sha256:{receipt_id}",
                params_hash="sha256:x",
                status=status,
            )
        )
    finally:
        project.close()


@pytest.mark.parametrize("executor_status", ["completed", "running"])
def test_action_run_job_launches_runs_and_terminalizes(
    tmp_path, executor_status
) -> None:
    from frisket.contracts.action import ActionResult
    from frisket.engine.executor.action_jobs import launch_queued_action_job
    from frisket.engine.jobs.queue import ACTION_RUN_KIND, open_queue
    from frisket.engine.jobs.runs import register_action_run_handler
    from frisket.engine.jobs.worker import HandlerRegistry, Worker

    project_id = "proj-actionjob"
    project_path = tmp_path / f"{project_id}.frisket"
    kind = "test.action_job"
    receipt_id = "receipt_action_job_ok"
    _reserve_action_job_receipt(
        project_path, project_id, kind=kind, receipt_id=receipt_id
    )

    seen: dict = {}

    def _executor(project, env):
        seen["kind"] = env.action_kind
        # The worker owns receipt terminalization, not inference that work ran.
        return ActionResult(
            action={
                "kind": env.action_kind,
                "action_id": env.action_id,
            },
            status=executor_status,
            project_id=env.project_id,
            receipt_id=env.receipt_id,
        )

    queue = open_queue(workspace=tmp_path)
    launched = launch_queued_action_job(
        envelope=_envelope(project_id, kind=kind, receipt_id=receipt_id),
        queue=queue,
        job_kind=ACTION_RUN_KIND,
    )
    assert launched.status == "queued"
    assert launched.run_id is None
    assert launched.receipt_id == receipt_id

    registry = HandlerRegistry()
    registry.register_action_executor(kind, _executor)
    register_action_run_handler(registry, workspace_root=tmp_path)
    assert Worker(queue, registry, worker_id="aj-test").run_once() is True
    assert seen["kind"] == kind

    stored = _receipt_status(project_path, receipt_id)
    assert stored is not None
    assert stored.status == (
        "completed" if executor_status == "completed" else "failed"
    )


def test_action_run_stale_writer_refusal_leaves_shared_receipt_untouched(
    tmp_path,
) -> None:
    from frisket.engine.executor.action_jobs import run_action_run_job
    from frisket.engine.store import Project
    from frisket.engine.store.receipts import ReceiptStore
    from frisket.execution.attempt import StaleAttemptWriter

    project_id = "proj-actionjob-stale-writer"
    project_path = tmp_path / f"{project_id}.frisket"
    kind = "test.action_job_stale_writer"
    receipt_id = "receipt_action_job_stale_writer"
    _reserve_action_job_receipt(
        project_path,
        project_id,
        kind=kind,
        receipt_id=receipt_id,
    )
    envelope = _envelope(project_id, kind=kind, receipt_id=receipt_id)

    def raise_stale_writer(_project, _envelope):
        raise StaleAttemptWriter("attempt A lost its effect-site fence")

    project = Project(project_path)
    try:
        result = run_action_run_job(
            project,
            {
                "action_job": envelope.to_json(),
                "job_id": 17,
            },
            executor_lookup=lambda requested: (
                raise_stale_writer if requested == kind else None
            ),
        )

        assert result.status == "failed"
        assert [error.code for error in result.errors] == ["stale_attempt_writer"]
        stored = ReceiptStore(project).find_by_id(receipt_id)
        assert stored is not None
        receipt = stored.parsed()
        assert receipt.status == "running"
        assert receipt.run_id is None
        assert receipt.errors == []
    finally:
        project.close()


def test_action_run_renewal_failure_is_rethrown_for_queue_retry(
    tmp_path,
) -> None:
    from frisket.engine.executor.action_jobs import run_action_run_job
    from frisket.engine.store import Project
    from frisket.engine.store.output_claims import ClaimLeaseRenewalFailed
    from frisket.engine.store.receipts import ReceiptStore

    project_id = "proj-actionjob-renewal"
    project_path = tmp_path / f"{project_id}.frisket"
    kind = "test.action_job_renewal"
    receipt_id = "receipt_action_job_renewal"
    _reserve_action_job_receipt(
        project_path,
        project_id,
        kind=kind,
        receipt_id=receipt_id,
    )
    envelope = _envelope(project_id, kind=kind, receipt_id=receipt_id)

    def raise_renewal_failure(_project, _envelope):
        raise ClaimLeaseRenewalFailed("injected claim renewal outage")

    project = Project(project_path)
    try:
        with pytest.raises(
            ClaimLeaseRenewalFailed,
            match="claim renewal outage",
        ):
            run_action_run_job(
                project,
                {
                    "action_job": envelope.to_json(),
                    "job_id": 18,
                },
                executor_lookup=lambda requested: (
                    raise_renewal_failure if requested == kind else None
                ),
            )

        stored = ReceiptStore(project).find_by_id(receipt_id)
        assert stored is not None
        receipt = stored.parsed()
        assert receipt.status == "running"
        assert receipt.run_id is None
        assert receipt.errors == []
    finally:
        project.close()


def test_action_run_job_missing_executor_terminalizes_failed(tmp_path) -> None:
    from frisket.engine.executor.action_jobs import launch_queued_action_job
    from frisket.engine.jobs.queue import ACTION_RUN_KIND, open_queue
    from frisket.engine.jobs.runs import register_action_run_handler
    from frisket.engine.jobs.worker import HandlerRegistry, Worker

    project_id = "proj-actionjob-missing"
    project_path = tmp_path / f"{project_id}.frisket"
    kind = "test.action_job_missing"
    receipt_id = "receipt_action_job_missing"
    _reserve_action_job_receipt(
        project_path, project_id, kind=kind, receipt_id=receipt_id
    )

    queue = open_queue(workspace=tmp_path)
    launch_queued_action_job(
        envelope=_envelope(project_id, kind=kind, receipt_id=receipt_id),
        queue=queue,
        job_kind=ACTION_RUN_KIND,
    )
    registry = HandlerRegistry()
    register_action_run_handler(registry, workspace_root=tmp_path)
    # no executor registered for `kind`: the worker must terminalize the receipt
    # failed (missing handler), never leave it running.
    assert Worker(queue, registry, worker_id="aj-missing").run_once() is True

    stored = _receipt_status(project_path, receipt_id)
    assert stored is not None and stored.status == "failed"
    assert stored.errors and stored.errors[0].code == "action_handler_missing"


def test_action_run_job_retryable_failed_result_requeues(tmp_path) -> None:
    from frisket.contracts.action import ActionError, ActionResult, ActionSpec
    from frisket.engine.executor.action_jobs import (
        ActionJobEnvelope,
        launch_queued_action_job,
        reserve_queued_action_job_receipt,
    )
    from frisket.engine.executor.action_support import _params_hash
    from frisket.engine.jobs.queue import ACTION_RUN_KIND, open_queue
    from frisket.engine.jobs.runs import register_action_run_handler
    from frisket.engine.jobs.worker import HandlerRegistry, Worker
    from frisket.engine.store import Project

    project_id = "proj-actionjob-retryable"
    project_path = tmp_path / f"{project_id}.frisket"
    kind = "test.action_job_retryable"
    action = ActionSpec.model_validate(
        {
            "schema_version": "frisket.action.v2",
            "kind": kind,
            "params": {},
            "idempotency_key": f"{kind}@sha256:retry",
        }
    )
    project = Project.create(project_path, name="ActionJobRetryable")
    queue = open_queue(workspace=tmp_path)
    try:
        params_hash = _params_hash(action)
        reservation = reserve_queued_action_job_receipt(
            project,
            action,
            project_id=project_id,
            params_hash=params_hash,
        )
        assert isinstance(reservation, dict)
        envelope = ActionJobEnvelope(
            action_kind=kind,
            action_id=reservation["action_id"],
            receipt_id=reservation["receipt_id"],
            params_hash=reservation["params_hash"],
            idempotency_key=action.idempotency_key or "",
            project_id=project_id,
            action=action.model_dump(mode="json", exclude_none=True),
        )
        launch_queued_action_job(
            envelope=envelope,
            queue=queue,
            job_kind=ACTION_RUN_KIND,
            max_attempts=2,
            project=project,
        )

        def _executor(_project, env):
            return ActionResult(
                action={
                    "kind": env.action_kind,
                    "action_id": env.action_id,
                },
                status="failed",
                project_id=env.project_id,
                receipt_id=env.receipt_id,
                errors=[
                    ActionError(
                        code="embedding_index_busy",
                        message="refresh claim is held",
                        action_kind=env.action_kind,
                        details={"retryable": True},
                    )
                ],
            )

        registry = HandlerRegistry()
        registry.register_action_executor(kind, _executor)
        register_action_run_handler(registry, workspace_root=tmp_path)
        assert (
            Worker(
                queue, registry, worker_id="aj-retry", retry_base_seconds=0
            ).run_once()
            is True
        )
        [job] = queue.list_project_jobs(project_id, kind=ACTION_RUN_KIND)
        assert job.status == "queued"
        assert job.attempts == 1
        assert "embedding_index_busy" in (job.error or "")

        stored = _receipt_status(project_path, reservation["receipt_id"])
        assert stored is not None and stored.status == "queued"
    finally:
        queue.close()
        project.close()


def test_retryable_action_run_exhaustion_terminalizes_receipt(tmp_path) -> None:
    from frisket.contracts.action import ActionError, ActionResult, ActionSpec
    from frisket.engine.executor.action_jobs import (
        ActionJobEnvelope,
        launch_queued_action_job,
        reserve_queued_action_job_receipt,
    )
    from frisket.engine.executor.queue_terminalization import (
        register_queue_terminalization,
    )
    from frisket.engine.executor.action_support import _params_hash
    from frisket.engine.jobs.queue import ACTION_RUN_KIND, open_queue
    from frisket.engine.jobs.runs import register_action_run_handler
    from frisket.engine.jobs.worker import HandlerRegistry, Worker
    from frisket.engine.store import Project

    project_id = "proj-actionjob-retry-exhausted"
    project_path = tmp_path / f"{project_id}.frisket"
    kind = "test.action_job_retry_exhausted"
    action = ActionSpec.model_validate(
        {
            "schema_version": "frisket.action.v2",
            "kind": kind,
            "params": {},
            "idempotency_key": f"{kind}@sha256:retry",
        }
    )
    project = Project.create(project_path, name="ActionJobRetryExhausted")
    queue = open_queue(workspace=tmp_path)
    try:
        params_hash = _params_hash(action)
        reservation = reserve_queued_action_job_receipt(
            project,
            action,
            project_id=project_id,
            params_hash=params_hash,
        )
        assert isinstance(reservation, dict)
        envelope = ActionJobEnvelope(
            action_kind=kind,
            action_id=reservation["action_id"],
            receipt_id=reservation["receipt_id"],
            params_hash=reservation["params_hash"],
            idempotency_key=action.idempotency_key or "",
            project_id=project_id,
            action=action.model_dump(mode="json", exclude_none=True),
        )
        launch_queued_action_job(
            envelope=envelope,
            queue=queue,
            job_kind=ACTION_RUN_KIND,
            max_attempts=2,
            project=project,
        )

        def _executor(_project, env):
            return ActionResult(
                action={
                    "kind": env.action_kind,
                    "action_id": env.action_id,
                },
                status="failed",
                project_id=env.project_id,
                receipt_id=env.receipt_id,
                errors=[
                    ActionError(
                        code="embedding_index_busy",
                        message="refresh claim is held",
                        action_kind=env.action_kind,
                        details={"retryable": True},
                    )
                ],
            )

        registry = HandlerRegistry()
        registry.register_action_executor(kind, _executor)
        register_action_run_handler(registry, workspace_root=tmp_path)
        # Exhaustion terminalizes the receipt via the queue's registered
        # product hook; the queue stays independent of receipt storage.
        register_queue_terminalization(queue, workspace_root=tmp_path)
        worker = Worker(
            queue, registry, worker_id="aj-retry-exhausted", retry_base_seconds=0
        )
        assert worker.run_once() is True
        assert worker.run_once() is True

        [job] = queue.list_project_jobs(project_id, kind=ACTION_RUN_KIND)
        assert job.status == "failed"
        stored = _receipt_status(project_path, reservation["receipt_id"])
        assert stored is not None
        assert stored.status == "failed"
        assert stored.errors
        assert stored.errors[0].code in {
            "action_job_retry_exhausted",
            "action_job_lease_exhausted",
        }
    finally:
        queue.close()
        project.close()


def test_action_job_cancelled_receipt_is_finished_insert_compatible(tmp_path) -> None:
    from frisket.engine.executor.action_jobs import action_job_cancelled_result
    from frisket.engine.store import Project
    from frisket.engine.store.receipts import ReceiptStore

    project_id = "proj-actionjob-cancel-finished"
    project_path = tmp_path / f"{project_id}.frisket"
    kind = "test.action_job_cancel_finished"
    receipt_id = "receipt_action_job_cancel_finished"
    _reserve_action_job_receipt(
        project_path, project_id, kind=kind, receipt_id=receipt_id
    )

    project = Project(project_path)
    try:
        result = action_job_cancelled_result(
            project,
            project_id=project_id,
            receipt_id=receipt_id,
            action_kind=kind,
        )
        assert result.status == "cancelled"
        stored = ReceiptStore(project).find_by_id(receipt_id)
        assert stored is not None
        cancelled = stored.parsed()
        replay = cancelled.model_copy(
            update={
                "receipt_id": f"{receipt_id}_finished_insert",
                "idempotency_key": f"{kind}:finished-insert",
            }
        )
        ReceiptStore(project).insert_finished(replay)
        assert ReceiptStore(project).status_by_id(replay.receipt_id) == "cancelled"
    finally:
        project.close()


def test_action_job_terminal_receipt_cannot_be_rewritten_by_late_completion(
    tmp_path,
) -> None:
    from frisket.engine.executor.action_jobs import action_job_success_result
    from frisket.engine.store import Project

    project_id = "proj-actionjob-monotonic"
    project_path = tmp_path / f"{project_id}.frisket"
    kind = "test.action_job_monotonic"
    receipt_id = "receipt_action_job_cancelled"
    _insert_action_job_receipt(
        project_path,
        project_id,
        kind=kind,
        receipt_id=receipt_id,
        status="cancelled",
    )

    project = Project(project_path)
    try:
        result = action_job_success_result(
            project,
            project_id=project_id,
            receipt_id=receipt_id,
            action_kind=kind,
            job_id=99,
        )
    finally:
        project.close()

    assert result.status == "cancelled"
    stored = _receipt_status(project_path, receipt_id)
    assert stored is not None
    assert stored.status == "cancelled"


def test_action_job_terminalization_fails_closed_for_missing_receipt(tmp_path) -> None:
    from frisket.engine.executor.action_jobs import (
        ActionJobTerminalizationError,
        action_job_success_result,
    )
    from frisket.engine.store import Project

    project = Project.create(tmp_path / "action-job-missing-receipt.frisket")
    try:
        with pytest.raises(ActionJobTerminalizationError, match="not found"):
            action_job_success_result(
                project,
                project_id="project-action-job-missing-receipt",
                receipt_id="receipt_action_job_missing_terminalization",
                action_kind="test.action_job_missing_terminalization",
            )
    finally:
        project.close()


def test_action_job_terminalization_cas_loss_is_atomic(tmp_path, monkeypatch) -> None:
    from frisket.contracts.action import ActionError
    from frisket.engine.executor.action_jobs import (
        ActionJobTerminalizationError,
        action_job_failure_result,
    )
    from frisket.engine.store import Project
    from frisket.engine.store.output_claims import OutputColumnClaimStore
    from frisket.engine.store.receipts import ReceiptStore

    project_id = "project-action-job-cas-loss"
    receipt_id = "receipt_action_job_cas_loss"
    action_kind = "test.action_job_cas_loss"
    project_path = tmp_path / "action-job-cas-loss.frisket"
    _insert_action_job_receipt(
        project_path,
        project_id,
        kind=action_kind,
        receipt_id=receipt_id,
        status="running",
    )
    project = Project(project_path)
    try:
        sheet_id = project.add_sheet("Claims")
        claims, conflict = OutputColumnClaimStore(project).acquire(
            sheet_id=sheet_id,
            output_names=["result"],
            action_kind=action_kind,
            receipt_id=receipt_id,
        )
        assert len(claims) == 1
        assert conflict is None

        monkeypatch.setattr(
            ReceiptStore,
            "update_if_status_in",
            lambda *_args, **_kwargs: False,
        )
        with pytest.raises(
            ActionJobTerminalizationError, match="failed to terminalize"
        ):
            action_job_failure_result(
                project,
                project_id=project_id,
                receipt_id=receipt_id,
                action_kind=action_kind,
                error=ActionError(
                    code="fixture_failure",
                    message="the injected terminal CAS lost",
                    action_kind=action_kind,
                ),
            )

        stored = ReceiptStore(project).parsed_by_id(receipt_id)
        claim = project.db.execute(
            "SELECT status FROM output_column_claims WHERE receipt_id=?",
            (receipt_id,),
        ).fetchone()
        assert stored is not None and stored.status == "running"
        assert claim is not None and claim["status"] == "active"
    finally:
        project.close()


@pytest.mark.parametrize("terminal_status", ["completed", "partial"])
def test_action_job_success_releases_claim_for_every_success_status(
    tmp_path,
    terminal_status,
) -> None:
    from frisket.engine.executor.action_jobs import action_job_success_result
    from frisket.engine.store import Project
    from frisket.engine.store.output_claims import OutputColumnClaimStore

    project_id = f"project-action-job-claim-{terminal_status}"
    receipt_id = f"receipt_action_job_claim_{terminal_status}"
    action_kind = "test.action_job_claim_release"
    project_path = tmp_path / f"action-job-claim-{terminal_status}.frisket"
    _insert_action_job_receipt(
        project_path,
        project_id,
        kind=action_kind,
        receipt_id=receipt_id,
        status="running",
    )
    project = Project(project_path)
    try:
        sheet_id = project.add_sheet("Claims")
        claims, conflict = OutputColumnClaimStore(project).acquire(
            sheet_id=sheet_id,
            output_names=["result"],
            action_kind=action_kind,
            receipt_id=receipt_id,
        )
        assert len(claims) == 1
        assert conflict is None

        result = action_job_success_result(
            project,
            project_id=project_id,
            receipt_id=receipt_id,
            action_kind=action_kind,
            status=terminal_status,
        )

        claim = project.db.execute(
            "SELECT status FROM output_column_claims WHERE receipt_id=?",
            (receipt_id,),
        ).fetchone()
        assert result.status == terminal_status
        assert claim is not None and claim["status"] == "released"
    finally:
        project.close()


def test_action_job_failure_preserves_error_facts_by_value(tmp_path) -> None:
    from frisket.contracts.action import ActionError
    from frisket.engine.executor.action_jobs import action_job_failure_result
    from frisket.engine.store import Project
    from frisket.engine.store.receipts import ReceiptStore

    project_id = "project-action-job-error-facts"
    receipt_id = "receipt_action_job_error_facts"
    action_kind = "test.action_job_error_facts"
    project_path = tmp_path / "action-job-error-facts.frisket"
    _insert_action_job_receipt(
        project_path,
        project_id,
        kind=action_kind,
        receipt_id=receipt_id,
        status="running",
    )
    error = ActionError(
        code="provider_declined",
        message="provider rejected the paid request",
        action_kind=action_kind,
        details={"invoice_id": "inv_fixture", "cost_actual": 4.75},
    )
    project = Project(project_path)
    try:
        result = action_job_failure_result(
            project,
            project_id=project_id,
            receipt_id=receipt_id,
            action_kind=action_kind,
            error=error,
        )
        stored = ReceiptStore(project).parsed_by_id(receipt_id)

        assert result.errors == [error]
        assert stored is not None
        assert stored.errors == [error]
    finally:
        project.close()


def test_queued_receipt_replay_correlates_job_without_authoring_it() -> None:
    from frisket.contracts.action import Receipt, ReceiptEvidence
    from frisket.engine.executor import action_jobs

    receipt = Receipt(
        receipt_id="receipt_action_job_queued_projection",
        project_id="project-action-job-projection",
        action_id="act_action_job_projection",
        action_kind="test.action_job_projection",
        op_ids=[17],
        status="queued",
        evidence=[
            ReceiptEvidence(
                ref={
                    "kind": action_jobs.ACTION_JOB_ENQUEUED_EVIDENCE_KIND,
                    "queue_kind": "action.run",
                    "job_id": 91,
                }
            )
        ],
    )

    job_id = action_jobs._action_job_receipt_job_id(receipt)  # noqa: SLF001
    result = action_jobs._result_from_existing_action_job_receipt(  # noqa: SLF001
        receipt,
        job_id=job_id,
    )

    assert result.status == "queued"
    assert result.job_id == 91
    assert result.run_id is None
    assert result.op_ids == [17]
    assert "job_id" not in receipt.model_dump(mode="json")


def test_action_job_terminal_projection_keeps_outputs_warnings_and_coordinates(
    tmp_path,
) -> None:
    from frisket.contracts.action import Receipt, ReceiptIO
    from frisket.engine.executor.action_jobs import (
        _mark_action_job_receipt_running,
        action_job_success_result,
    )
    from frisket.engine.store import Project
    from frisket.engine.store.receipts import ReceiptStore

    project_id = "project-action-job-terminal-projection"
    receipt_id = "receipt_action_job_terminal_projection"
    action_kind = "test.action_job_terminal_projection"
    project = Project.create(tmp_path / "action-job-terminal-projection.frisket")
    try:
        ReceiptStore(project).insert_running(
            Receipt(
                receipt_id=receipt_id,
                project_id=project_id,
                action_id="act_action_job_terminal_projection",
                action_kind=action_kind,
                op_ids=[23],
                status="running",
                outputs=[
                    ReceiptIO(
                        name="rows",
                        ref={
                            "kind": "source_rows",
                            "sheet_id": 4,
                            "row_ids": [10, 11],
                        },
                    )
                ],
                warnings=["terminal warning"],
            )
        )

        result = action_job_success_result(
            project,
            project_id=project_id,
            receipt_id=receipt_id,
            action_kind=action_kind,
            status="partial",
            job_id=92,
        )
        replay = action_job_success_result(
            project,
            project_id=project_id,
            receipt_id=receipt_id,
            action_kind=action_kind,
            job_id=92,
        )
        worker_replay = _mark_action_job_receipt_running(
            project,
            project_id=project_id,
            receipt_id=receipt_id,
            job_id=92,
        )
    finally:
        project.close()

    for projected in (result, replay, worker_replay):
        assert projected is not None
        assert projected.status == "partial"
        assert projected.project_id == project_id
        assert projected.run_id is None
        assert projected.job_id == 92
        assert projected.op_ids == [23]
        assert projected.warnings == ["terminal warning"]
        assert [(output.kind, output.row_ids) for output in projected.outputs] == [
            ("rows", [10, 11])
        ]
