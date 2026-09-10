"""Team worker capability gating: a team deployment's worker runs via ``frisket worker
--database-url ...`` (the SAME ``_run_worker(hosted=False)`` branch a plain
local checkout uses, distinguished only by whether a run-queue database URL
was given) -- never ``frisket hosted-worker`` (that CLI branch is an
external managed composition and must never see ``model.pull`` at all, pinned
separately by ``test_cli_worker_model_pull_registration.py``).

Artifact pulls are a worker capability regardless of whether an organization
has enabled local-server provisioning. Endpoint-local admission is enforced
per endpoint; HF/spaCy/translation artifact jobs must still have a handler.
"""

from __future__ import annotations

import pytest

import frisket.engine.jobs.model_pull as model_pull_module
from frisket.cli import _register_team_model_pull_handler, _run_worker


def _sqlite_url(tmp_path) -> str:
    return f"sqlite:///{tmp_path / 'team-queue.db'}"


def test_team_worker_without_local_pull_flag_still_registers_artifact_handler(
    tmp_path, monkeypatch
) -> None:
    calls: list[dict] = []
    real = model_pull_module.register_model_pull_handler

    def spy(registry, *, workspace_root, queue, **kwargs):
        calls.append({"workspace_root": workspace_root, "queue": queue})
        return real(registry, workspace_root=workspace_root, queue=queue, **kwargs)

    monkeypatch.setattr(model_pull_module, "register_model_pull_handler", spy)
    monkeypatch.delenv("FRISKET_ENABLE_MODEL_PULL", raising=False)

    rc = _run_worker(["--drain", "--database-url", _sqlite_url(tmp_path)], hosted=False)

    assert rc == 0
    assert len(calls) == 1


def test_team_worker_with_flag_registers_model_pull(tmp_path, monkeypatch) -> None:
    calls: list[dict] = []
    real = model_pull_module.register_model_pull_handler

    def spy(registry, *, workspace_root, queue, **kwargs):
        calls.append({"workspace_root": workspace_root, "queue": queue})
        return real(registry, workspace_root=workspace_root, queue=queue, **kwargs)

    monkeypatch.setattr(model_pull_module, "register_model_pull_handler", spy)
    monkeypatch.setenv("FRISKET_ENABLE_MODEL_PULL", "1")

    rc = _run_worker(["--drain", "--database-url", _sqlite_url(tmp_path)], hosted=False)

    assert rc == 0
    assert len(calls) == 1


def test_team_worker_audits_model_pull_finished_after_handler_succeeds(
    tmp_path, monkeypatch
) -> None:
    """Only the REQUEST side of a team pull used to get an audit row
    (``model_pull_requested``, written by the route) -- nothing recorded the
    TERMINAL outcome once a worker actually claimed and ran the job. The
    team registration path wraps the handler so a ``model_pull_finished``
    audit row (status, model ref, correlation id) lands in the control DB
    after every attempt, whether the handler returns or raises."""
    import sqlalchemy as sa

    from frisket.engine.jobs import model_pull_store as store
    from frisket.engine.jobs.queue import MODEL_PULL_KIND, open_queue
    from frisket.team.schema import audit_log, metadata

    control_url = f"sqlite:///{tmp_path / 'control.db'}"
    control_engine = sa.create_engine(control_url, future=True)
    metadata.create_all(control_engine)

    queue_url = _sqlite_url(tmp_path)
    queue = open_queue(database_url=queue_url)
    row, _created = store.create_or_get_active(
        queue.engine, workspace_root=str(tmp_path), model_ref="smollm:135m"
    )
    job_id = queue.enqueue(
        MODEL_PULL_KIND, {"pull_id": row.id, "workspace_root": str(tmp_path)}
    )
    store.set_job_id(queue.engine, row.id, job_id=job_id)

    def fake_register(registry, *, workspace_root, queue, **kwargs):
        def handle(payload: dict, _context) -> dict:
            store.mark_running(
                queue.engine, payload["pull_id"], job_id=payload["job_id"]
            )
            store.mark_done(queue.engine, payload["pull_id"])
            return {"status": "done"}

        registry.register(MODEL_PULL_KIND, handle)

    monkeypatch.setattr(model_pull_module, "register_model_pull_handler", fake_register)
    monkeypatch.setenv("FRISKET_ENABLE_MODEL_PULL", "1")
    monkeypatch.setenv("FRISKET_DATABASE_URL", control_url)

    rc = _run_worker(["--drain", "--database-url", queue_url], hosted=False)
    assert rc == 0

    with control_engine.connect() as cx:
        rows = cx.execute(sa.select(audit_log.c.action, audit_log.c.detail)).all()
    finished = [r for r in rows if r.action == "model_pull_finished"]
    assert len(finished) == 1, rows
    assert str(row.id) in finished[0].detail
    assert "done" in finished[0].detail


