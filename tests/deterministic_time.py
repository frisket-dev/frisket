"""Deterministic-time harness — the ONE test module allowed to touch real
time primitives.

The rule it serves: a negative assertion may never race a real clock.
"X didn't happen" is proved under a manual clock or a rendezvous
(happens-before), never by outrunning a real-time window.

The test-facing API is the single front door::

    with controlled_time() as t:
        queue = t.queue(tmp_path / "q.db")       # clock-injected at construction
        worker = t.worker(queue, registry)       # clock + transition hook wired
        t.background(worker.run_once)            # joined + checked on exit
        t.wait_entered(); ...; t.release()       # the rendezvous vocabulary
        t.advance_seconds(120); t.now()          # the manual clock vocabulary
        t.wait_claimed(jid); t.wait_finalized(jid)
        t.wait_until(lambda: ..., message=...)   # positive-assertion await
        b = t.item_barrier()                     # repeatable per-item barrier:
        b.wait_arrived(1); b.release(1)          #   park/step each row of a
        b.release_all()                          #   batch job, not whole-job
        t.monotonic(); t.async_sleeper()         # seconds clock + asyncio-
                                                 #   sleep seams (datalab poll,
                                                 #   the Nominatim pacer)

Tests never assemble clocks, hooks, and rendezvous individually; if a shape
cannot be expressed through the front door, extend the front door. The
context manager is ergonomics only — the src-side mechanism stays explicit
constructor injection (declared, never ambient).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

DEFAULT_TIMEOUT = 30.0

# Manual-clock datetime origin: an arbitrary fixed instant. Tests advance
# from here; nothing may compare it against the real wall clock.
EPOCH = datetime(2026, 1, 1, tzinfo=UTC)


class ManualClock:
    """The rate_limiter clock idiom (``now_ms``) plus a datetime view
    (``now``) for ``SqlAlchemyJobQueue(clock=...)``. Advances only when the
    test says so."""

    def __init__(self, start_ms: int = 0) -> None:
        self._now_ms = int(start_ms)
        self._lock = threading.Lock()

    def now_ms(self) -> int:
        with self._lock:
            return self._now_ms

    def now(self) -> datetime:
        return EPOCH + timedelta(milliseconds=self.now_ms())

    def advance_ms(self, ms: int) -> None:
        with self._lock:
            self._now_ms += int(ms)


class ManualSleeper:
    """Records requested sleeps and advances the manual clock instead of
    sleeping (the idiom the rate-limiter tests already use inline)."""

    def __init__(self, clock: ManualClock) -> None:
        self.clock = clock
        self.sleeps: list[int] = []

    def sleep_ms(self, ms: int) -> None:
        self.sleeps.append(ms)
        self.clock.advance_ms(ms)


class _Rendezvous:
    """Two-event handshake: the waiting side signals *entered* and blocks
    until the test *releases* — giving whatever the test did in between a
    happens-before edge over the waiter's next loop iteration."""

    def __init__(self, *, timeout: float) -> None:
        self.timeout = timeout
        self.entered = threading.Event()
        self.released = threading.Event()

    def arrive(self) -> None:
        self.entered.set()
        if not self.released.wait(self.timeout):
            raise AssertionError(f"rendezvous never released after {self.timeout}s")


class _RendezvousSleeper:
    """A rate-limiter ``sleeper`` that rendezvouses instead of sleeping."""

    def __init__(self, rendezvous: _Rendezvous) -> None:
        self.rendezvous = rendezvous
        self.sleeps: list[int] = []

    def sleep_ms(self, ms: int) -> None:
        self.sleeps.append(ms)
        self.rendezvous.arrive()


class ManualAsyncSleeper:
    """An ``asyncio.sleep``-shaped seam (seconds) that records requested
    sleeps and advances the manual clock instead of sleeping — for the
    async clock/sleep seams (``datalab_convert``'s poll loop, geocode's
    Nominatim pacer)."""

    def __init__(self, clock: ManualClock) -> None:
        self.clock = clock
        self.sleeps: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.clock.advance_ms(int(seconds * 1000))


