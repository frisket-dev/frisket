from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa

from frisket.engine.jobs import SqliteJobQueue
from frisket.engine.jobs.queue import job_handler_authorities_table
from frisket.engine.jobs.queue_health import queue_health_payload
from frisket.engine.jobs.worker import HandlerRegistry, Worker


def _project_payload(
    *, storage_organization_id: int = 17, project_id: str = "alpha"
) -> dict:
    return {
        "storage_organization_id": storage_organization_id,
        "project_id": project_id,
        "workspace_root": "/unused",
    }


def test_cancel_does_not_release_claimed_handler_authority(tmp_path) -> None:
    queue = SqliteJobQueue(tmp_path / "queue.db")
    try:
        job_id = queue.enqueue("action.run", _project_payload())
        claimed = queue.claim("worker-one", lease_seconds=60)
        assert claimed is not None
        assert claimed.handler_authority_id
        assert queue.has_active_project_handler("alpha", storage_organization_id=17)

        assert queue.cancel(job_id) is True
        assert queue.get(job_id).status == "cancelled"
        assert not queue.job_execution_stopped(job_id)
        assert not queue.job_execution_stopped(job_id + 1000)
        assert queue.has_active_project_handler("alpha", storage_organization_id=17)

        assert (
            queue.acknowledge_handler_exit(
                job_id,
                "worker-one",
                authority_id="not-the-claim",
            )
            is False
        )
        assert queue.has_active_project_handler("alpha", storage_organization_id=17)
        assert queue.acknowledge_handler_exit(
            job_id,
            "worker-one",
            authority_id=claimed.handler_authority_id,
        )
        assert not queue.has_active_project_handler("alpha", storage_organization_id=17)
        assert queue.job_execution_stopped(job_id)
    finally:
        queue.close()


def test_lease_recovery_can_overlap_two_independent_handler_authorities(
    tmp_path,
) -> None:
    now = [datetime(2026, 7, 27, tzinfo=UTC)]
    queue = SqliteJobQueue(tmp_path / "queue.db", clock=lambda: now[0])
    try:
        job_id = queue.enqueue("action.run", _project_payload())
        first = queue.claim("worker-one", lease_seconds=1)
        assert first is not None and first.handler_authority_id

        now[0] += timedelta(seconds=2)
        assert queue.recover_expired() == 1
        assert queue.has_active_project_handler("alpha", storage_organization_id=17)

        second = queue.claim("worker-two", lease_seconds=60)
        assert second is not None and second.handler_authority_id
        assert second.handler_authority_id != first.handler_authority_id
        assert queue.acknowledge_handler_exit(
            job_id,
            "worker-two",
            authority_id=second.handler_authority_id,
        )
        assert queue.has_active_project_handler("alpha", storage_organization_id=17)

        # Hard process death has no time/status inference. An operator or
        # supervisor that has established worker-one is stopped uses this
        # explicit recovery primitive.
        abandoned = queue.abandon_stopped_worker_handler_authorities(
            "worker-one",
            heartbeat_cutoff=now[0],
        )
        assert abandoned.abandoned_authorities == 1
        assert abandoned.live_worker is False
        assert not queue.has_active_project_handler("alpha", storage_organization_id=17)
    finally:
        queue.close()


def test_cancel_unstarted_refuses_a_claimed_then_recovered_job(tmp_path) -> None:
    now = [datetime(2026, 7, 27, tzinfo=UTC)]
    queue = SqliteJobQueue(tmp_path / "queue.db", clock=lambda: now[0])
    try:
        job_id = queue.enqueue("action.run", _project_payload())
        observed = queue.get(job_id)
        assert observed is not None and observed.attempts == 0

        claimed = queue.claim("worker-one", lease_seconds=1)
        assert claimed is not None and claimed.id == job_id
        now[0] += timedelta(seconds=2)
        assert queue.recover_expired() == 1
        recovered = queue.get(job_id)
        assert recovered is not None
        assert recovered.status == "queued"
        assert recovered.attempts == 1

        # The service's earlier attempts==0 observation is now stale.
        assert queue.cancel_unstarted(job_id) is False
        assert queue.get(job_id).status == "queued"
        assert queue.has_active_project_handler("alpha", storage_organization_id=17)
    finally:
        queue.close()


