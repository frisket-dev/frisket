"""Scheduled alerts observe current Views regardless of the writer or clock."""

from datetime import UTC, datetime, timedelta
import json

import pytest

from frisket.engine.jobs import SqliteJobQueue, Worker, default_registry
from frisket.engine.jobs.alerts import (
    enqueue_due_alert_evaluations,
    register_alert_evaluate_handler,
)
from frisket.engine.store import Project
from tests.alert_helpers import add_alert


@pytest.fixture
def scene(tmp_path, make_project):
    p = make_project("alerts")
    sheet = p.add_sheet("Records")
    col = p.add_column(sheet, "text")
    rows = p.add_rows(sheet, [{"text": "old"}], {"text": col})
    alert = add_alert(
        p,
        "Monitor",
        sheet_id=sheet,
        query={"kind": "filter", "sheet_id": sheet, "filter": {}},
        detection_policy={
            "kind": "conditions",
            "entered": True,
            "exited": True,
            "fields": [{"sheet_id": sheet, "column_id": col}],
        },
    )
    queue = SqliteJobQueue(tmp_path / ".jobs.db")
    registry = default_registry()
    register_alert_evaluate_handler(registry, workspace_root=tmp_path, queue=queue)
    yield p, sheet, col, rows[0], alert, queue, Worker(queue, registry)
    queue.close()


def schedule(scene, *, now=None, **kwargs):
    p, _, _, _, _, queue, _ = scene
    return enqueue_due_alert_evaluations(
        workspace_root=p.path.parent, queue=queue, now=now, **kwargs
    )


def run(scene, now=None):
    p, _, _, _, alert, queue, worker = scene
    jobs = schedule(scene, now=now)
    assert len(jobs) == 1
    assert worker.run_once()
    job = queue.get(jobs[0]["job_id"])
    assert job.status == "done", job
    return p.alert_latest_run(alert)


def events(scene, result):
    return [r["event_kind"] for r in scene[0].alert_check_events(result["id"])]


def test_scheduled_checks_observe_edits_and_undo(scene):
    p, _, col, row, _, _, _ = scene
    now = datetime.now(UTC)
    assert events(scene, run(scene, now)) == []
    p.apply_edits([{"row_id": row, "column_id": col, "value": "new"}])
    assert schedule(scene, now=now + timedelta(minutes=30)) == []
    assert events(scene, run(scene, now + timedelta(hours=2))) == ["row_changed"]
    p.undo()
    assert events(scene, run(scene, now + timedelta(hours=4))) == ["row_changed"]


def test_pending_event_continuation_dedupes_and_manual_disables(scene):
    p, _, _, _, alert, queue, _ = scene
    queue.enqueue(
        "alert.evaluate",
        {
            "project_id": "alerts",
            "alert_id": alert,
            "dedupe_key": f"alert:{alert}:eval:run-3:index:next",
        },
    )
    assert schedule(scene) == []
    p.update_alert(alert, schedule=None)
    assert p.get_alert(alert)["schedule"] is None
    p.update_alert(alert, name="Still manual")
    assert p.get_alert(alert)["schedule"] is None
    assert schedule(scene, now=datetime.now(UTC) + timedelta(days=2)) == []
    with pytest.raises(ValueError, match="schedule"):
        p.update_alert(alert, schedule="each second")


def test_deleted_view_cannot_publish_queued_check(scene):
    p, _, _, _, alert, queue, worker = scene
    jobs = schedule(scene)
    p.delete_view(p.get_alert(alert)["view_id"])
    assert worker.run_once()
    assert queue.get(jobs[0]["job_id"]).result["reason"] == "not_found"
    assert schedule(scene) == []


def test_incomplete_check_retains_baseline_and_does_not_hotloop(scene):
    from frisket.engine.store.result_generations import ResultGenerationStore
    from frisket.engine.store.runs import RunResultStore
    from helpers import run_writer_authority_fixture

    p, sheet, col, row, alert, _, _ = scene
    baseline = run(scene)
    op = p.append_change("map.ask", label="pending observation")
    run_id = RunResultStore(p).start_run(
        op, sheet, "map.ask", row_ids=[row], total_rows=1
    )
    authority = run_writer_authority_fixture(p, run_id, output_column_ids={col})
    ResultGenerationStore(p).declare(
        run_id,
        col,
        output_role="answer",
        compatibility_key="sha256:test",
        write_mode="replace_scope",
        claim_token=authority.claim_token,
    )
    failed = run(scene, datetime.now(UTC) + timedelta(hours=2))
    assert failed["status"] == "error"
    assert failed["error_code"] == "alert_inputs_unavailable"
    assert p.get_alert(alert)["baseline_run_id"] == baseline["id"]
    assert (
        p.db.execute(
            "SELECT row_id FROM alert_seen_rows WHERE alert_id=?", (alert,)
        ).fetchone()[0]
        == row
    )
    assert schedule(scene) == []


