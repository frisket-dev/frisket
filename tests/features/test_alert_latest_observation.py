from tests.alert_helpers import add_alert
import json

import pytest

from frisket.notifications.projections import alert_check_notification_candidate


@pytest.fixture
def scene(make_project):
    project = make_project("alerts")
    sheet = project.add_sheet("records")
    column = project.add_column(sheet, "value")
    rows = project.add_rows(
        sheet, [{"value": "old"} for _ in range(3)], {"value": column}
    )
    alert = add_alert(
        project,
        "Monitor",
        scope="sheet",
        sheet_id=sheet,
        query={"kind": "filter", "sheet_id": sheet, "filter": {}},
        detection_policy={
            "kind": "conditions",
            "entered": True,
            "exited": True,
            "fields": [{"sheet_id": sheet, "column_id": column}],
        },
    )
    yield project, sheet, column, rows, alert


def record(scene, rows, *, status="ok", query_hash="scope-1", hits=None):
    project, sheet, _, _, alert = scene
    return project.record_alert_check(
        alert,
        hits=hits
        if hits is not None
        else ({"sheet_id": sheet, "row_id": row, "snippet": "match"} for row in rows),
        status=status,
        op_cursor_before=project.op_cursor,
        op_cursor_after=project.op_cursor,
        resolved_query_hash=query_hash,
    )


def kinds(project, run):
    return [row["event_kind"] for row in project.alert_check_events(run)]


def test_first_observation_silent_and_returning_rows_alert_again(scene):
    project, _, _, rows, alert = scene
    first = record(scene, rows[:1])
    assert kinds(project, first) == []
    assert project.get_alert_check(first)["matched_rows"] == 1
    assert project.get_alert_check(first)["new_rows"] == 0
    assert (
        alert_check_notification_candidate(project, alert_id=alert, run_id=first)
        is None
    )
    assert kinds(project, record(scene, [])) == ["row_exited"]
    assert kinds(project, record(scene, rows[:1])) == ["row_entered"]
    assert kinds(project, record(scene, rows[:1])) == []
    assert project.db.execute("SELECT count(*) FROM watch_seen_rows").fetchone()[0] == 1


def test_conditions_share_one_check_and_incomplete_preserves_baseline(scene):
    project, _, column, rows, alert = scene
    record(scene, rows[:2])
    project.apply_edits([{"row_id": rows[0], "column_id": column, "value": "updated"}])
    failed = record(scene, [], status="error")
    assert kinds(project, failed) == []
    assert project.db.execute("SELECT count(*) FROM watch_seen_rows").fetchone()[0] == 2
    changed = record(scene, [rows[0], rows[2]])
    assert kinds(project, changed) == ["row_entered", "row_exited", "row_changed"]
    event = project.alert_check_events(changed)[-1]
    assert "old" in event["before_json"]
    assert "updated" in event["after_json"]
    assert project.get_alert(alert)["last_run_id"] == changed


def test_criteria_change_silently_replaces_baseline(scene):
    project, _, _, rows, _ = scene
    record(scene, rows[:1])
    changed = record(scene, rows[1:], query_hash="scope-2")
    assert kinds(project, changed) == []
    assert kinds(project, record(scene, rows, query_hash="scope-2")) == ["row_entered"]


def test_definition_change_during_staging_cannot_publish_old_check(scene):
    project, sheet, _, rows, alert = scene
    record(scene, rows[:1])
    before = project.get_alert(alert)["last_run_id"]

    def arriving_hits():
        yield {"sheet_id": sheet, "row_id": rows[0]}
        project.db.execute(
            "UPDATE views SET spec=? WHERE id=(SELECT view_id FROM watches WHERE id=?)",
            (
                json.dumps(
                    {
                        "selection": {
                            "scope": {"kind": "sheet", "sheet_id": sheet},
                            "filters": [],
                        },
                        "presentation": {},
                    }
                ),
                alert,
            ),
        )
        project.db.commit()

    with pytest.raises(ValueError, match="definition changed"):
        record(scene, [], hits=arriving_hits())
    assert project.get_alert(alert)["last_run_id"] == before
    assert project.db.execute("SELECT count(*) FROM watch_runs").fetchone()[0] == 1