def test_worker_finally_releases_authority_after_cancelled_handler_exits(
    tmp_path,
) -> None:
    queue = SqliteJobQueue(tmp_path / "queue.db")
    entered = threading.Event()
    release = threading.Event()
    registry = HandlerRegistry()

    def handler(_payload: dict, _context) -> dict:
        entered.set()
        assert release.wait(timeout=10)
        return {"ok": True}

    registry.register("action.run", handler)
    job_id = queue.enqueue("action.run", _project_payload())
    worker = Worker(queue, registry, worker_id="worker-one", lease_seconds=60)
    # The handler thread itself is the authority that cancellation must not
    # erase; a deterministic clock cannot reproduce concurrent life.
    thread = threading.Thread(  # realtime: handler liveness is the subject
        target=worker.run_once
    )
    try:
        thread.start()
        assert entered.wait(timeout=10)
        assert queue.cancel(job_id) is True
        assert queue.has_active_project_handler("alpha", storage_organization_id=17)

        release.set()
        thread.join(timeout=10)
        assert not thread.is_alive()
        assert not queue.has_active_project_handler("alpha", storage_organization_id=17)
    finally:
        release.set()
        thread.join(timeout=10)
        queue.close()


def test_atomic_abandon_refuses_fresh_worker_without_releasing_authority(
    tmp_path,
) -> None:
    now = datetime(2026, 7, 27, 12, tzinfo=UTC)
    queue = SqliteJobQueue(tmp_path / "queue.db", clock=lambda: now)
    try:
        queue.enqueue("action.run", _project_payload())
        claimed = queue.claim("worker-one", lease_seconds=60)
        assert claimed is not None
        queue.record_worker_heartbeat("worker-one", now=now)

        refused = queue.abandon_stopped_worker_handler_authorities(
            "worker-one",
            heartbeat_cutoff=now - timedelta(seconds=90),
        )
        assert refused.live_worker is True
        assert refused.abandoned_authorities == 0
        assert queue.has_active_project_handler("alpha", storage_organization_id=17)

        abandoned = queue.abandon_stopped_worker_handler_authorities(
            "worker-one",
            heartbeat_cutoff=now + timedelta(seconds=1),
        )
        assert abandoned.live_worker is False
        assert abandoned.abandoned_authorities == 1
        assert not queue.has_active_project_handler("alpha", storage_organization_id=17)
    finally:
        queue.close()


# ---------------------------------------------------------------------------
# A crashed worker's authority is immortal BY DESIGN -- and must be visible.
#
# Nothing automatic may release these rows: a lease expires because a handler
# is slow just as readily as because its process died, and lease recovery
# deliberately hands the same job to a second worker WHILE THE FIRST IS STILL
# UNWINDING (see job_handler_authorities_table's comment). An authority
# released on a timeout would tell hosted project deletion that no code owns
# the project while code is still writing to it -- trading a stuck deletion
# for a purge underneath a live writer. So the rows stay, the operator lever
# (abandon_stopped_worker_handler_authorities, driven by a downstream
# composition's handler-authority-recover CLI) stays the only release, and
# what was missing
# was any way for an operator to LEARN a deletion is wedged.
# ---------------------------------------------------------------------------


def _crashed_worker_queue(tmp_path, now):
    """A worker that claimed, heartbeated once, and then died mid-handler."""
    queue = SqliteJobQueue(tmp_path / "queue.db", clock=lambda: now[0])
    queue.enqueue("action.run", _project_payload())
    queue.record_worker_heartbeat("worker-crash", now=now[0])
    claimed = queue.claim("worker-crash", lease_seconds=30)
    assert claimed is not None and claimed.handler_authority_id
    return queue


def test_lease_recovery_outlives_even_a_fully_finished_replacement_claim(
    tmp_path,
) -> None:
    """Characterization of the deliberate ruling, so a later 'fix' goes red."""
    now = [datetime(2026, 7, 28, tzinfo=UTC)]
    queue = _crashed_worker_queue(tmp_path, now)
    try:
        now[0] += timedelta(days=6)

        assert queue.recover_expired() == 1
        assert queue.has_active_project_handler("alpha", storage_organization_id=17)

        # a fresh process claims the requeued job, finishes it cleanly, and
        # releases ITS authority -- the dead worker's row still outlives it
        second = queue.claim("worker-two", lease_seconds=60)
        assert second is not None
        assert queue.complete(second.id, "worker-two")  # releases ITS authority
        assert queue.get(second.id).status == "done"

        # all work for this project is over, yet deletion still sees an owner
        assert queue.has_active_project_handler("alpha", storage_organization_id=17)
        with queue.engine.connect() as cx:
            holders = (
                cx.execute(sa.select(job_handler_authorities_table.c.worker_id))
                .scalars()
                .all()
            )
        assert holders == ["worker-crash"]
    finally:
        queue.close()