def test_clock_changes_membership_without_data_write(scene, monkeypatch):
    import frisket.querysets as qs

    p, sheet, _, row, alert, _, _ = scene
    col = p.add_column(sheet, "published", type="date")
    today = datetime.now(UTC)
    p.apply_edits([{"row_id": row, "column_id": col, "value": str(today.date())}])
    view = p.get_alert(alert)["view_id"]
    from frisket.engine.views.specs import legacy_selection

    selection = legacy_selection(
        p,
        sheet_id=sheet,
        spec={
            "kind": "filter",
            "sheet_id": sheet,
            "filter": {"published": {"date_relative": {"amount": 1, "unit": "days"}}},
        },
    )
    p.update_view(view, spec={"selection": selection, "presentation": {}})
    first = run(scene, today)
    assert first["matched_rows"] == 1
    cursor = p.op_cursor

    class Tomorrow(datetime):
        @classmethod
        def now(cls, tz=None):
            return today + timedelta(days=1)

    monkeypatch.setattr(qs, "datetime", Tomorrow)
    later = run(scene, today + timedelta(days=1))
    assert p.op_cursor == cursor
    assert events(scene, later) == ["row_exited"]
    assert (
        json.loads(p.alert_check_events(later["id"])[0]["after_json"])["reason"]
        == "membership_changed"
    )


def test_queue_prefix_is_literal_and_organization_scoped(scene):
    _, _, _, _, _, queue, _ = scene
    queue.enqueue(
        "alert.evaluate",
        {
            "project_id": "alerts",
            "storage_org_id": 9,
            "dedupe_key": "alert:1:eval:a_b%:next",
        },
    )
    assert (
        queue.find_job_by_refs(
            "alert.evaluate",
            project_id="alerts",
            storage_org_id=9,
            dedupe_key_prefix="alert:1:eval:a_b%:",
        )
        is not None
    )
    assert (
        queue.find_job_by_refs(
            "alert.evaluate",
            project_id="alerts",
            storage_org_id=8,
            dedupe_key_prefix="alert:1:eval:",
        )
        is None
    )
    assert (
        queue.find_job_by_refs(
            "alert.evaluate",
            project_id="alerts",
            storage_org_id=9,
            dedupe_key_prefix="alert:1:eval:a_b_:",
        )
        is None
    )


def test_exit_records_deletion_and_restoration_is_a_new_entry(scene):
    from frisket.engine.executor import run_action_spec

    p, sheet, _, row, _, _, _ = scene
    now = datetime.now(UTC)
    run(scene, now)
    result = run_action_spec(
        p,
        {
            "action_id": "row.delete",
            "scope": {"kind": "project"},
            "params": {"sheet_id": sheet, "row_ids": [row]},
            "idempotency_key": "delete@sha256:test",
        },
        project_id="alerts",
    )
    assert result.status == "completed"
    departed = run(scene, now + timedelta(hours=2))
    assert events(scene, departed) == ["row_exited"]
    assert json.loads(p.alert_check_events(departed["id"])[0]["after_json"]) == {
        "matched": False,
        "reason": "row_deleted",
    }
    p.undo()
    assert events(scene, run(scene, now + timedelta(hours=4))) == ["row_entered"]


def test_hosted_scheduler_uses_explicit_project_opener(scene):
    import frisket.cli as cli

    p, _, _, _, alert, queue, _ = scene
    opened = []

    def opener(key, path):
        opened.append((key.storage_org_id, key.project_slug, path))
        return Project(path)

    assert (
        cli._sweep(cli._ALERTS, queue, [(p.path.parent, 84)], project_opener=opener)
        == 1
    )
    assert opened == [(84, "alerts", p.path)]
    assert (
        queue.find_job_by_refs(
            "alert.evaluate",
            project_id="alerts",
            storage_org_id=84,
            dedupe_key_prefix=f"alert:{alert}:eval:",
        )
        is not None
    )


