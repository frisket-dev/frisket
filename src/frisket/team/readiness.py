"""Small, credential-free deployment readiness projection for Server.

Liveness answers whether HTTP is running.  Readiness additionally proves that
the control database and run queue are reachable and that a worker running the
same code has heartbeated recently, even when the queue is empty.
"""

from __future__ import annotations

from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from typing import Any

import sqlalchemy as sa

from frisket import DISTRIBUTION_NAME
from frisket.engine.jobs.projection import parse_status_time
from frisket.engine.worker_version import code_version

READINESS_SCHEMA_VERSION = "frisket.server_readiness.v1"
IDENTITY_SCHEMA_VERSION = "frisket.server_identity.v1"
CONTROL_DATABASE_UNREACHABLE = "control_database_unreachable"
RUN_QUEUE_UNREACHABLE = "run_queue_unreachable"
NO_CURRENT_WORKER = "no_current_worker"
STANDALONE_WORKER_EXITED = "standalone_worker_exited"


def runtime_identity(*, target_code_version: str | None = None) -> dict[str, str]:
    """Return public, credential-free product identity for remote verification.

    Published images bake their full source commit into ``VERSION``, which is
    what ``code_version()`` reads outside a checkout.  Package metadata adds the
    human release version.  Neither field contains a registry credential,
    filesystem path, database locator, or mutable operator configuration.
    """

    try:
        package_version = version(DISTRIBUTION_NAME)
    except PackageNotFoundError:
        package_version = "unknown"
    return {
        "schema_version": IDENTITY_SCHEMA_VERSION,
        "package_version": package_version,
        "code_version": target_code_version or code_version(),
    }


def deployment_readiness(
    *,
    engine: sa.Engine,
    queue: Any,
    runtime_state: Any | None = None,
    target_code_version: str | None = None,
    now: datetime | None = None,
    worker_liveness_seconds: float = 60.0,
) -> dict[str, Any]:
    """Return a stable public report containing machine codes, never locators."""

    failures: list[str] = []
    try:
        with engine.connect() as cx:
            cx.execute(sa.text("SELECT 1"))
    except Exception:  # noqa: BLE001 - readiness exposes only a stable code
        failures.append(CONTROL_DATABASE_UNREACHABLE)

    heartbeats: list[Any] = []
    try:
        # A bounded aggregate also proves that the initialized queue schema is
        # readable. Queue construction already validates/applies its ledger.
        queue.counts()
        heartbeats = list(queue.list_worker_heartbeats())
    except Exception:  # noqa: BLE001 - never leak a DSN/driver message
        failures.append(RUN_QUEUE_UNREACHABLE)

    target = target_code_version or code_version()
    stamp = now or datetime.now(UTC)
    current_worker = False
    for heartbeat in heartbeats:
        if heartbeat.worker_version != target:
            continue
        seen = parse_status_time(heartbeat.last_heartbeat_at)
        if seen is None:
            continue
        age = (stamp - seen).total_seconds()
        if 0.0 <= age <= worker_liveness_seconds:
            current_worker = True
            break
    if RUN_QUEUE_UNREACHABLE not in failures and not current_worker:
        failures.append(NO_CURRENT_WORKER)

    if bool(getattr(runtime_state, "worker_exited_unexpectedly", False)):
        failures.append(STANDALONE_WORKER_EXITED)

    return {
        "schema_version": READINESS_SCHEMA_VERSION,
        "identity": runtime_identity(target_code_version=target),
        "ok": not failures,
        "failures": failures,
    }


__all__ = [
    "CONTROL_DATABASE_UNREACHABLE",
    "IDENTITY_SCHEMA_VERSION",
    "NO_CURRENT_WORKER",
    "READINESS_SCHEMA_VERSION",
    "RUN_QUEUE_UNREACHABLE",
    "STANDALONE_WORKER_EXITED",
    "deployment_readiness",
    "runtime_identity",
]
