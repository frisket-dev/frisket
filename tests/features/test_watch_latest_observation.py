import json

import pytest

from frisket.engine.store import Project
from frisket.server.notifications.projections import watch_run_notification_candidate


@pytest.fixture
def scene(tmp_path):
    with Project.create(tmp_path / "alerts.frisket") as project:
        sheet = project.add_sheet("records")
        column = project.add_column(sheet, "value")
        rows = project.add_rows(
            sheet, [{"value": "old"} for _ in range(3)], {"value": column}
        )
        watch = project.add_watch(
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
        yield project, sheet, column, rows, watch


def record(scene, rows, *, status="ok", query_hash="scope-1", hits=None):
    project, sheet, _, _, watch = scene
    return project.record_watch_run(
        watch,
        hits=hits
        if hits is not None
        else ({"sheet_id": sheet, "row_id": row, "snippet": "match"} for row in rows),
        status=status,
        op_cursor_before=project.op_cursor,
        op_cursor_after=project.op_cursor,
        resolved_query_hash=query_hash,
    )


def kinds(project, run):
    return [row["event_kind"] for row in project.watch_run_events(run)]


def test_first_observation_silent_and_returning_rows_alert_again(scene):
    project, _, _, rows, watch = scene
    first = record(scene, rows[:1])
    assert kinds(project, first) == []
    assert project.get_watch_run(first)["matched_rows"] == 1
    assert project.get_watch_run(first)["new_rows"] == 0
    assert (
        watch_run_notification_candidate(project, watch_id=watch, run_id=first) is None
    )
    assert kinds(project, record(scene, [])) == ["row_exited"]
    assert kinds(project, record(scene, rows[:1])) == ["row_entered"]
    assert kinds(project, record(scene, rows[:1])) == []
    assert project.db.execute("SELECT count(*) FROM watch_seen_rows").fetchone()[0] == 1


def test_conditions_share_one_check_and_incomplete_preserves_baseline(scene):
    project, _, column, rows, watch = scene
    record(scene, rows[:2])
    project.apply_edits([{"row_id": rows[0], "column_id": column, "value": "updated"}])
    failed = record(scene, [], status="error")
    assert kinds(project, failed) == []
    assert project.db.execute("SELECT count(*) FROM watch_seen_rows").fetchone()[0] == 2
    changed = record(scene, [rows[0], rows[2]])
    assert kinds(project, changed) == ["row_entered", "row_exited", "row_changed"]
    event = project.watch_run_events(changed)[-1]
    assert "old" in event["before_json"]
    assert "updated" in event["after_json"]
    assert project.get_watch(watch)["last_run_id"] == changed


def test_criteria_change_silently_replaces_baseline(scene):
    project, _, _, rows, _ = scene
    record(scene, rows[:1])
    changed = record(scene, rows[1:], query_hash="scope-2")
    assert kinds(project, changed) == []
    assert kinds(project, record(scene, rows, query_hash="scope-2")) == ["row_entered"]


def test_definition_change_during_staging_cannot_publish_old_check(scene):
    project, sheet, _, rows, watch = scene
    record(scene, rows[:1])
    before = project.get_watch(watch)["last_run_id"]

    def arriving_hits():
        yield {"sheet_id": sheet, "row_id": rows[0]}
        project.db.execute(
            "UPDATE watches SET query=? WHERE id=?",
            (
                json.dumps(
                    {
                        "kind": "filter",
                        "sheet_id": sheet,
                        "filter": {"value": {"eq": "new"}},
                    }
                ),
                watch,
            ),
        )
        project.db.commit()

    with pytest.raises(ValueError, match="definition changed"):
        record(scene, [], hits=arriving_hits())
    assert project.get_watch(watch)["last_run_id"] == before
    assert project.db.execute("SELECT count(*) FROM watch_runs").fetchone()[0] == 1


def test_all_changes_counted_with_bounded_previews_and_one_summary_key(scene):
    project, sheet, column, _, watch = scene
    record(scene, [])
    rows = project.add_rows(
        sheet, [{"value": "extra"} for _ in range(601)], {"value": column}
    )
    run = record(scene, rows)
    assert project.get_watch_run(run)["matched_rows"] == 601
    assert project.watch_run_events_total(run) == 601
    assert len(project.watch_run_hits(run, 1000)) == 500
    small = watch_run_notification_candidate(
        project, watch_id=watch, run_id=run, limit=5
    )
    large = watch_run_notification_candidate(
        project, watch_id=watch, run_id=run, limit=500
    )
    assert "601" in small["title"]
    assert small["payload"]["event_count"] == 601
    assert small["dedupe_key"] == large["dedupe_key"]
    assert len(small["source_event_ids"]) == 5