def test_team_worker_audits_model_pull_finished_after_handler_raises(
    tmp_path, monkeypatch
) -> None:
    """The audit wrap must fire on the exception path too -- a handler that
    raises still leaves a durable ``model_pulls`` row in a terminal
    (``failed``) state, and that terminal status must reach the audit
    trail exactly like a success does."""
    import sqlalchemy as sa

    from frisket.engine.jobs import model_pull_store as store
    from frisket.engine.jobs.queue import MODEL_PULL_KIND, open_queue
    from frisket.team.schema import audit_log, metadata

    control_url = f"sqlite:///{tmp_path / 'control.db'}"
    control_engine = sa.create_engine(control_url, future=True)
    metadata.create_all(control_engine)

    queue_url = _sqlite_url(tmp_path)
    queue = open_queue(database_url=queue_url)
    row, _created = store.create_or_get_active(
        queue.engine, workspace_root=str(tmp_path), model_ref="smollm:135m"
    )
    job_id = queue.enqueue(
        MODEL_PULL_KIND,
        {"pull_id": row.id, "workspace_root": str(tmp_path)},
        max_attempts=1,
    )
    store.set_job_id(queue.engine, row.id, job_id=job_id)

    def fake_register(registry, *, workspace_root, queue, **kwargs):
        def handle(payload: dict, _context) -> dict:
            store.mark_running(
                queue.engine, payload["pull_id"], job_id=payload["job_id"]
            )
            store.mark_failed(
                queue.engine,
                payload["pull_id"],
                error_code="pull_worker_error",
                error_message="synthetic failure",
            )
            raise RuntimeError("synthetic failure")

        registry.register(MODEL_PULL_KIND, handle)

    monkeypatch.setattr(model_pull_module, "register_model_pull_handler", fake_register)
    monkeypatch.setenv("FRISKET_ENABLE_MODEL_PULL", "1")
    monkeypatch.setenv("FRISKET_DATABASE_URL", control_url)

    rc = _run_worker(["--drain", "--database-url", queue_url], hosted=False)
    assert rc == 0

    with control_engine.connect() as cx:
        rows = cx.execute(sa.select(audit_log.c.action, audit_log.c.detail)).all()
    finished = [r for r in rows if r.action == "model_pull_finished"]
    assert len(finished) == 1, rows
    assert str(row.id) in finished[0].detail
    assert "failed" in finished[0].detail


