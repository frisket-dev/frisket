from __future__ import annotations

from datetime import UTC, datetime

from frisket.engine.jobs.queue import Job
from frisket.server.notifications.candidates import validate_notification_candidate
from frisket.server.notifications.producers import (
    job_transition_notification_candidate,
    run_transition_notification_candidate,
)


def _job(
    *,
    job_id: int = 8,
    kind: str = "source.poll",
    status: str = "failed",
    payload: dict | None = None,
    error: str | None = "worker failed",
) -> Job:
    now = datetime(2026, 6, 19, 12, 0, tzinfo=UTC)
    return Job(
        id=job_id,
        kind=kind,
        payload=payload or {"project_id": "proj", "source_id": 3},
        status=status,
        attempts=1,
        max_attempts=3,
        locked_by=None,
        locked_at=None,
        lease_expires_at=None,
        available_at=now,
        created_at=now,
        started_at=now,
        finished_at=now,
        result=None,
        error=error,
        project_id="proj",
        source_id=3,
        workspace_root="/tmp/frisket/ws",
    )


def test_run_terminal_transitions_emit_valid_candidates() -> None:
    completed = run_transition_notification_candidate(
        project_id="proj",
        run_id=12,
        status="completed",
        action_kind="map.template",
        completed_rows=10,
        failed_rows=0,
    )
    assert completed is not None
    assert validate_notification_candidate(completed) == completed
    assert completed["source_kind"] == "run"
    assert completed["source_ref"] == {
        "project_id": "proj",
        "run_id": 12,
        "status": "completed",
    }
    assert completed["dedupe_key"] == "run:proj:12:completed"
    assert completed["severity"] == "info"
    assert completed["deep_link"] == {"kind": "run", "project_id": "proj", "run_id": 12}
    assert completed["payload"]["action_kind"] == "map.template"

    failed = run_transition_notification_candidate(
        project_id="proj",
        run_id=13,
        status="failed",
        action_kind="map.classify",
        error="provider unavailable",
    )
    assert failed is not None
    assert validate_notification_candidate(failed) == failed
    assert failed["dedupe_key"] == "run:proj:13:failed"
    assert failed["severity"] == "warning"
    assert failed["payload"]["error"] == "provider unavailable"

    cancelled = run_transition_notification_candidate(
        project_id="proj",
        run_id=14,
        status="cancelled",
        action_kind="map.regex_extract",
    )
    assert cancelled is not None
    assert validate_notification_candidate(cancelled) == cancelled
    assert cancelled["dedupe_key"] == "run:proj:14:cancelled"
    assert cancelled["severity"] == "info"


def test_run_attention_states_emit_candidates_but_progress_does_not() -> None:
    stalled = run_transition_notification_candidate(
        project_id="proj",
        run_id=20,
        status="stalled",
        stalled_reason="lease_expired",
    )
    assert stalled is not None
    assert validate_notification_candidate(stalled) == stalled
    assert stalled["dedupe_key"] == "run:proj:20:stalled:lease_expired"
    assert stalled["severity"] == "warning"
    assert stalled["source_ref"]["stalled_reason"] == "lease_expired"

    orphaned = run_transition_notification_candidate(
        project_id="proj",
        run_id=21,
        status="orphaned",
        stalled_reason="queue_job_missing",
    )
    assert orphaned is not None
    assert validate_notification_candidate(orphaned) == orphaned
    assert orphaned["dedupe_key"] == "run:proj:21:orphaned:queue_job_missing"
    assert orphaned["severity"] == "critical"

    needs_user_action = run_transition_notification_candidate(
        project_id="proj",
        run_id=22,
        status="needs_user_action",
        reason="cost_confirmation_required",
    )
    assert needs_user_action is not None
    assert validate_notification_candidate(needs_user_action) == needs_user_action
    assert (
        needs_user_action["dedupe_key"]
        == "run:proj:22:needs_user_action:cost_confirmation_required"
    )
    assert needs_user_action["severity"] == "warning"

    assert (
        run_transition_notification_candidate(
            project_id="proj",
            run_id=23,
            status="running",
            completed_rows=5,
            failed_rows=0,
        )
        is None
    )
    assert (
        run_transition_notification_candidate(
            project_id="proj",
            run_id=24,
            status="queued",
        )
        is None
    )


def test_job_transition_candidates_skip_project_run_completion_duplicates() -> None:
    source_poll_failed = job_transition_notification_candidate(_job())
    assert source_poll_failed is not None
    assert validate_notification_candidate(source_poll_failed) == source_poll_failed
    assert source_poll_failed["source_kind"] == "job"
    assert source_poll_failed["source_ref"]["job_id"] == 8
    assert source_poll_failed["source_ref"]["job_kind"] == "source.poll"
    assert source_poll_failed["dedupe_key"] == (
        "job:/tmp/frisket/ws:8:source.poll:failed"
    )
    assert source_poll_failed["severity"] == "warning"
    assert source_poll_failed["payload"]["error"] == "worker failed"

    stalled = job_transition_notification_candidate(
        _job(status="running", error=None),
        public_status="stalled",
        stalled_reason="lease_expired",
    )
    assert stalled is not None
    assert validate_notification_candidate(stalled) == stalled
    assert (
        stalled["dedupe_key"]
        == "job:/tmp/frisket/ws:8:source.poll:stalled:lease_expired"
    )
    assert stalled["severity"] == "warning"

    assert (
        job_transition_notification_candidate(
            _job(
                kind="project.run",
                status="done",
                payload={"project_id": "proj", "run_id": 9},
            )
        )
        is None
    )
    assert job_transition_notification_candidate(_job(status="queued")) is None
