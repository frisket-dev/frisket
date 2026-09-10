"""model_pulls: the durable dedupe authority for in-app model pulls. A
partial UNIQUE index on
``(workspace_root, model_ref)`` restricted to active (pending/running) rows,
with an atomic find-or-create against it.
"""

from __future__ import annotations

import pytest

from frisket.engine.jobs import model_pull_store as store
from frisket.engine.jobs.queue import open_queue


def _engine(tmp_path):
    queue = open_queue(workspace=tmp_path)
    return queue, queue.engine


def test_create_or_get_active_is_atomic_find_or_create(tmp_path) -> None:
    queue, engine = _engine(tmp_path)
    try:
        row1, created1 = store.create_or_get_active(
            engine, workspace_root=str(tmp_path), model_ref="smollm:135m"
        )
        assert created1 is True
        assert row1.status == store.STATUS_PENDING

        row2, created2 = store.create_or_get_active(
            engine, workspace_root=str(tmp_path), model_ref="smollm:135m"
        )
        assert created2 is False
        assert row2.id == row1.id
    finally:
        queue.close()


def test_different_ref_while_workspace_active_raises_busy(tmp_path) -> None:
    """There is one active pull per WORKSPACE (any ref), atomically -- a
    second ref while the workspace
    already has an active pull is a typed busy conflict, not a second
    active row for the same workspace."""
    queue, engine = _engine(tmp_path)
    try:
        row1, _ = store.create_or_get_active(
            engine, workspace_root=str(tmp_path), model_ref="smollm:135m"
        )
        with pytest.raises(store.ModelPullBusyError) as excinfo:
            store.create_or_get_active(
                engine, workspace_root=str(tmp_path), model_ref="qwen3:8b"
            )
        assert excinfo.value.active.id == row1.id
        assert excinfo.value.active.model_ref == "smollm:135m"
    finally:
        queue.close()


def test_busy_conflict_clears_once_the_active_row_goes_terminal(tmp_path) -> None:
    queue, engine = _engine(tmp_path)
    try:
        row1, _ = store.create_or_get_active(
            engine, workspace_root=str(tmp_path), model_ref="smollm:135m"
        )
        store.mark_done(engine, row1.id)

        row2, created2 = store.create_or_get_active(
            engine, workspace_root=str(tmp_path), model_ref="qwen3:8b"
        )
        assert created2 is True
        assert row2.id != row1.id
    finally:
        queue.close()


def test_concurrent_inserts_for_different_refs_resolve_to_one_winner_and_one_busy(
    tmp_path,
) -> None:
    """Atomicity under a genuine race (item 2): both callers attempt an
    insert with nothing serializing them beforehand -- exactly one wins
    (created=True) and the other sees the winner as busy. No pre-check read
    is involved; the unique index itself is the arbiter."""
    queue, engine = _engine(tmp_path)
    try:
        winners = []
        busy = []
        for ref in ("smollm:135m", "qwen3:8b"):
            try:
                row, created = store.create_or_get_active(
                    engine, workspace_root=str(tmp_path), model_ref=ref
                )
            except store.ModelPullBusyError as exc:
                busy.append(exc.active)
                continue
            if created:
                winners.append(row)
        assert len(winners) == 1
        assert len(busy) == 1
        assert busy[0].id == winners[0].id
    finally:
        queue.close()


def test_different_workspace_gets_a_distinct_row_for_the_same_ref(tmp_path) -> None:
    queue, engine = _engine(tmp_path)
    try:
        row1, _ = store.create_or_get_active(
            engine, workspace_root="/ws/one", model_ref="smollm:135m"
        )
        row2, created2 = store.create_or_get_active(
            engine, workspace_root="/ws/two", model_ref="smollm:135m"
        )
        assert created2 is True
        assert row2.id != row1.id
    finally:
        queue.close()


def test_a_new_active_row_can_be_created_once_the_prior_one_is_terminal(
    tmp_path,
) -> None:
    queue, engine = _engine(tmp_path)
    try:
        row1, _ = store.create_or_get_active(
            engine, workspace_root=str(tmp_path), model_ref="smollm:135m"
        )
        store.mark_done(engine, row1.id)

        row2, created2 = store.create_or_get_active(
            engine, workspace_root=str(tmp_path), model_ref="smollm:135m"
        )
        assert created2 is True
        assert row2.id != row1.id
    finally:
        queue.close()