def test_team_worker_does_not_audit_model_pull_finished_after_a_non_final_retryable_attempt(
    tmp_path, monkeypatch
) -> None:
    """The ``model_pull_finished`` audit used to fire after EVERY attempt,
    including a non-final retryable one where the row stays ACTIVE
    (``record_attempt_error`` -- status untouched, attempts remain). Auditing
    that attempt as 'finished' is misleading (the pull isn't finished at all)
    and would duplicate on every retry. The wrap must write the audit row
    ONLY once the pull row is actually terminal (done/failed/cancelled)
    after the attempt."""
    import sqlalchemy as sa

    from frisket.engine.jobs import model_pull_store as store
    from frisket.engine.jobs.queue import MODEL_PULL_KIND, open_queue
    from frisket.team.schema import audit_log, metadata

    control_url = f"sqlite:///{tmp_path / 'control.db'}"
    control_engine = sa.create_engine(control_url, future=True)
    metadata.create_all(control_engine)

    queue_url = _sqlite_url(tmp_path)
    queue = open_queue(database_url=queue_url)
    row, _created = store.create_or_get_active(
        queue.engine, workspace_root=str(tmp_path), model_ref="smollm:135m"
    )
    # max_attempts=3: the single attempt below is NOT the job's final one, so
    # the queue requeues it with a retry delay instead of terminal-failing.
    job_id = queue.enqueue(
        MODEL_PULL_KIND,
        {"pull_id": row.id, "workspace_root": str(tmp_path)},
        max_attempts=3,
    )
    store.set_job_id(queue.engine, row.id, job_id=job_id)

    def fake_register(registry, *, workspace_root, queue, **kwargs):
        def handle(payload: dict, _context) -> dict:
            store.mark_running(
                queue.engine, payload["pull_id"], job_id=payload["job_id"]
            )
            # The row stays ACTIVE ('running') -- exactly what a non-final
            # retryable attempt does; it is NOT finalized to failed/done.
            store.record_attempt_error(
                queue.engine,
                payload["pull_id"],
                error_code="transient",
                error_message="synthetic transient failure",
            )
            raise RuntimeError("synthetic transient failure")

        registry.register(MODEL_PULL_KIND, handle)

    monkeypatch.setattr(model_pull_module, "register_model_pull_handler", fake_register)
    monkeypatch.setenv("FRISKET_ENABLE_MODEL_PULL", "1")
    monkeypatch.setenv("FRISKET_DATABASE_URL", control_url)

    rc = _run_worker(["--drain", "--database-url", queue_url], hosted=False)
    assert rc == 0

    row_after = store.get(queue.engine, row.id)
    assert row_after.status == "running", (
        "a non-final retryable attempt must leave the row ACTIVE -- this "
        "test's premise (and the audit gate it pins) only holds if the row "
        "genuinely is not terminal after the attempt"
    )

    with control_engine.connect() as cx:
        rows = cx.execute(sa.select(audit_log.c.action, audit_log.c.detail)).all()
    finished = [r for r in rows if r.action == "model_pull_finished"]
    assert finished == [], (
        "a non-final retryable attempt (row still active) must not write a "
        f"model_pull_finished audit row: {finished}"
    )


def test_team_model_pull_audit_wrapper_forwards_claimed_job_context(
    tmp_path, monkeypatch
) -> None:
    """The CLI audit wrapper must preserve the worker's claimed-row facts."""
    from frisket.engine.jobs import HandlerRegistry, SqliteJobQueue, Worker
    from frisket.engine.jobs.queue import MODEL_PULL_KIND

    queue = SqliteJobQueue(tmp_path / "queue.db")
    registry = HandlerRegistry()
    observed_orgs: list[object] = []

    def fake_register(registry, *, workspace_root, queue, **kwargs):
        def handle(_payload: dict, handler_context) -> dict:
            observed_orgs.append(handler_context.trusted_job_org_id)
            return {"status": "done"}

        registry.register(MODEL_PULL_KIND, handle)

    monkeypatch.setattr(model_pull_module, "register_model_pull_handler", fake_register)
    try:
        assert _register_team_model_pull_handler(
            registry,
            workspace_root=tmp_path,
            queue=queue,
            env={"FRISKET_ENABLE_MODEL_PULL": "1"},
        )
        job_id = queue.enqueue(MODEL_PULL_KIND, {"org_id": 7001})

        assert Worker(queue, registry, worker_id="cli-audit-context").run_once()
        assert queue.get(job_id).status == "done"
        assert observed_orgs == [7001]
    finally:
        queue.close()


def test_team_model_pull_refuses_duplicate_terminal_audit_installation(
    tmp_path,
) -> None:
    from frisket.engine.jobs import HandlerRegistry, SqliteJobQueue
    from frisket.engine.jobs.queue import MODEL_PULL_KIND

    queue = SqliteJobQueue(tmp_path / "queue.db")
    registry = HandlerRegistry()
    try:
        assert _register_team_model_pull_handler(
            registry,
            workspace_root=tmp_path,
            queue=queue,
            env={"FRISKET_ENABLE_MODEL_PULL": "1"},
        )
        installed = registry.get(MODEL_PULL_KIND)
        assert installed is not None

        with pytest.raises(ValueError) as duplicate:
            _register_team_model_pull_handler(
                registry,
                workspace_root=tmp_path,
                queue=queue,
                env={"FRISKET_ENABLE_MODEL_PULL": "1"},
            )

        assert MODEL_PULL_KIND in str(duplicate.value)
        assert registry.get(MODEL_PULL_KIND) is installed
    finally:
        queue.close()
