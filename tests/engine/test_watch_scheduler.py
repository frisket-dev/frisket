"""Scheduled alerts observe current Views regardless of the writer or clock."""

from datetime import UTC, datetime, timedelta
import json

import pytest

from frisket.engine.jobs import SqliteJobQueue, Worker, default_registry
from frisket.engine.jobs.watches import (
    enqueue_due_watch_evaluations,
    register_watch_evaluate_handler,
)
from frisket.engine.store import Project
from tests.watch_helpers import add_watch


@pytest.fixture
def scene(tmp_path):
    p = Project.create(tmp_path / "alerts.frisket")
    sheet = p.add_sheet("Records")
    col = p.add_column(sheet, "text")
    rows = p.add_rows(sheet, [{"text": "old"}], {"text": col})
    watch = add_watch(
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
    register_watch_evaluate_handler(registry, workspace_root=tmp_path, queue=queue)
    yield p, sheet, col, rows[0], watch, queue, Worker(queue, registry)
    queue.close()
    p.close()


def schedule(scene, *, now=None, **kwargs):
    p, _, _, _, _, queue, _ = scene
    return enqueue_due_watch_evaluations(
        workspace_root=p.path.parent, queue=queue, now=now, **kwargs
    )


def run(scene, now=None):
    p, _, _, _, watch, queue, worker = scene
    jobs = schedule(scene, now=now)
    assert len(jobs) == 1
    assert worker.run_once()
    job = queue.get(jobs[0]["job_id"])
    assert job.status == "done", job
    return p.watch_latest_run(watch)


def events(scene, result):
    return [r["event_kind"] for r in scene[0].watch_run_events(result["id"])]


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
    p, _, _, _, watch, queue, _ = scene
    queue.enqueue(
        "watch.evaluate",
        {
            "project_id": "alerts",
            "watch_id": watch,
            "dedupe_key": f"watch:{watch}:eval:run-3:index:next",
        },
    )
    assert schedule(scene) == []
    p.update_watch(watch, schedule=None)
    assert p.get_watch(watch)["schedule"] is None
    p.update_watch(watch, name="Still manual")
    assert p.get_watch(watch)["schedule"] is None
    assert schedule(scene, now=datetime.now(UTC) + timedelta(days=2)) == []
    with pytest.raises(ValueError, match="schedule"):
        p.update_watch(watch, schedule="each second")


def test_deleted_view_cannot_publish_queued_check(scene):
    p, _, _, _, watch, queue, worker = scene
    jobs = schedule(scene)
    p.delete_view(p.get_watch(watch)["view_id"])
    assert worker.run_once()
    assert queue.get(jobs[0]["job_id"]).result["reason"] == "not_found"
    assert schedule(scene) == []


def test_incomplete_check_retains_baseline_and_does_not_hotloop(scene):
    from frisket.engine.store.result_generations import ResultGenerationStore
    from frisket.engine.store.runs import RunResultStore
    from helpers import run_writer_authority_fixture

    p, sheet, col, row, watch, _, _ = scene
    baseline = run(scene)
    op = p.append_op("map.ask", label="pending observation")
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
    assert failed["error_code"] == "watch_inputs_unavailable"
    assert p.get_watch(watch)["baseline_run_id"] == baseline["id"]
    assert (
        p.db.execute(
            "SELECT row_id FROM watch_seen_rows WHERE watch_id=?", (watch,)
        ).fetchone()[0]
        == row
    )
    assert schedule(scene) == []


def test_clock_changes_membership_without_data_write(scene, monkeypatch):
    import frisket.querysets as qs

    p, sheet, _, row, watch, _, _ = scene
    col = p.add_column(sheet, "published", type="date")
    today = datetime.now(UTC)
    p.apply_edits([{"row_id": row, "column_id": col, "value": str(today.date())}])
    view = p.get_watch(watch)["view_id"]
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
        json.loads(p.watch_run_events(later["id"])[0]["after_json"])["reason"]
        == "membership_changed"
    )


def test_queue_prefix_is_literal_and_tenant_scoped(scene):
    _, _, _, _, _, queue, _ = scene
    queue.enqueue(
        "watch.evaluate",
        {
            "project_id": "alerts",
            "storage_org_id": 9,
            "dedupe_key": "watch:1:eval:a_b%:next",
        },
    )
    assert (
        queue.find_job_by_refs(
            "watch.evaluate",
            project_id="alerts",
            storage_org_id=9,
            dedupe_key_prefix="watch:1:eval:a_b%:",
        )
        is not None
    )
    assert (
        queue.find_job_by_refs(
            "watch.evaluate",
            project_id="alerts",
            storage_org_id=8,
            dedupe_key_prefix="watch:1:eval:",
        )
        is None
    )
    assert (
        queue.find_job_by_refs(
            "watch.evaluate",
            project_id="alerts",
            storage_org_id=9,
            dedupe_key_prefix="watch:1:eval:a_b_:",
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
    assert json.loads(p.watch_run_events(departed["id"])[0]["after_json"]) == {
        "matched": False,
        "reason": "row_deleted",
    }
    p.undo()
    assert events(scene, run(scene, now + timedelta(hours=4))) == ["row_entered"]


def test_hosted_scheduler_uses_explicit_project_opener(scene):
    from frisket.cli import _enqueue_due_watches

    p, _, _, _, watch, queue, _ = scene
    opened = []

    def opener(key, path):
        opened.append((key.storage_org_id, key.project_slug, path))
        return Project(path)

    assert (
        _enqueue_due_watches(queue, [(p.path.parent, 84)], project_opener=opener) == 1
    )
    assert opened == [(84, "alerts", p.path)]
    assert (
        queue.find_job_by_refs(
            "watch.evaluate",
            project_id="alerts",
            storage_org_id=84,
            dedupe_key_prefix=f"watch:{watch}:eval:",
        )
        is not None
    )


def test_shared_periodic_loop_runs_checks_and_digests(scene, monkeypatch):
    import frisket.cli as cli
    import threading

    p, _, _, _, _, queue, _ = scene
    calls = []
    stop = threading.Event()
    opener = object()

    def checks(_queue, roots, *, project_opener):
        calls.append(("checks", roots, project_opener))

    def digests(_queue, roots, *, project_opener):
        calls.append(("digests", roots, project_opener))
        stop.set()

    monkeypatch.setattr(cli, "_enqueue_due_watches", checks)
    monkeypatch.setattr(cli, "_enqueue_due_notification_digests", digests)
    roots = [(p.path.parent, 84)]
    thread = cli._start_notification_digest_scheduler(
        queue, roots, 0.01, stop, project_opener=opener
    )
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert calls == [("checks", roots, opener), ("digests", roots, opener)]


def test_failed_job_without_observation_waits_until_next_cadence(scene):
    p, _, _, _, watch, queue, _ = scene
    job_id = queue.enqueue(
        "watch.evaluate",
        {
            "project_id": "alerts",
            "watch_id": watch,
            "dedupe_key": f"watch:{watch}:eval:sched-failed",
        },
    )
    assert queue.fail_queued(job_id, "project could not open")
    assert p.watch_latest_run(watch) is None
    assert schedule(scene) == []
    assert len(schedule(scene, now=datetime.now(UTC) + timedelta(hours=2))) == 1


def test_schedule_queued_before_switch_to_manual_does_not_evaluate(scene):
    p, _, _, _, watch, queue, worker = scene
    jobs = schedule(scene)
    p.update_watch(watch, schedule=None)
    assert worker.run_once()
    assert queue.get(jobs[0]["job_id"]).result["reason"] == "manual_only"
    assert p.watch_latest_run(watch) is None