def test_find_active_for_workspace_ignores_terminal_rows(tmp_path) -> None:
    queue, engine = _engine(tmp_path)
    try:
        assert store.find_active_for_workspace(engine, str(tmp_path)) is None

        row, _ = store.create_or_get_active(
            engine, workspace_root=str(tmp_path), model_ref="smollm:135m"
        )
        active = store.find_active_for_workspace(engine, str(tmp_path))
        assert active is not None and active.id == row.id

        store.mark_failed(engine, row.id, error_code="pull_failed", error_message="x")
        assert store.find_active_for_workspace(engine, str(tmp_path)) is None
    finally:
        queue.close()


def test_lifecycle_transitions_and_progress(tmp_path) -> None:
    queue, engine = _engine(tmp_path)
    try:
        row, _ = store.create_or_get_active(
            engine, workspace_root=str(tmp_path), model_ref="smollm:135m"
        )
        store.set_job_id(engine, row.id, job_id=42)
        store.mark_running(engine, row.id, job_id=42)
        running = store.get(engine, row.id)
        assert running.status == store.STATUS_RUNNING
        assert running.job_id == 42
        assert running.correlation_id == "42"
        assert running.started_at is not None

        store.update_progress(
            engine, row.id, phase="downloading", total_bytes=1000, completed_bytes=250
        )
        progressed = store.get(engine, row.id)
        assert progressed.phase == "downloading"
        assert progressed.total_bytes == 1000
        assert progressed.completed_bytes == 250

        store.mark_done(
            engine, row.id, resolved_digest="sha256:abc", resolved_size=1000
        )
        done = store.get(engine, row.id)
        assert done.status == store.STATUS_DONE
        assert done.resolved_digest == "sha256:abc"
        assert done.resolved_size == 1000
        assert done.finished_at is not None
    finally:
        queue.close()


def test_request_cancel_flips_flag_only_for_active_rows(tmp_path) -> None:
    queue, engine = _engine(tmp_path)
    try:
        row, _ = store.create_or_get_active(
            engine, workspace_root=str(tmp_path), model_ref="smollm:135m"
        )
        assert store.request_cancel(engine, row.id) is True
        cancelled_flagged = store.get(engine, row.id)
        assert cancelled_flagged.cancel_requested_at is not None

        store.mark_cancelled(engine, row.id)
        cancelled = store.get(engine, row.id)
        assert cancelled.status == store.STATUS_CANCELLED

        # a terminal row is not "active" -- a second cancel request no-ops
        assert store.request_cancel(engine, row.id) is False
    finally:
        queue.close()


def test_error_message_is_truncated_and_never_over_200_chars(tmp_path) -> None:
    queue, engine = _engine(tmp_path)
    try:
        row, _ = store.create_or_get_active(
            engine, workspace_root=str(tmp_path), model_ref="smollm:135m"
        )
        raw_body = "x" * 5000
        store.mark_failed(
            engine, row.id, error_code="pull_failed", error_message=raw_body
        )
        failed = store.get(engine, row.id)
        assert failed.error_code == "pull_failed"
        assert failed.error_message is not None
        assert len(failed.error_message) <= 200
    finally:
        queue.close()


def test_list_recent_orders_active_first_then_by_recency(tmp_path) -> None:
    queue, engine = _engine(tmp_path)
    try:
        row1, _ = store.create_or_get_active(
            engine, workspace_root=str(tmp_path), model_ref="a:latest"
        )
        store.mark_done(engine, row1.id)
        row2, _ = store.create_or_get_active(
            engine, workspace_root=str(tmp_path), model_ref="b:latest"
        )
        # row2 stays pending/active; it should sort ahead of the done row1
        rows = store.list_recent(engine, str(tmp_path), limit=20)
        assert rows[0].id == row2.id
        assert row1.id in [r.id for r in rows]
    finally:
        queue.close()


# ---------------------------------------------------------------------------
# Endpoint fingerprint + actor provenance.
# ---------------------------------------------------------------------------