def test_all_changes_counted_with_bounded_previews_and_one_summary_key(scene):
    project, sheet, column, _, alert = scene
    record(scene, [])
    rows = project.add_rows(
        sheet, [{"value": "extra"} for _ in range(601)], {"value": column}
    )
    run = record(scene, rows)
    assert project.get_alert_check(run)["matched_rows"] == 601
    assert project.alert_check_events_total(run) == 601
    assert len(project.alert_check_matches(run, 1000)) == 500
    small = alert_check_notification_candidate(
        project, alert_id=alert, run_id=run, limit=5
    )
    large = alert_check_notification_candidate(
        project, alert_id=alert, run_id=run, limit=500
    )
    assert "601" in small["title"]
    assert small["payload"]["event_count"] == 601
    assert small["dedupe_key"] == large["dedupe_key"]
    assert len(small["source_event_ids"]) == 5


def test_new_condition_initializes_without_swallowing_existing_entry_alerts(scene):
    project, _, _, rows, alert = scene
    first = record(scene, rows[:1])
    project.update_alert(alert, name="Renamed")
    assert kinds(project, record(scene, rows[:2])) == ["row_entered"]
    project.db.execute(
        "UPDATE watches SET detection_policy=? WHERE id=?",
        (
            json.dumps({"kind": "conditions", "entered": True, "count_changed": True}),
            alert,
        ),
    )
    project.db.commit()
    new_baseline = record(scene, rows)
    assert kinds(project, new_baseline) == ["row_entered"]
    assert project.get_alert(alert)["baseline_run_id"] == new_baseline
    assert project.get_alert_check(first)["detection_policy"] is not None


def test_pending_monitored_value_preserves_successful_baseline(scene):
    from frisket.engine.store.result_generations import ResultGenerationStore
    from frisket.engine.store.runs import RunResultStore
    from helpers import run_writer_authority_fixture

    project, sheet, column, rows, alert = scene
    baseline = record(scene, rows)
    op = project.append_change("map.ask", label="pending observation")
    run = RunResultStore(project).start_run(
        op, sheet, "map.ask", row_ids=rows, total_rows=len(rows)
    )
    authority = run_writer_authority_fixture(project, run, output_column_ids={column})
    ResultGenerationStore(project).declare(
        run,
        column,
        output_role="answer",
        compatibility_key="sha256:test",
        write_mode="replace_scope",
        claim_token=authority.claim_token,
    )
    incomplete = record(scene, rows)
    assert project.get_alert_check(incomplete)["status"] == "error"
    assert (
        project.get_alert_check(incomplete)["error_code"] == "watch_inputs_unavailable"
    )
    assert project.get_alert(alert)["baseline_run_id"] == baseline
    assert project.db.execute("SELECT count(*) FROM watch_seen_rows").fetchone()[0] == 3


def test_saved_observation_notification_recovers_without_requery(scene, monkeypatch):
    from frisket.engine.jobs import open_queue
    from frisket.engine.jobs.notifications_delivery import (
        reconcile_notification_delivery_requests,
    )
    from frisket.notifications.projections import emit_alert_check_notification

    project, _, _, rows, alert = scene
    record(scene, [])
    run = record(scene, rows)
    assert project.get_alert_check(run)["notification_pending"] == 1
    # Crash after baseline publication, before projecting any notification.
    queue = open_queue(workspace=project.path.parent / "queue")
    monkeypatch.setattr(
        "frisket.alerts.service.evaluate_alert",
        lambda *args: pytest.fail("recovery must not query live data"),
    )
    reconcile_notification_delivery_requests(queue, workspace_root=project.path.parent)
    assert project.get_alert_check(run)["notification_pending"] == 0
    assert (
        project.db.execute(
            "SELECT count(*) FROM notification_items WHERE source_kind='watch'"
        ).fetchone()[0]
        == 1
    )
    emit_alert_check_notification(project, alert_id=alert, run_id=run)
    assert (
        project.db.execute(
            "SELECT count(*) FROM notification_items WHERE source_kind='watch'"
        ).fetchone()[0]
        == 1
    )


def test_notification_persists_total_not_preview_count(scene):
    from frisket.notifications.projections import emit_alert_check_notification

    project, sheet, column, _, alert = scene
    record(scene, [])
    rows = project.add_rows(
        sheet, [{"value": "extra"} for _ in range(501)], {"value": column}
    )
    run = record(scene, rows)
    emit_alert_check_notification(project, alert_id=alert, run_id=run)
    notification = project.db.execute(
        "SELECT event_count FROM notification_items WHERE source_kind='watch'"
    ).fetchone()
    assert notification[0] == 501


