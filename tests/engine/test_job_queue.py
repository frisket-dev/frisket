import functools
import os
import subprocess
import sys

import pytest

from frisket.engine.jobs import (
    QUEUE_DB_NAME,
    PostgresJobQueue,
    SqliteJobQueue,
    open_queue,
)

PG_URL = os.environ.get("FRISKET_PG_TEST_URL")


@pytest.fixture
def queue(tmp_path):
    # One class serves both backends; its SQLite configuration IS the local
    # backend under test. Real-Postgres claim semantics stay on the
    # FRISKET_PG_TEST_URL-gated `pg_queue` fixture below.
    q = SqliteJobQueue(tmp_path / "q.db")
    yield q
    q.close()


@pytest.fixture
def clocked_queue(tmp_path):
    """A queue on the controlled-time front door: lease expiry happens when
    the test advances the clock, never by racing a real sleep against a
    tiny lease."""
    from deterministic_time import controlled_time

    with controlled_time() as t:
        yield t.queue(tmp_path / "q.db"), t


@pytest.fixture
def pg_queue(tmp_path):
    """A real Postgres queue when FRISKET_PG_TEST_URL is set; else skip."""
    if not PG_URL:
        pytest.skip("FRISKET_PG_TEST_URL not set (real-postgres tests gated)")
    q = PostgresJobQueue(PG_URL)
    # isolate: drop leftovers from previous runs
    import sqlalchemy as sa

    with q.engine.begin() as cx:
        cx.execute(sa.text("DELETE FROM jobs"))
    yield q
    q.close()


class TestLifecycle:
    def test_enqueue_get_roundtrip(self, queue):
        jid = queue.enqueue("echo", {"x": 1}, max_attempts=5)
        job = queue.get(jid)
        assert job.kind == "echo"
        assert job.payload == {"x": 1}
        assert job.status == "queued"
        assert job.attempts == 0
        assert job.max_attempts == 5

    def test_claim_marks_running_with_lease(self, queue):
        jid = queue.enqueue("echo", {})
        job = queue.claim("w1", lease_seconds=60)
        assert job.id == jid
        assert job.status == "running"
        assert job.attempts == 1
        assert job.locked_by == "w1"
        assert job.lease_expires_at > job.locked_at

    def test_claim_is_fifo(self, queue):
        ids = [queue.enqueue("echo", {"i": i}) for i in range(3)]
        claimed = [queue.claim("w1").id for _ in range(3)]
        assert claimed == ids

    def test_claim_empty_returns_none(self, queue):
        assert queue.claim("w1") is None

    def test_delayed_job_not_claimable_until_available(self, queue):
        queue.enqueue("echo", {}, delay_seconds=30)
        assert queue.claim("w1") is None

    def test_complete(self, queue):
        jid = queue.enqueue("echo", {"x": 1})
        queue.claim("w1")
        assert queue.complete(jid, "w1", {"ok": True})
        job = queue.get(jid)
        assert job.status == "done"
        assert job.result == {"ok": True}
        assert job.finished_at is not None
        assert job.locked_by is None

    def test_complete_by_wrong_worker_rejected(self, queue):
        jid = queue.enqueue("echo", {})
        queue.claim("w1")
        assert not queue.complete(jid, "imposter")
        assert queue.get(jid).status == "running"

    def test_cancel_queued_only(self, queue):
        jid = queue.enqueue("echo", {})
        assert queue.cancel(jid)
        assert queue.get(jid).status == "cancelled"
        # running jobs are not the queue's to cancel
        jid2 = queue.enqueue("echo", {})
        queue.claim("w1")
        assert not queue.cancel(jid2)

    def test_counts_and_list(self, queue):
        first = queue.enqueue("echo", {})
        second = queue.enqueue("echo", {})
        queue.claim("w1")  # claims `first` (FIFO)
        counts = queue.counts()
        assert counts["queued"] == 1
        assert counts["running"] == 1
        assert {j.id for j in queue.list_jobs()} == {first, second}
        assert [j.id for j in queue.list_jobs(status="queued")] == [second]