class _ItemBarrier:
    """The repeatable per-item rendezvous (THE rendezvous is a single
    arrive/release handshake; batch jobs need one per row). Each ``arrive()``
    takes the next ticket and parks until the test has released at least
    that many items — so "exactly k items have finished while the rest are
    still parked" is a fact the test constructs, not a window it races."""

    def __init__(self, *, timeout: float) -> None:
        self.timeout = timeout
        self._cond = threading.Condition()
        self._arrived = 0
        self._released = 0

    def arrive(self) -> None:
        with self._cond:
            self._arrived += 1
            ticket = self._arrived
            self._cond.notify_all()
            if not self._cond.wait_for(lambda: self._released >= ticket, self.timeout):
                raise AssertionError(
                    f"item {ticket} never released after {self.timeout}s"
                )

    def wait_arrived(self, count: int) -> None:
        with self._cond:
            if not self._cond.wait_for(lambda: self._arrived >= count, self.timeout):
                raise AssertionError(
                    f"item {count} never arrived after {self.timeout}s "
                    f"(saw {self._arrived})"
                )

    def release(self, count: int = 1) -> None:
        with self._cond:
            self._released += count
            self._cond.notify_all()

    def release_all(self) -> None:
        with self._cond:
            self._released = 1 << 30
            self._cond.notify_all()