def test_shared_periodic_loop_runs_checks_and_digests():
    import frisket.cli as cli
    import threading

    [scheduler] = [
        s
        for s in cli._SCHEDULERS
        if s.interval_option == "schedule_notification_digests_interval"
    ]
    assert scheduler.sweeps == (cli._ALERTS, cli._NOTIFICATION_DIGESTS)
    ticks = []
    stop = threading.Event()

    def tick():
        ticks.append(threading.current_thread().name)
        stop.set()

    thread = cli._start_scheduler(scheduler.thread_name, tick, 0.01, stop)
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert ticks == ["notification-digest-scheduler"]


def test_failed_job_without_observation_waits_until_next_cadence(scene):
    p, _, _, _, alert, queue, _ = scene
    job_id = queue.enqueue(
        "alert.evaluate",
        {
            "project_id": "alerts",
            "alert_id": alert,
            "dedupe_key": f"alert:{alert}:eval:sched-failed",
        },
    )
    assert queue.fail_queued(job_id, "project could not open")
    assert p.alert_latest_run(alert) is None
    assert schedule(scene) == []
    assert len(schedule(scene, now=datetime.now(UTC) + timedelta(hours=2))) == 1


def test_schedule_queued_before_switch_to_manual_does_not_evaluate(scene):
    p, _, _, _, alert, queue, worker = scene
    jobs = schedule(scene)
    p.update_alert(alert, schedule=None)
    assert worker.run_once()
    assert queue.get(jobs[0]["job_id"]).result["reason"] == "manual_only"
    assert p.alert_latest_run(alert) is None


def test_deleted_view_and_alert_ids_never_retarget_old_work(scene):
    p, sheet, _, _, old_alert, queue, worker = scene
    old_view = p.get_alert(old_alert)["view_id"]
    admitted = schedule(scene)
    p.delete_view(old_view)
    replacement = add_alert(
        p,
        "Replacement",
        sheet_id=sheet,
        query={"kind": "filter", "sheet_id": sheet, "filter": {}},
    )
    assert replacement > old_alert
    assert p.get_alert(replacement)["view_id"] > old_view
    assert p.get_view(old_view) is None
    # A stale View command cannot edit the replacement, either.
    p.update_view(old_view, name="Stale rename")
    assert p.get_view(p.get_alert(replacement)["view_id"])["name"] == "Replacement"
    assert worker.run_once()
    assert queue.get(admitted[0]["job_id"]).result["reason"] == "not_found"
    assert p.alert_latest_run(replacement) is None


@pytest.mark.parametrize("scoped", [False, True])
@pytest.mark.parametrize("failure", ["open", "scan"])
def test_one_broken_project_does_not_stop_other_scheduled_alerts(
    scene, monkeypatch, caplog, scoped, failure
):
    import sqlite3
    from frisket.engine.jobs import alerts
    from frisket.project_identity import ProjectStorageKey

    p, _, _, _, alert, queue, _ = scene
    broken_path = p.path.parent / "a-broken.frisket"
    Project.create(broken_path).close()
    closed = []
    opened = []

    def open_project(path):
        opened.append(path.stem)
        if path == broken_path and failure == "open":
            raise sqlite3.OperationalError("database is locked")
        project = Project(path)
        if path == broken_path:

            def fail_scan():
                raise ValueError("unreadable watch metadata")

            project.alerts = fail_scan
            close = project.close

            def tracked_close():
                closed.append(path.stem)
                close()

            project.close = tracked_close
        return project

    def scoped_opener(key, path):
        assert key == ProjectStorageKey(23, path.stem)
        return open_project(path)

    monkeypatch.setattr(alerts, "Project", open_project)
    result = enqueue_due_alert_evaluations(
        workspace_root=p.path.parent,
        queue=queue,
        storage_org_id=23 if scoped else None,
        project_opener=scoped_opener if scoped else None,
    )
    assert opened == ["a-broken", "alerts"]
    assert len(result) == 1 and result[0]["alert_id"] == alert
    assert queue.get(result[0]["job_id"]).status == "queued"
    errors = [
        record
        for record in caplog.records
        if record.message == "alert_project_schedule_failed"
    ]
    assert len(errors) == 1 and errors[0].project_id == "a-broken"
    assert errors[0].exc_info is not None
    assert closed == (["a-broken"] if failure == "scan" else [])
