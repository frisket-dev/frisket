from __future__ import annotations

import importlib
from collections.abc import Callable
from dataclasses import dataclass
from importlib.util import find_spec
from typing import Any

import pytest

# Grounded, already-shipped referents: the deploy-readiness predicate the
# runtime monitor must reuse, and its stable credential-free failure codes.
from frisket.engine.jobs.queue_health import (
    NO_POST_CUTOVER_TARGET_WORKER,
    SCHEMA_VERSION_STALE,
    evaluate_run_queue_deploy_readiness,
)

_MODULE = "frisket.engine.jobs.queue_runtime_monitor"

assert callable(evaluate_run_queue_deploy_readiness)  # anchor the shared predicate


@dataclass(frozen=True)
class _RuntimeMonitorApi:
    monitor_error: type[Exception]
    monitor_report: type[Any]
    evaluate: Callable[..., Any]


def _module_present(name: str) -> bool:
    """True iff ``name`` is importable. Catches a missing-parent-package
    ``ModuleNotFoundError`` from ``find_spec`` so the red stays a clean
    ``AssertionError`` and is never misread as a collection/import error."""
    try:
        return find_spec(name) is not None
    except ModuleNotFoundError:
        return False


def _runtime_monitor_api() -> _RuntimeMonitorApi:
    assert _module_present(_MODULE), (
        f"missing {_MODULE}: bounded scheduled run-queue runtime monitor "
        "(follow-on plan sections 2.5 and 5.1) has no product mechanism yet"
    )
    module = importlib.import_module(_MODULE)
    names = {
        "monitor_error": "RunQueueMonitorError",
        "monitor_report": "RunQueueMonitorReport",
    }
    absent = [export for export in names.values() if not hasattr(module, export)]
    assert not absent, f"{_MODULE} lacks exports: {absent}"
    assert hasattr(module, "evaluate_run_queue_runtime_monitor"), (
        f"{_MODULE} lacks evaluate_run_queue_runtime_monitor(...)"
    )
    return _RuntimeMonitorApi(
        monitor_error=getattr(module, names["monitor_error"]),
        monitor_report=getattr(module, names["monitor_report"]),
        evaluate=module.evaluate_run_queue_runtime_monitor,
    )


@pytest.mark.gap
def test_monitor_alerts_on_no_target_worker_backlog_schema_and_old_claim() -> None:
    """The scheduled monitor alerts on the full post-promotion predicate.

    Contract (plan section 5.1): on a bounded schedule the monitor evaluates
    the same version/worker/schema/backlog predicate and emits failure events
    for no compatible worker (even when the queue is EMPTY), oldest queued age
    over threshold, stale claims, and schema drift -- each as a stable machine
    code, never a run-queue locator or credential.
    """
    api = _runtime_monitor_api()
    sink: list[Any] = []
    report = api.evaluate(
        alert_sink=sink.append,
        empty_queue_worker_lost=True,
        backlog_over_threshold=True,
        schema_drifted=True,
        stale_claim_present=True,
    )
    emitted = {getattr(event, "code", event) for event in sink}
    assert NO_POST_CUTOVER_TARGET_WORKER in emitted, (
        "empty-queue worker loss must still raise the no-target-worker alert"
    )
    assert SCHEMA_VERSION_STALE in emitted, "schema drift must raise a schema alert"
    assert report.ok is False, "a failing predicate must make the monitor report not-ok"
    assert {"oldest_queued_age", "stale_claim"} <= {
        getattr(event, "code", event) for event in sink
    }, "backlog age and stale-claim conditions must each raise their own alert"


@pytest.mark.gap
def test_monitor_deduplicates_and_emits_recovery_without_public_detail_leak() -> None:
    """Repeated failures deduplicate; recovery emits once; health stays sanitized.

    Contract (plan section 5.1): the monitor emits DEDUPLICATED failure events
    (a persistent condition alerts once, not every tick) and a single recovery
    event when the condition clears, and it never leaks internal detail into
    the sanitized public health surface.
    """
    api = _runtime_monitor_api()
    sink: list[Any] = []
    for _ in range(3):
        api.evaluate(alert_sink=sink.append, empty_queue_worker_lost=True)
    failure_events = [e for e in sink if getattr(e, "kind", None) == "failure"]
    assert len(failure_events) == 1, (
        "a persistent failure condition must deduplicate to a single alert"
    )
    recovery_report = api.evaluate(
        alert_sink=sink.append, empty_queue_worker_lost=False
    )
    recovery_events = [e for e in sink if getattr(e, "kind", None) == "recovery"]
    assert len(recovery_events) == 1, (
        "clearing the condition must emit one recovery event"
    )
    rendered = " ".join(repr(e) for e in sink) + repr(recovery_report)
    for secret_marker in ("postgres://", "postgresql://", "password="):
        assert secret_marker not in rendered, (
            "monitor events/report must never carry a locator or credential"
        )