def test_expired_lease_recovers_without_releasing_the_authority(
    tmp_path,
) -> None:
    """Lease expiry, rather than release identity, is the recovery boundary."""
    now = [datetime(2026, 7, 28, tzinfo=UTC)]
    queue = _crashed_worker_queue(tmp_path, now)
    try:
        job_id = queue.get(1).id
        assert queue.get(job_id).status == "running"

        now[0] += timedelta(seconds=61)
        assert queue.recover_expired() == 1
        assert queue.get(job_id).status == "queued"
        assert queue.get(job_id).locked_by is None

        assert queue.has_active_project_handler("alpha", storage_organization_id=17)
    finally:
        queue.close()


def test_health_publishes_the_wedge_a_crashed_worker_leaves_behind(tmp_path) -> None:
    now = [datetime(2026, 7, 28, tzinfo=UTC)]
    queue = _crashed_worker_queue(tmp_path, now)
    try:
        now[0] += timedelta(days=6)
        queue.recover_expired()

        payload = queue_health_payload(queue, now=now[0], liveness_window_seconds=60)
        authorities = payload["handler_authorities"]
        assert authorities["unheard"] == 1
        # the age is what makes it actionable: a restart blip vs. a six-day
        # wedge are the same bare count
        assert authorities["oldest_unheard_claim_age_seconds"] == 6 * 86400.0
    finally:
        queue.close()


def test_a_live_worker_holding_an_authority_is_never_reported_unheard(
    tmp_path,
) -> None:
    """No crying wolf: normal in-flight work must not look like a wedge."""
    now = [datetime(2026, 7, 28, tzinfo=UTC)]
    queue = _crashed_worker_queue(tmp_path, now)
    try:
        now[0] += timedelta(seconds=10)
        queue.record_worker_heartbeat("worker-crash", now=now[0])

        payload = queue_health_payload(queue, now=now[0], liveness_window_seconds=60)
        assert queue.has_active_project_handler("alpha", storage_organization_id=17)
        assert payload["handler_authorities"] == {
            "unheard": 0,
            "oldest_unheard_claim_age_seconds": None,
        }
    finally:
        queue.close()


def test_the_report_and_the_operator_lever_agree_on_which_workers_are_quiet(
    tmp_path,
) -> None:
    """The count published must be the count the recovery CLI would release.

    Two spellings of "this worker has gone quiet" would let health report a
    wedge the lever then refuses to clear (or the reverse).
    """
    now = [datetime(2026, 7, 28, tzinfo=UTC)]
    queue = _crashed_worker_queue(tmp_path, now)
    try:
        # a SECOND worker on another project, still heartbeating
        queue.enqueue("action.run", _project_payload(project_id="beta"))
        now[0] += timedelta(days=6)
        queue.record_worker_heartbeat("worker-live", now=now[0])
        live_claim = queue.claim("worker-live", lease_seconds=60)
        assert live_claim is not None

        cutoff = now[0] - timedelta(seconds=60)
        reported = queue.unheard_handler_authorities(
            heartbeat_cutoff=cutoff, now=now[0]
        )
        assert reported.count == 1

        refused = queue.abandon_stopped_worker_handler_authorities(
            "worker-live", heartbeat_cutoff=cutoff
        )
        assert refused.live_worker is True and refused.abandoned_authorities == 0

        released = queue.abandon_stopped_worker_handler_authorities(
            "worker-crash", heartbeat_cutoff=cutoff
        )
        assert released.abandoned_authorities == reported.count

        assert not queue.has_active_project_handler("alpha", storage_organization_id=17)
        assert queue.has_active_project_handler("beta", storage_organization_id=17)
        assert (
            queue.unheard_handler_authorities(heartbeat_cutoff=cutoff, now=now[0]).count
            == 0
        )
    finally:
        queue.close()


def test_a_worker_that_never_heartbeated_at_all_counts_as_unheard(tmp_path) -> None:
    """An absent heartbeat row is not evidence of liveness."""
    now = datetime(2026, 7, 28, tzinfo=UTC)
    queue = SqliteJobQueue(tmp_path / "queue.db", clock=lambda: now)
    try:
        queue.enqueue("action.run", _project_payload())
        assert queue.claim("silent-worker", lease_seconds=60) is not None
        assert not queue.list_worker_heartbeats()

        reported = queue.unheard_handler_authorities(
            heartbeat_cutoff=now - timedelta(seconds=60), now=now
        )
        assert reported.count == 1
        assert reported.oldest_claim_age_seconds == 0.0
    finally:
        queue.close()