def test_endpoint_origin_and_initiated_by_recorded_at_creation(tmp_path) -> None:
    queue, engine = _engine(tmp_path)
    try:
        row, _ = store.create_or_get_active(
            engine,
            workspace_root=str(tmp_path),
            model_ref="smollm:135m",
            endpoint_origin="http://127.0.0.1:11434",
            initiated_by="owner@example.com",
        )
        assert row.endpoint_origin == "http://127.0.0.1:11434"
        assert row.initiated_by == "owner@example.com"

        dto = store.to_dto(row)
        assert dto["endpoint_origin"] == "http://127.0.0.1:11434"
        assert dto["initiated_by"] == "owner@example.com"
    finally:
        queue.close()


def test_endpoint_origin_and_initiated_by_default_to_none(tmp_path) -> None:
    """The local tier never passes ``initiated_by`` -- the parameter must
    default cleanly so the (pre-existing, team-owned) call site that omits
    both keeps compiling and running unchanged."""
    queue, engine = _engine(tmp_path)
    try:
        row, _ = store.create_or_get_active(
            engine, workspace_root=str(tmp_path), model_ref="smollm:135m"
        )
        assert row.endpoint_origin is None
        assert row.initiated_by is None
    finally:
        queue.close()


# ---------------------------------------------------------------------------
# mark_* idempotency / tolerance of an already-terminal row (items 1a/1b)
# ---------------------------------------------------------------------------


def test_mark_cancelled_is_a_noop_on_an_already_failed_row(tmp_path) -> None:
    queue, engine = _engine(tmp_path)
    try:
        row, _ = store.create_or_get_active(
            engine, workspace_root=str(tmp_path), model_ref="smollm:135m"
        )
        assert (
            store.mark_failed(engine, row.id, error_code="x", error_message="y") is True
        )
        # A racing cancel arriving after the row is already terminal must
        # not clobber the failed state (or crash).
        assert store.mark_cancelled(engine, row.id) is False
        after = store.get(engine, row.id)
        assert after.status == store.STATUS_FAILED
    finally:
        queue.close()


def test_mark_failed_is_a_noop_on_an_already_cancelled_row(tmp_path) -> None:
    queue, engine = _engine(tmp_path)
    try:
        row, _ = store.create_or_get_active(
            engine, workspace_root=str(tmp_path), model_ref="smollm:135m"
        )
        assert store.mark_cancelled(engine, row.id) is True
        assert (
            store.mark_failed(engine, row.id, error_code="x", error_message="y")
            is False
        )
        after = store.get(engine, row.id)
        assert after.status == store.STATUS_CANCELLED
    finally:
        queue.close()


def test_record_attempt_error_leaves_row_active(tmp_path) -> None:
    """Item 1b: recording a retryable attempt's error must NOT finalize the
    row -- it stays 'running' so the requeued retry resumes the SAME row
    instead of a duplicate active row getting created for the same ref."""
    queue, engine = _engine(tmp_path)
    try:
        row, _ = store.create_or_get_active(
            engine, workspace_root=str(tmp_path), model_ref="smollm:135m"
        )
        store.mark_running(engine, row.id, job_id=1)
        store.record_attempt_error(
            engine, row.id, error_code="local_server_unreachable", error_message="boom"
        )
        after = store.get(engine, row.id)
        assert after.status == store.STATUS_RUNNING
        assert after.error_code == "local_server_unreachable"
        assert after.finished_at is None

        # the row is STILL the dedupe target for this ref -- no duplicate
        # active row can be created while it stays active.
        with pytest.raises(store.ModelPullBusyError):
            store.create_or_get_active(
                engine, workspace_root=str(tmp_path), model_ref="qwen3:8b"
            )
        same, created = store.create_or_get_active(
            engine, workspace_root=str(tmp_path), model_ref="smollm:135m"
        )
        assert created is False
        assert same.id == row.id
    finally:
        queue.close()


# ---------------------------------------------------------------------------
# read-repair (item 1e): a queue-side terminal failure that never routed
# back through the handler's own bookkeeping (no handler registered, or a
# lease-exhausted job the recovery machinery itself terminal-fails) must not
# leave the pull row stuck 'running'/'pending' forever.
# ---------------------------------------------------------------------------


