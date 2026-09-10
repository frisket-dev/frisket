from __future__ import annotations

from collections.abc import Callable, MutableMapping
from dataclasses import dataclass, field
from typing import Any

from frisket.engine.jobs.queue_health import (
    NO_POST_CUTOVER_TARGET_WORKER,
    SCHEMA_VERSION_STALE,
)

# Net-new alert codes owned by this monitor (no queue_health equivalent
# exists yet). Stable, credential-free machine codes -- never a run-queue
# locator or password.
OLDEST_QUEUED_AGE = "oldest_queued_age"
STALE_CLAIM = "stale_claim"


class RunQueueMonitorError(Exception):
    """Raised for a misconfigured monitor call (fail loud, never silent).

    Currently raised when ``alert_sink`` is not callable.
    """


@dataclass(frozen=True)
class RunQueueMonitorEvent:
    """One emitted alert-sink event.

    ``kind`` is ``"failure"`` or ``"recovery"``; ``code`` is one of the
    stable machine codes above. Never carries a locator or credential.
    """

    kind: str
    code: str


@dataclass(frozen=True)
class RunQueueMonitorReport:
    """Result of one monitor tick.

    ``ok`` is the go/no-go signal for this tick; ``failures`` is the stable
    machine codes currently active (regardless of whether they were already
    alerted on a prior tick); ``events`` is what THIS tick emitted to the
    alert sink (empty when a persistent condition is deduplicated).
    """

    ok: bool
    failures: tuple[str, ...] = field(default_factory=tuple)
    events: tuple[RunQueueMonitorEvent, ...] = field(default_factory=tuple)


# Per-alert-sink dedup memory, scoped to the sink's owning object's identity.
# Keying on the sink rather than a bare process-global lets independent
# monitors (e.g. one per test, or one per environment) run in the same
# process without bleeding "already alerted" state into each other, while a
# real production sink -- a single long-lived callable reused every tick --
# gets exactly the persistent memory the dedup contract requires.
#
# A bound method such as ``alert_sink.append`` is a fresh wrapper object on
# every access (``sink.append is sink.append`` is False), so it cannot be
# used as a dict key across calls directly and it is not weak-referenceable
# in a way that survives that per-access churn. Instead this keys on
# ``id(owner)`` where ``owner`` is the bound method's ``__self__`` (the
# actual persistent object, e.g. the list backing ``sink.append``) and pins a
# strong reference to ``owner`` alongside its state so ``id()`` can never be
# recycled out from under a live entry. This intentionally never evicts --
# real callers pass a small, bounded number of distinct alert sinks over a
# process lifetime, so the permanent pin is cheap and, unlike an unpinned
# `id()` map or a `WeakKeyDictionary` over the ephemeral wrapper, never
# silently reuses another object's stale dedup memory.
_DEFAULT_STATE_BY_OWNER_ID: dict[int, tuple[Any, dict[str, bool]]] = {}


def _default_state_for(
    alert_sink: Callable[[RunQueueMonitorEvent], None],
) -> dict[str, bool]:
    owner = getattr(alert_sink, "__self__", alert_sink)
    key = id(owner)
    entry = _DEFAULT_STATE_BY_OWNER_ID.get(key)
    if entry is not None and entry[0] is owner:
        return entry[1]
    state: dict[str, bool] = {}
    _DEFAULT_STATE_BY_OWNER_ID[key] = (owner, state)
    return state


def evaluate_run_queue_runtime_monitor(
    *,
    alert_sink: Callable[[RunQueueMonitorEvent], None],
    empty_queue_worker_lost: bool = False,
    backlog_over_threshold: bool = False,
    schema_drifted: bool = False,
    stale_claim_present: bool = False,
    state: MutableMapping[str, bool] | None = None,
) -> RunQueueMonitorReport:
    """Evaluate one bounded monitor tick and emit deduplicated alert events.

    Each of the four conditions maps to one stable code:

    - ``empty_queue_worker_lost`` -> ``NO_POST_CUTOVER_TARGET_WORKER`` (fires
      even when the queue is empty -- an empty queue must not auto-pass);
    - ``schema_drifted`` -> ``SCHEMA_VERSION_STALE``;
    - ``backlog_over_threshold`` -> ``OLDEST_QUEUED_AGE``;
    - ``stale_claim_present`` -> ``STALE_CLAIM``.

    A condition emits a ``"failure"`` event only on the tick it first becomes
    active (dedup: a persistent failure alerts once, not every tick) and a
    single ``"recovery"`` event on the tick it clears. ``state`` is the dedup
    memory across ticks; when omitted, memory is kept per-``alert_sink`` (see
    ``_DEFAULT_STATE_BY_SINK``) so independent callers never share it.
    """
    if not callable(alert_sink):
        raise RunQueueMonitorError("alert_sink must be callable")
    condition_state = state if state is not None else _default_state_for(alert_sink)

    conditions = (
        (NO_POST_CUTOVER_TARGET_WORKER, empty_queue_worker_lost),
        (SCHEMA_VERSION_STALE, schema_drifted),
        (OLDEST_QUEUED_AGE, backlog_over_threshold),
        (STALE_CLAIM, stale_claim_present),
    )

    events: list[RunQueueMonitorEvent] = []
    failures: list[str] = []
    for code, active in conditions:
        was_active = condition_state.get(code, False)
        if active:
            failures.append(code)
            if not was_active:
                event = RunQueueMonitorEvent(kind="failure", code=code)
                events.append(event)
                alert_sink(event)
        elif was_active:
            event = RunQueueMonitorEvent(kind="recovery", code=code)
            events.append(event)
            alert_sink(event)
        condition_state[code] = active

    return RunQueueMonitorReport(
        ok=not failures, failures=tuple(failures), events=tuple(events)
    )