def test_worker_recovers_saved_notification_without_an_expired_lease(scene):
    from frisket.engine.jobs import (
        HandlerRegistry,
        Worker,
        open_queue,
        register_notification_handlers,
    )

    project, _, _, rows, _ = scene
    record(scene, [])
    run = record(scene, rows)
    queue = open_queue(workspace=project.path.parent / "queue")
    registry = HandlerRegistry()
    register_notification_handlers(registry, workspace_root=project.path.parent)
    # No queued/leased jobs exist: recovery must run from the real idle worker.
    worker = Worker(queue, registry, worker_id="notification-restart")
    assert worker.run_once() is False
    assert project.get_alert_check(run)["notification_pending"] == 0
    assert (
        project.db.execute(
            "SELECT count(*) FROM notification_items WHERE source_kind='watch'"
        ).fetchone()[0]
        == 1
    )
    assert worker.run_once() is False
    assert (
        project.db.execute(
            "SELECT count(*) FROM notification_items WHERE source_kind='watch'"
        ).fetchone()[0]
        == 1
    )


def test_failed_notification_does_not_block_later_checks(scene, monkeypatch):
    from frisket.engine.jobs import open_queue
    from frisket.engine.jobs.notifications_delivery import (
        reconcile_notification_delivery_requests,
    )
    from frisket.notifications import projections

    project, _, _, rows, _ = scene
    record(scene, [])
    first = record(scene, rows[:1])
    second = record(scene, rows)
    original = projections.emit_alert_check_notification

    def fail_first(project, *, alert_id, run_id):
        if run_id == first:
            raise RuntimeError("transient projection failure")
        return original(project, alert_id=alert_id, run_id=run_id)

    monkeypatch.setattr(projections, "emit_alert_check_notification", fail_first)
    queue = open_queue(workspace=project.path.parent / "queue")
    reconcile_notification_delivery_requests(queue, workspace_root=project.path.parent)
    assert project.get_alert_check(first)["notification_pending"] == 1
    assert project.get_alert_check(second)["notification_pending"] == 0
    monkeypatch.setattr(projections, "emit_alert_check_notification", original)
    reconcile_notification_delivery_requests(queue, workspace_root=project.path.parent)
    assert project.get_alert_check(first)["notification_pending"] == 0
    assert (
        project.db.execute(
            "SELECT count(*) FROM notification_items WHERE source_kind='watch'"
        ).fetchone()[0]
        == 2
    )


def test_unchanged_membership_and_fields_are_not_rewritten(scene):
    project, _, _, rows, alert = scene
    record(scene, rows)
    first_members = [
        tuple(row)
        for row in project.db.execute(
            "SELECT * FROM watch_seen_rows WHERE watch_id=?", (alert,)
        )
    ]
    first_fields = [
        tuple(row)
        for row in project.db.execute(
            "SELECT * FROM watch_row_field_state WHERE watch_id=?", (alert,)
        )
    ]
    statements = []
    project.db.set_trace_callback(statements.append)
    try:
        second = record(scene, rows)
    finally:
        project.db.set_trace_callback(None)
    assert kinds(project, second) == []
    assert [
        tuple(row)
        for row in project.db.execute(
            "SELECT * FROM watch_seen_rows WHERE watch_id=?", (alert,)
        )
    ] == first_members
    assert [
        tuple(row)
        for row in project.db.execute(
            "SELECT * FROM watch_row_field_state WHERE watch_id=?", (alert,)
        )
    ] == first_fields


def test_terminal_monitored_cell_is_an_observation_not_permanent_block(scene):
    from frisket.engine.store.result_generations import ResultGenerationStore
    from frisket.engine.store.runs import RunResultStore
    from helpers import run_writer_authority_fixture

    project, sheet, column, rows, alert = scene
    record(scene, rows)
    op = project.append_change("map.ask", label="failed observation")
    run = RunResultStore(project).start_run(
        op, sheet, "map.ask", row_ids=rows, total_rows=len(rows)
    )
    authority = run_writer_authority_fixture(project, run, output_column_ids={column})
    generations = ResultGenerationStore(project)
    generations.declare(
        run,
        column,
        output_role="answer",
        compatibility_key="sha256:test",
        write_mode="replace_scope",
        claim_token=authority.claim_token,
    )
    generations.seal(
        run, [column], claim_token=authority.claim_token, terminal_disposition="failed"
    )
    RunResultStore(project).finish_run(
        run,
        "failed",
        writer_attempt_id=authority.writer_attempt_id,
        claim_token=authority.claim_token,
    )
    checked = record(scene, rows)
    assert project.get_alert_check(checked)["status"] == "ok"
    assert kinds(project, checked) == ["row_changed"] * len(rows)
    assert "not_completed" in project.alert_check_events(checked)[0]["after_json"]