class TestConcurrentColdOpen:
    def test_two_processes_cold_open_same_sqlite_queue(self, tmp_path):
        """Two independent processes constructing SqliteJobQueue against the
        same file-backed database at once, both exit zero. This proves the
        queue-initialization filelock.FileLock (not merely the in-process
        thread lock), which serializes the first WAL negotiation and
        migration run across processes."""
        db_path = tmp_path / "cold-open.db"
        code = (
            "from frisket.engine.jobs import SqliteJobQueue; "
            "q = SqliteJobQueue(__import__('sys').argv[1]); "
            "q.enqueue('echo', {}); q.close()"
        )
        processes = [
            subprocess.Popen(
                [sys.executable, "-c", code, str(db_path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(2)
        ]
        results = [p.communicate(timeout=30) for p in processes]
        assert [p.returncode for p in processes] == [0, 0], results

        # The queue is usable afterward and has both processes' enqueued jobs.
        q = SqliteJobQueue(db_path)
        try:
            assert q.counts()["queued"] == 2
        finally:
            q.close()


class TestRetries:
    def test_fail_requeues_with_backoff(self, queue):
        jid = queue.enqueue("echo", {}, max_attempts=3)
        queue.claim("w1")
        assert queue.fail(jid, "w1", "boom", retry_delay_seconds=60)
        job = queue.get(jid)
        assert job.status == "queued"
        assert job.error == "queue_job_failed: boom"
        assert job.attempts == 1
        # backoff: not claimable until available_at
        assert queue.claim("w1") is None

    def test_fail_exhausts_to_failed(self, queue):
        jid = queue.enqueue("echo", {}, max_attempts=2)
        for _ in range(2):
            queue.claim("w1")
            queue.fail(jid, "w1", "boom", retry_delay_seconds=0)
        job = queue.get(jid)
        assert job.status == "failed"
        assert job.attempts == 2
        assert queue.claim("w1") is None

    def test_fail_no_retry_is_terminal(self, queue):
        jid = queue.enqueue("echo", {}, max_attempts=5)
        queue.claim("w1")
        queue.fail(jid, "w1", "no handler", retry=False)
        assert queue.get(jid).status == "failed"

    def test_fail_redacts_secret_and_preserves_canonical_code(self, queue):
        sentinel = "sk-queue-failure-sentinel"
        jid = queue.enqueue("echo", {}, max_attempts=1)
        queue.claim("w1")

        assert queue.fail(
            jid,
            "w1",
            f"provider_timeout: api_key={sentinel}\nretry exhausted",
            retry=False,
        )

        error = queue.get(jid).error
        assert error == "provider_timeout: api_key=[REDACTED] retry exhausted"
        assert sentinel not in error
        assert "Traceback" not in error

    def test_fail_queued_applies_same_defense_in_depth(self, queue):
        sentinel = "sk-queued-failure-sentinel"
        jid = queue.enqueue("echo", {})

        assert queue.fail_queued(jid, f"Authorization: Bearer {sentinel}")

        job = queue.get(jid)
        assert job.status == "failed"
        assert job.error == "queue_job_failed: Authorization: [REDACTED]"
        assert sentinel not in job.error


class TestLeases:
    """Lease expiry on the queue's injected manual clock: the test advances
    time past the lease instead of sleeping real 50ms against a 10ms lease."""

    def test_heartbeat_extends_lease(self, clocked_queue):
        queue, _clock = clocked_queue
        jid = queue.enqueue("echo", {})
        job = queue.claim("w1", lease_seconds=1)
        assert queue.heartbeat(jid, "w1", lease_seconds=600)
        assert queue.get(jid).lease_expires_at > job.lease_expires_at

    def test_heartbeat_after_recovery_fails(self, clocked_queue):
        queue, clock = clocked_queue
        jid = queue.enqueue("echo", {})
        queue.claim("w1", lease_seconds=60)
        clock.advance_seconds(120)
        assert queue.recover_expired() == 1
        assert not queue.heartbeat(jid, "w1")  # lost the lease

    def test_dead_worker_job_recovers_to_queued(self, clocked_queue):
        queue, clock = clocked_queue
        jid = queue.enqueue("echo", {}, max_attempts=3)
        queue.claim("dead-worker", lease_seconds=60)
        clock.advance_seconds(120)
        assert queue.recover_expired() == 1
        job = queue.get(jid)
        assert job.status == "queued"
        assert job.error == "worker_lease_expired: worker lease expired"
        assert job.locked_by is None
        # and it is claimable again, attempts carried forward
        job2 = queue.claim("w2")
        assert job2.id == jid
        assert job2.attempts == 2

    def test_expired_lease_recovery_preserves_an_existing_stored_error(
        self, clocked_queue
    ):
        from frisket.engine.jobs.queue import jobs_table

        queue, clock = clocked_queue
        existing = "worker_crash: worker detail kept exactly"
        jid = queue.enqueue("echo", {}, max_attempts=3)
        queue.claim("dead-worker", lease_seconds=60)
        with queue.engine.begin() as cx:
            cx.execute(
                jobs_table.update().where(jobs_table.c.id == jid).values(error=existing)
            )
        clock.advance_seconds(120)

        assert queue.recover_expired() == 1
        assert queue.get(jid).error == existing

    def test_expired_lease_with_exhausted_attempts_fails(self, clocked_queue):
        queue, clock = clocked_queue
        jid = queue.enqueue("echo", {}, max_attempts=1)
        queue.claim("dead-worker", lease_seconds=60)
        clock.advance_seconds(120)
        queue.recover_expired()
        job = queue.get(jid)
        assert job.status == "failed"
        assert job.error == (
            "worker_lease_expired: worker lease expired (attempts exhausted)"
        )

    def test_live_lease_not_recovered(self, clocked_queue):
        # A NEGATIVE assertion, deterministic under the frozen manual clock:
        # time cannot pass unless this test advances it.
        queue, _clock = clocked_queue
        queue.enqueue("echo", {})
        queue.claim("w1", lease_seconds=600)
        assert queue.recover_expired() == 0


class TestSqliteConcurrency:
    """BEGIN IMMEDIATE semantics: parallel claimers never double-claim."""

    def test_two_claimers_no_double_claims(self, tmp_path):
        from deterministic_time import controlled_time

        q = SqliteJobQueue(tmp_path / "q.db")
        n = 30
        for i in range(n):
            q.enqueue("echo", {"i": i})
        claimed: dict[str, list[int]] = {"w1": [], "w2": []}

        def claimer(wid):
            while True:
                job = q.claim(wid)
                if job is None:
                    return
                claimed[wid].append(job.id)

        with controlled_time() as t:
            threads = [
                t.background(functools.partial(claimer, w)) for w in ("w1", "w2")
            ]
            for thread in threads:
                thread.join(timeout=30)
        all_ids = claimed["w1"] + claimed["w2"]
        assert len(all_ids) == n
        assert len(set(all_ids)) == n  # no job handed out twice
        q.close()


@pytest.mark.skipif(not PG_URL, reason="FRISKET_PG_TEST_URL not set")
class TestPostgresSkipLocked:
    """Real-postgres SKIP LOCKED semantics (gated; see module docstring)."""

    def test_two_concurrent_claimers_no_double_claims(self, pg_queue):
        from deterministic_time import controlled_time

        n = 30
        for i in range(n):
            pg_queue.enqueue("echo", {"i": i})
        claimed: dict[str, list[int]] = {"w1": [], "w2": []}

        def claimer(wid):
            while True:
                job = pg_queue.claim(wid)
                if job is None:
                    return
                claimed[wid].append(job.id)

        with controlled_time() as t:
            threads = [
                t.background(functools.partial(claimer, w)) for w in ("w1", "w2")
            ]
            for thread in threads:
                thread.join(timeout=30)
        all_ids = claimed["w1"] + claimed["w2"]
        assert len(all_ids) == n
        assert len(set(all_ids)) == n

    def test_skip_locked_skips_a_held_row(self, pg_queue):
        """A row locked by an open claiming transaction is skipped, not
        blocked on: the second claimer immediately gets the NEXT job."""
        import sqlalchemy as sa

        from frisket.engine.jobs import jobs_table

        first = pg_queue.enqueue("echo", {"i": 0})
        second = pg_queue.enqueue("echo", {"i": 1})
        c = jobs_table.c
        with pg_queue.engine.connect() as cx:
            cx.execute(sa.text("BEGIN"))
            held = cx.execute(
                sa.select(c.id)
                .where(c.status == "queued")
                .order_by(c.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            ).scalar_one()
            assert held == first
            job = pg_queue.claim("w2")  # separate connection, must not block
            assert job.id == second
            cx.execute(sa.text("ROLLBACK"))


class TestOpenQueue:
    def test_open_queue_workspace_sqlite(self, tmp_path):
        q = open_queue(workspace=tmp_path)
        assert isinstance(q, SqliteJobQueue)
        assert (tmp_path / QUEUE_DB_NAME).exists()
        q.close()

    def test_open_queue_database_url(self, tmp_path):
        q = open_queue(database_url=f"sqlite:///{tmp_path}/cp.db")
        assert isinstance(q, PostgresJobQueue)
        q.close()

    def test_open_queue_requires_one(self):
        with pytest.raises(ValueError):
            open_queue()


class TestClaimedProjectLocation:
    """Trusted handler path resolution for claimed storage identity.

    A claimed ProjectStorageKey always beats mutable payload fields; the
    directory comes from registration config. workspace_root_storage_org_id
    declares an org-scoped root (hosted per-org Workspace) so a matching
    claim resolves directly under it instead of double-nesting, and a claim
    for another org fails closed.
    """

    def _payload(self, key):
        from frisket.engine.jobs.queue import CLAIMED_PROJECT_STORAGE_KEY_PAYLOAD_KEY

        # Poisoned mutable fields: the claim must win over both of these.
        return {
            CLAIMED_PROJECT_STORAGE_KEY_PAYLOAD_KEY: key,
            "project_id": "attacker-slug",
            "workspace_root": "/attacker/root",
        }

    def test_global_root_appends_claimed_org(self, tmp_path):
        from frisket.engine.jobs.queue import claimed_project_location
        from frisket.project_identity import ProjectStorageKey

        key = ProjectStorageKey(storage_org_id=7, project_slug="proj")
        slug, root, path = claimed_project_location(
            self._payload(key),
            workspace_root=tmp_path,
            require_storage_identity=False,
        )
        assert slug == "proj"
        assert root == tmp_path / "7"
        assert path == tmp_path / "7" / "proj.frisket"

    def test_declared_org_scoped_root_resolves_without_double_nesting(self, tmp_path):
        from frisket.engine.jobs.queue import claimed_project_location
        from frisket.project_identity import ProjectStorageKey

        key = ProjectStorageKey(storage_org_id=7, project_slug="proj")
        org_root = tmp_path / "7"
        slug, root, path = claimed_project_location(
            self._payload(key),
            workspace_root=org_root,
            require_storage_identity=False,
            workspace_root_storage_org_id=7,
        )
        assert slug == "proj"
        assert root == org_root
        assert path == org_root / "proj.frisket"

    def test_declared_org_scoped_root_rejects_cross_org_claim(self, tmp_path):
        from frisket.engine.jobs.queue import claimed_project_location
        from frisket.project_identity import ProjectStorageKey

        key = ProjectStorageKey(storage_org_id=8, project_slug="proj")
        with pytest.raises(ValueError, match="(?i)storage"):
            claimed_project_location(
                self._payload(key),
                workspace_root=tmp_path / "7",
                require_storage_identity=False,
                workspace_root_storage_org_id=7,
            )

    def test_missing_claim_fails_closed_when_identity_required(self, tmp_path):
        from frisket.engine.jobs.queue import claimed_project_location

        with pytest.raises(ValueError, match="(?i)storage identity"):
            claimed_project_location(
                {"project_id": "proj", "workspace_root": str(tmp_path)},
                workspace_root=tmp_path,
                require_storage_identity=True,
            )
