"""Queue-health projection (onboard-queue-health-v1).

A single bounded DTO both tiers publish so a queued job stuck behind a dead or
absent worker is *visible* instead of an infinite spinner. The signal that
matters for onboarding: there are queued jobs but no worker has heartbeated
within the liveness window ("queued N minutes with no live worker").
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from frisket.engine.jobs.projection import parse_status_time
from frisket.engine.jobs.queue import MODEL_PULL_KIND, JobQueue

QUEUE_HEALTH_SCHEMA_VERSION = "frisket.queue_health.v1"


def _count_live_model_pull_workers(
    heartbeats, *, now: datetime, liveness_window_seconds: float
) -> int:
    """Count LIVE workers that actually advertise the ``model.pull`` handler
    kind -- as opposed to
    ``model_pull_enabled``, which only says the CAPABILITY is turned on
    somewhere. A worker whose ``kinds`` predates this column (None) never
    counts -- a missing fact is not evidence of capability."""
    count = 0
    for hb in heartbeats:
        seen = parse_status_time(hb.last_heartbeat_at)
        if seen is None:
            continue
        age = (now - seen).total_seconds()
        if age < 0 or age > liveness_window_seconds:
            continue
        kinds = (hb.kinds or "").split(",")
        if MODEL_PULL_KIND in kinds:
            count += 1
    return count


def _oldest_queued_age_seconds(queue: JobQueue, *, now: datetime) -> float | None:
    """Age (seconds) of the longest-waiting queued job, or None when empty."""
    created = parse_status_time(queue.oldest_queued_created_at())
    if created is None:
        return None
    return max(0.0, (now - created).total_seconds())


def queue_health_payload(
    queue: JobQueue,
    *,
    now: datetime,
    liveness_window_seconds: float,
    timeout_seconds: float | None = None,
    model_pull_enabled: bool = False,
) -> dict[str, Any]:
    """Publishable queue/worker health.

    ``no_live_worker`` is the load-bearing flag: queued work exists yet no
    worker heartbeated within ``liveness_window_seconds``. ``stale`` adds the
    dwell condition (oldest queued job older than the window) so a momentary
    gap between workers is not alarmed.

    ``model_pull_enabled`` is the SAME predicate the route checks at enqueue
    time and the
    worker checks at registration -- surfaced here so an enabled API sitting
    in front of a capability-less worker fleet (the flag flipped on for the
    route but not for every worker process) is visible as a fact, not an
    inexplicably stuck job.

    ``handler_authorities.unheard`` is the same idea for the one queue state
    no automatic path can clear: an authority whose worker stopped
    heartbeating. Releasing it needs proof the handler stopped, which only an
    operator has, so this reports the condition rather than acting on it --
    otherwise the first person to notice a permanently "pending" project
    deletion is the user who asked for it.
    """
    live = queue.count_live_workers(within_seconds=liveness_window_seconds, now=now)
    heartbeats = queue.list_worker_heartbeats()
    unheard = queue.unheard_handler_authorities(
        heartbeat_cutoff=now - timedelta(seconds=liveness_window_seconds),
        now=now,
    )
    model_pull_workers = _count_live_model_pull_workers(
        heartbeats, now=now, liveness_window_seconds=liveness_window_seconds
    )
    last_age: float | None = None
    for hb in heartbeats:
        seen = parse_status_time(hb.last_heartbeat_at)
        if seen is None:
            continue
        age = max(0.0, (now - seen).total_seconds())
        if last_age is None or age < last_age:
            last_age = age

    counts = queue.counts()
    queued_count = int(counts.get("queued", 0))
    oldest = _oldest_queued_age_seconds(queue, now=now)
    no_live_worker = queued_count > 0 and live == 0
    stale = no_live_worker and oldest is not None and oldest >= liveness_window_seconds
    timed_out = (
        timeout_seconds is not None
        and timeout_seconds > 0
        and oldest is not None
        and oldest >= timeout_seconds
    )

    return {
        "schema_version": QUEUE_HEALTH_SCHEMA_VERSION,
        "model_pull_enabled": model_pull_enabled,
        "model_pull_workers": model_pull_workers,
        "workers": {
            "live": live,
            "count": len(heartbeats),
            "last_heartbeat_age_seconds": (
                round(last_age, 3) if last_age is not None else None
            ),
            "liveness_window_seconds": liveness_window_seconds,
        },
        "jobs": {status: int(counts.get(status, 0)) for status in counts},
        "handler_authorities": {
            "unheard": unheard.count,
            "oldest_unheard_claim_age_seconds": (
                round(unheard.oldest_claim_age_seconds, 3)
                if unheard.oldest_claim_age_seconds is not None
                else None
            ),
        },
        "queued": {
            "count": queued_count,
            "oldest_age_seconds": round(oldest, 3) if oldest is not None else None,
            "no_live_worker": no_live_worker,
            "stale": stale,
            "timed_out": timed_out,
            "timeout_seconds": timeout_seconds,
        },
    }


# ---------------------------------------------------------------------------
# Internal deploy-readiness predicate
# ---------------------------------------------------------------------------

# Stable machine failure codes. They are the ONLY payload the report exposes for
# a failure; none ever carries a run-queue locator or its embedded credentials.
RUN_QUEUE_UNREACHABLE = "run_queue_unreachable"
APP_WORKER_LOCATOR_MISMATCH = "app_worker_locator_mismatch"
SCHEMA_VERSION_STALE = "schema_version_stale"
NO_POST_CUTOVER_TARGET_WORKER = "no_post_cutover_target_worker"


@dataclass(frozen=True)
class RunQueueDeployReadiness:
    """Fail-closed deploy-readiness report.

    ``ok`` is the go/no-go signal; ``failures`` is a stable, credential-free
    tuple of machine codes. ``repr`` and every field are safe to log — no
    locator or password is ever stored here.
    """

    ok: bool
    failures: tuple[str, ...] = field(default_factory=tuple)


def _locator_fingerprint(locator: str | None) -> str:
    """A non-reversible fingerprint used to compare locators without ever
    printing the locator (which embeds a password)."""
    if locator is None:
        return "absent"
    normalized = locator.strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def evaluate_run_queue_deploy_readiness(
    *,
    queue: JobQueue | None,
    app_run_queue_locator: str | None,
    worker_run_queue_locator: str | None,
    observed_schema_version: int | None,
    target_schema_version: int,
    target_code_version: str,
    cutover_started_at: datetime,
    now: datetime,
    liveness_window_seconds: float = 60.0,
) -> RunQueueDeployReadiness:
    """Internal, fail-closed run-queue deploy-readiness gate (plan section 5.1).

    Proves, before promotion, that: the run queue is reachable; the app and
    worker read one fingerprint-identical locator; the queue schema is current;
    a fresh target-version worker heartbeat exists AFTER cutover start even when
    the queue is empty. Every failure is a stable machine code; the report
    never contains a locator or password.
    """
    failures: list[str] = []

    # Locator parity (fingerprinted; the raw locators are never stored/printed).
    if _locator_fingerprint(app_run_queue_locator) != _locator_fingerprint(
        worker_run_queue_locator
    ):
        failures.append(APP_WORKER_LOCATOR_MISMATCH)

    # Schema currency: a ledger behind the target blocks readiness.
    if (
        observed_schema_version is None
        or observed_schema_version < target_schema_version
    ):
        failures.append(SCHEMA_VERSION_STALE)

    if queue is None:
        # A run queue the gate cannot reach fails closed; heartbeat/claim
        # derivations are unavailable, so the deploy is not ready.
        failures.append(RUN_QUEUE_UNREACHABLE)
        return RunQueueDeployReadiness(ok=False, failures=tuple(failures))

    # A fresh target-version heartbeat observed AFTER cutover start is required
    # even when the queue is empty (an empty queue must not auto-pass).
    if not _has_fresh_post_cutover_target_worker(
        queue,
        target_code_version=target_code_version,
        cutover_started_at=cutover_started_at,
        now=now,
        liveness_window_seconds=liveness_window_seconds,
    ):
        failures.append(NO_POST_CUTOVER_TARGET_WORKER)

    return RunQueueDeployReadiness(ok=not failures, failures=tuple(failures))


def _has_fresh_post_cutover_target_worker(
    queue: JobQueue,
    *,
    target_code_version: str,
    cutover_started_at: datetime,
    now: datetime,
    liveness_window_seconds: float,
) -> bool:
    cutover = parse_status_time(cutover_started_at)
    for heartbeat in queue.list_worker_heartbeats():
        if heartbeat.worker_version != target_code_version:
            continue
        seen = parse_status_time(heartbeat.last_heartbeat_at)
        if seen is None:
            continue
        if cutover is not None and seen <= cutover:
            continue  # only a heartbeat strictly after cutover proves new code ran
        age = (now - seen).total_seconds()
        if 0.0 <= age <= liveness_window_seconds:
            return True
    return False