class _WorkerEvents:
    """A ``Worker.set_transition_hook`` consumer: records (event, job_id)
    transitions and lets tests await them."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._seen: list[tuple[str, int]] = []

    def __call__(self, event: str, job: Any) -> None:
        with self._cond:
            self._seen.append((event, job.id))
            self._cond.notify_all()

    def wait(self, event: str, job_id: int | None, *, timeout: float) -> None:
        def _hit() -> bool:
            return any(
                seen_event == event and (job_id is None or seen_id == job_id)
                for seen_event, seen_id in self._seen
            )

        with self._cond:
            if not self._cond.wait_for(_hit, timeout):
                raise AssertionError(
                    f"worker transition {event!r} (job {job_id}) "
                    f"not seen after {timeout}s; saw {self._seen!r}"
                )


class ControlledTime:
    """The yielded front-door object; see the module docstring. One manual
    clock, one rendezvous, one transition-event stream per context."""

    def __init__(self, *, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.timeout = timeout
        self._clock = ManualClock()
        self._rendezvous = _Rendezvous(timeout=timeout)
        self._events = _WorkerEvents()
        self._threads: list[threading.Thread] = []
        self._queues: list[Any] = []
        self._barriers: list[_ItemBarrier] = []

    # ---------- the manual clock ----------

    def now_ms(self) -> int:
        return self._clock.now_ms()

    def now(self) -> datetime:
        return self._clock.now()

    def advance_ms(self, ms: int) -> None:
        self._clock.advance_ms(ms)

    def advance_seconds(self, seconds: float) -> None:
        self._clock.advance_ms(int(seconds * 1000))

    def monotonic(self) -> float:
        """Seconds view of the manual clock — for seams injected as a bare
        ``time.monotonic``-shaped callable (``datalab_convert``'s poll
        deadline, geocode's ``_nominatim_pace``)."""
        return self._clock.now_ms() / 1000.0

    # ---------- construction-time wiring ----------

    def queue(self, locator: str | Path, **kwargs: Any) -> Any:
        """A job queue on the manual clock (closed on context exit)."""
        from frisket.engine.jobs import SqliteJobQueue

        q = SqliteJobQueue(locator, clock=self._clock.now, **kwargs)
        self._queues.append(q)
        return q

    def worker(self, queue: Any, registry: Any = None, **kwargs: Any) -> Any:
        """A Worker on the manual clock with the transition hook wired."""
        from frisket.engine.jobs import Worker

        w = Worker(queue, registry, clock=self._clock, **kwargs)
        w.set_transition_hook(self._events)
        return w

    def sleeper(self) -> ManualSleeper:
        """A limiter sleeper that advances the manual clock (pure-logic
        limiter tests: assert on ``.sleeps``)."""
        return ManualSleeper(self._clock)

    def rendezvous_sleeper(self) -> _RendezvousSleeper:
        """A limiter sleeper that parks at THE rendezvous: the waiter
        arrives at its first wait chunk and stays until ``release()``."""
        return _RendezvousSleeper(self._rendezvous)

    def async_sleeper(self) -> ManualAsyncSleeper:
        """An ``asyncio.sleep``-shaped seam (seconds) that advances the
        manual clock (assert on ``.sleeps``)."""
        return ManualAsyncSleeper(self._clock)

    def item_barrier(self) -> _ItemBarrier:
        """A repeatable per-item barrier for a batch job's intra-job
        progress (fully released on context exit — a failing test never
        leaves rows parked)."""
        barrier = _ItemBarrier(timeout=self.timeout)
        self._barriers.append(barrier)
        return barrier

    def gate(self) -> Callable[[dict], dict]:
        """A registry handler that parks at THE rendezvous — "the job is
        still mid-handler" as a fact the test controls."""

        def handler(payload: dict) -> dict:
            self._rendezvous.arrive()
            return {"ok": True}

        return handler

    def background(self, fn: Callable[[], Any]) -> threading.Thread:
        """Run ``fn`` on a daemon thread; joined and liveness-checked on
        context exit."""
        t = threading.Thread(target=fn, daemon=True)
        t.start()
        self._threads.append(t)
        return t

    # ---------- awaits (positive, generous timeouts) ----------

    def wait_entered(self) -> None:
        if not self._rendezvous.entered.wait(self.timeout):
            raise AssertionError(f"waiter never arrived after {self.timeout}s")

    def release(self) -> None:
        self._rendezvous.released.set()

    def wait_claimed(self, job_id: int | None = None) -> None:
        self._events.wait("claimed", job_id, timeout=self.timeout)

    def wait_finalized(self, job_id: int | None = None) -> None:
        self._events.wait("finalized", job_id, timeout=self.timeout)

    def wait_until(
        self,
        predicate: Callable[[], Any],
        *,
        message: str = "condition not reached",
        interval: float = 0.01,
    ) -> Any:
        """Poll until ``predicate`` returns truthy and return that value.
        The timeout is a liveness bound, never a window anything is asserted
        NOT to happen inside."""
        deadline = time.monotonic() + self.timeout
        while True:
            value = predicate()
            if value:
                return value
            if time.monotonic() >= deadline:
                raise AssertionError(
                    f"wait_until timed out after {self.timeout}s: {message}"
                )
            time.sleep(interval)

    # ---------- teardown ----------

    def _close(self, *, check: bool) -> None:
        # Never leave a waiter parked: a failing assertion mid-test must not
        # hang the suite on a background thread stuck at the rendezvous.
        self.release()
        for barrier in self._barriers:
            barrier.release_all()
        stuck: list[threading.Thread] = []
        for t in self._threads:
            t.join(self.timeout if check else 1.0)
            if t.is_alive():
                stuck.append(t)
        for q in self._queues:
            q.close()
        if check and stuck:
            raise AssertionError(
                f"{len(stuck)} background thread(s) still alive at context exit"
            )


@contextmanager
def controlled_time(*, timeout: float = DEFAULT_TIMEOUT) -> Iterator[ControlledTime]:
    """The deterministic-time front door (see the module docstring)."""
    t = ControlledTime(timeout=timeout)
    ok = False
    try:
        yield t
        ok = True
    finally:
        # On a test-body failure, close best-effort without masking it.
        t._close(check=ok)