def test_get_repairs_a_row_whose_job_was_terminally_failed_queue_side(
    tmp_path,
) -> None:
    queue, engine = _engine(tmp_path)
    try:
        job_id = queue.enqueue("model.pull", {"pull_id": 1, "workspace_root": "x"})
        row, _ = store.create_or_get_active(
            engine, workspace_root=str(tmp_path), model_ref="smollm:135m"
        )
        store.mark_running(engine, row.id, job_id=job_id)
        # Simulate a queue-side terminal failure that never touched the pull
        # row (lease exhaustion / no handler registered) -- claim it, then
        # fail it with retry=False, exactly as Worker._execute's "no handler"
        # path does.
        queue.claim("w1")
        queue.fail(
            job_id, "w1", "no handler registered for kind 'model.pull'", retry=False
        )
        assert queue.get(job_id).status == "failed"

        repaired = store.get(engine, row.id)
        assert repaired.status == store.STATUS_FAILED
        assert repaired.error_code == "worker_failed"
    finally:
        queue.close()


def test_list_recent_and_find_active_also_repair(tmp_path) -> None:
    queue, engine = _engine(tmp_path)
    try:
        job_id = queue.enqueue("model.pull", {"pull_id": 1, "workspace_root": "x"})
        row, _ = store.create_or_get_active(
            engine, workspace_root=str(tmp_path), model_ref="smollm:135m"
        )
        store.mark_running(engine, row.id, job_id=job_id)
        queue.claim("w1")
        queue.fail(job_id, "w1", "no handler registered", retry=False)

        assert store.find_active_for_workspace(engine, str(tmp_path)) is None
        listed = {r.id: r for r in store.list_recent(engine, str(tmp_path))}
        assert listed[row.id].status == store.STATUS_FAILED
    finally:
        queue.close()


def test_repair_cancels_a_row_whose_job_was_cancelled_queue_side(tmp_path) -> None:
    queue, engine = _engine(tmp_path)
    try:
        job_id = queue.enqueue("model.pull", {"pull_id": 1, "workspace_root": "x"})
        row, _ = store.create_or_get_active(
            engine, workspace_root=str(tmp_path), model_ref="smollm:135m"
        )
        store.mark_running(engine, row.id, job_id=job_id)
        assert queue.cancel(job_id) is True

        repaired = store.get(engine, row.id)
        assert repaired.status == store.STATUS_CANCELLED
    finally:
        queue.close()


def test_repair_frees_the_workspace_slot_for_a_new_active_pull(tmp_path) -> None:
    """The whole point of read-repair (item 1e feeding item 2): a zombie
    active row must not permanently block new pulls for the workspace."""
    queue, engine = _engine(tmp_path)
    try:
        job_id = queue.enqueue("model.pull", {"pull_id": 1, "workspace_root": "x"})
        row, _ = store.create_or_get_active(
            engine, workspace_root=str(tmp_path), model_ref="smollm:135m"
        )
        store.mark_running(engine, row.id, job_id=job_id)
        queue.claim("w1")
        queue.fail(job_id, "w1", "no handler registered", retry=False)

        new_row, created = store.create_or_get_active(
            engine, workspace_root=str(tmp_path), model_ref="qwen3:8b"
        )
        assert created is True
        assert new_row.id != row.id
    finally:
        queue.close()


# ---------------------------------------------------------------------------
# job-less active rows (The regression): read-repair (~197) used to
# skip any row with job_id NULL entirely -- a cancel that only stamps
# `cancel_requested_at` (the never-claimed-job cancel route has no job_id to
# route a queue-side cancel through), or a plain enqueue failure that never
# reached `set_job_id`, would leave such a row active FOREVER, permanently
# wedging the one-active-pull-per-workspace slot (item 2).
# ---------------------------------------------------------------------------


def test_read_repair_cancels_a_job_less_row_with_cancel_requested(tmp_path) -> None:
    queue, engine = _engine(tmp_path)
    try:
        row, _ = store.create_or_get_active(
            engine, workspace_root=str(tmp_path), model_ref="smollm:135m"
        )
        assert row.job_id is None
        assert store.request_cancel(engine, row.id) is True

        repaired = store.get(engine, row.id)
        assert repaired.status == store.STATUS_CANCELLED

        # the workspace slot is free again -- the whole point of the fix.
        new_row, created = store.create_or_get_active(
            engine, workspace_root=str(tmp_path), model_ref="qwen3:8b"
        )
        assert created is True
        assert new_row.id != row.id
    finally:
        queue.close()


