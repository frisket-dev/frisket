from __future__ import annotations

import os

import pytest

from deterministic_time import controlled_time
from frisket.engine.jobs import HandlerRegistry, PostgresJobQueue, SqliteJobQueue

PG_URL = os.environ.get("FRISKET_PG_TEST_URL")


@pytest.fixture(params=["sqlite", "pg-on-sqlite"])
def queue(request, tmp_path):
    if request.param == "sqlite":
        q = SqliteJobQueue(tmp_path / "q.db")
    else:
        q = PostgresJobQueue(f"sqlite:///{tmp_path}/q-pg.db")
    yield q
    q.close()


def _counting(queue, method_name: str) -> list[int]:
    calls: list[int] = []
    real = getattr(queue, method_name)

    def counting(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    setattr(queue, method_name, counting)
    return calls


def test_worker_stays_live_while_mid_job(queue):
    """The lease-heartbeat thread re-stamps worker presence WHILE a job is
    executing. Deterministic shape: the handler is a gate the test holds, so
    'still mid-job' is a fact, not a race against a 0.5s sleep; the mid-job
    re-stamp is a positive await on the wrapped presence write."""
    stamps = _counting(queue, "record_worker_heartbeat")
    with controlled_time() as t:
        reg = HandlerRegistry()
        gate = t.gate()
        reg.register("slow", lambda payload, _context: gate(payload))
        jid = queue.enqueue("slow", {})
        # Small lease so the heartbeat-loop tick (lease_seconds/3, floored
        # at 50ms) fires promptly; liveness_interval=0 so no throttle window
        # hides the mid-job re-stamp (throttling has its own test below).
        worker = t.worker(queue, reg, lease_seconds=0.3, liveness_interval=0.0)
        t.background(worker.run_once)
        t.wait_entered()
        assert queue.get(jid).status == "running"  # provably still mid-job
        baseline = len(stamps)
        t.wait_until(
            lambda: len(stamps) > baseline,
            message="worker looked dead mid-job — the lease heartbeat thread "
            "never re-stamped worker_heartbeats",
        )
        assert queue.count_live_workers(within_seconds=60) > 0
        t.release()
        t.wait_finalized(jid)
        assert queue.get(jid).status == "done"


def test_worker_liveness_interval_still_throttles_during_a_job(queue):
    """The fix must not spam record_worker_heartbeat on every lease-heartbeat
    tick — it should still respect Worker.liveness_interval, same as the
    idle-poll path.

    Deterministic shape: the worker's liveness throttle runs on the
    controlled clock. Frozen clock -> after the initial top-of-run_once
    stamp, every heartbeat-loop tick is throttled (exactly 1 write, however
    many ticks fire); advancing the clock past the interval releases exactly
    the next write."""
    stamps = _counting(queue, "record_worker_heartbeat")
    ticks = _counting(queue, "heartbeat")
    with controlled_time() as t:
        reg = HandlerRegistry()
        gate = t.gate()
        reg.register("slow", lambda payload, _context: gate(payload))
        queue.enqueue("slow", {})
        worker = t.worker(
            queue,
            reg,
            lease_seconds=0.1,  # heartbeat-loop ticks every ~0.05s real
            liveness_interval=0.2,  # measured on the controlled clock
        )
        t.background(worker.run_once)
        t.wait_entered()
        # Several lease-heartbeat ticks fire while the clock is frozen...
        t.wait_until(lambda: len(ticks) >= 3, message="lease heartbeat never ticked")
        # ...and none of them may write presence: the interval has not
        # elapsed on the worker's clock. Deterministic negative — the clock
        # cannot move unless this test moves it.
        assert len(stamps) == 1  # the top-of-run_once stamp only
        # Advancing past the interval releases exactly the next tick's write.
        t.advance_ms(300)
        t.wait_until(
            lambda: len(stamps) >= 2,
            message="liveness stamp never resumed after the interval elapsed",
        )
        t.release()