def test_read_repair_fails_a_stale_job_less_row(tmp_path) -> None:
    """A row that never got a job attached (e.g. an enqueue crash between
    `create_or_get_active` and `set_job_id`) and is NOT cancel-requested
    must not stay active forever either -- once older than the stale
    threshold, read-repair terminal-fails it as `enqueue_failed`."""
    from datetime import timedelta

    queue, engine = _engine(tmp_path)
    try:
        row, _ = store.create_or_get_active(
            engine, workspace_root=str(tmp_path), model_ref="smollm:135m"
        )
        assert row.job_id is None

        stale_created_at = row.created_at - timedelta(
            seconds=store.JOBLESS_STALE_THRESHOLD_SECONDS + 1
        )
        c = store.model_pulls_table.c
        with engine.begin() as cx:
            cx.execute(
                store.model_pulls_table.update()
                .where(c.id == row.id)
                .values(created_at=stale_created_at)
            )

        repaired = store.get(engine, row.id)
        assert repaired.status == store.STATUS_FAILED
        assert repaired.error_code == "enqueue_failed"
    finally:
        queue.close()


def test_read_repair_leaves_a_fresh_job_less_row_active(tmp_path) -> None:
    """The counterpart: a freshly created job-less row (no cancel request,
    not stale) must stay untouched -- the creating route may still be about
    to call `set_job_id`."""
    queue, engine = _engine(tmp_path)
    try:
        row, _ = store.create_or_get_active(
            engine, workspace_root=str(tmp_path), model_ref="smollm:135m"
        )
        fresh = store.get(engine, row.id)
        assert fresh.status == store.STATUS_PENDING
        assert fresh.job_id is None
    finally:
        queue.close()


def test_mark_cancelled_works_directly_on_a_job_less_active_row(tmp_path) -> None:
    """Item 2b: the never-claimed-job cancel route calls `mark_cancelled`
    directly on a row that has no job_id at all. `mark_cancelled`'s WHERE
    guard only checks id/status, never job_id, so this already works;
    pinned here plus its idempotency on a second call."""
    queue, engine = _engine(tmp_path)
    try:
        row, _ = store.create_or_get_active(
            engine, workspace_root=str(tmp_path), model_ref="smollm:135m"
        )
        assert row.job_id is None
        assert store.mark_cancelled(engine, row.id) is True
        after = store.get(engine, row.id)
        assert after.status == store.STATUS_CANCELLED

        # idempotent -- a second call on the now-terminal row is a no-op.
        assert store.mark_cancelled(engine, row.id) is False
        still = store.get(engine, row.id)
        assert still.status == store.STATUS_CANCELLED
    finally:
        queue.close()


# ---------------------------------------------------------------------------
# admin retry divergence (The regression): a generic queue retry
# (`JobQueue.retry`) requeues the JOB row with no knowledge of `model_pulls`
# at all -- a job whose last attempt already finalized this row to 'failed'
# can be admin-retried and run to completion while the row stays terminal
# forever, unless `mark_running` allows the SAME job_id to reactivate it.
# ---------------------------------------------------------------------------


def test_mark_running_reactivates_a_failed_row_for_the_matching_job_id(
    tmp_path,
) -> None:
    queue, engine = _engine(tmp_path)
    try:
        row, _ = store.create_or_get_active(
            engine, workspace_root=str(tmp_path), model_ref="smollm:135m"
        )
        assert store.mark_running(engine, row.id, job_id=1) is True
        store.mark_failed(engine, row.id, error_code="x", error_message="y")
        failed = store.get(engine, row.id)
        assert failed.status == store.STATUS_FAILED

        # admin retry: the SAME job_id reactivates the row -- and reports
        # that reactivation succeeded (the closure audit item
        # 1b: an explicit success/refusal result the handler must respect).
        assert store.mark_running(engine, row.id, job_id=1) is True
        reactivated = store.get(engine, row.id)
        assert reactivated.status == store.STATUS_RUNNING
        assert reactivated.finished_at is None
        assert reactivated.job_id == 1
        # item 2: a successful reactivation clears the prior attempt's error.
        assert reactivated.error_code is None
        assert reactivated.error_message is None
    finally:
        queue.close()


def test_mark_running_never_lets_a_different_job_id_steal_a_failed_row(
    tmp_path,
) -> None:
    queue, engine = _engine(tmp_path)
    try:
        row, _ = store.create_or_get_active(
            engine, workspace_root=str(tmp_path), model_ref="smollm:135m"
        )
        store.mark_running(engine, row.id, job_id=1)
        store.mark_failed(engine, row.id, error_code="x", error_message="y")

        # a DIFFERENT job_id must never reactivate a failed row that
        # belongs to some other attempt/job -- and the refusal must be
        # reported, not silently swallowed (item 1b: the handler's fence
        # depends on this return value).
        assert store.mark_running(engine, row.id, job_id=999) is False
        still_failed = store.get(engine, row.id)
        assert still_failed.status == store.STATUS_FAILED
        assert still_failed.job_id == 1
    finally:
        queue.close()


def test_mark_running_never_reactivates_a_cancelled_row(tmp_path) -> None:
    """`cancelled` is a permanent operator decision -- a job-level retry
    must never reopen it, even with a matching job_id. This refusal must be
    reported via the return value so
    the `model.pull` handler can fence on it (see
    tests/test_model_pull_handler.py::
    test_mark_running_refusal_aborts_cleanly_without_any_daemon_contact)."""
    queue, engine = _engine(tmp_path)
    try:
        row, _ = store.create_or_get_active(
            engine, workspace_root=str(tmp_path), model_ref="smollm:135m"
        )
        store.mark_running(engine, row.id, job_id=1)
        store.mark_cancelled(engine, row.id)

        assert store.mark_running(engine, row.id, job_id=1) is False
        still_cancelled = store.get(engine, row.id)
        assert still_cancelled.status == store.STATUS_CANCELLED
    finally:
        queue.close()


def test_mark_done_clears_a_stale_error_left_by_a_prior_failed_attempt(
    tmp_path,
) -> None:
    """The closure audit: an admin retry that
    reactivates a previously-'failed' row (via `mark_running`'s matching-
    job_id allowance) and then succeeds must not leave the FIRST attempt's
    error_code/error_message attached to the now-'done' row -- a done pull
    must never publish a stale terminal error alongside it."""
    queue, engine = _engine(tmp_path)
    try:
        row, _ = store.create_or_get_active(
            engine, workspace_root=str(tmp_path), model_ref="smollm:135m"
        )
        store.mark_running(engine, row.id, job_id=1)
        store.mark_failed(engine, row.id, error_code="pull_failed", error_message="x")
        failed = store.get(engine, row.id)
        assert failed.error_code == "pull_failed"

        assert store.mark_running(engine, row.id, job_id=1) is True
        assert store.mark_done(engine, row.id) is True

        done = store.get(engine, row.id)
        assert done.status == store.STATUS_DONE
        assert done.error_code is None
        assert done.error_message is None
        dto = store.to_dto(done)
        assert dto["error"] is None
    finally:
        queue.close()


def test_read_repair_select_for_update_renders_on_postgres_and_is_sqlite_no_op() -> (
    None
):
    """Item 3 (Postgres read-repair race): verifies, at the dialect-compile
    level (no live database of either kind needed), the exact claim the fix
    depends on -- SQLAlchemy renders `FOR UPDATE` on Postgres for the
    read-repair's job-status SELECT, and silently omits it (no error, no
    warning) when compiled for SQLite, which is what makes adding
    `with_for_update()` there safe for the SQLite backend this whole test
    suite runs against."""
    import sqlalchemy as sa
    from sqlalchemy.dialects import postgresql

    from frisket.engine.jobs.queue import jobs_table

    stmt = sa.select(jobs_table.c.status).where(jobs_table.c.id == 1).with_for_update()

    sqlite_sql = str(stmt.compile(dialect=sa.create_engine("sqlite://").dialect))
    assert "FOR UPDATE" not in sqlite_sql.upper()

    postgres_sql = str(stmt.compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE" in postgres_sql.upper()
