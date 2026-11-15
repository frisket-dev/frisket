"""Returned group results retain the source capture from the paid request."""

import json

from frisket.engine.executor import group_summary_runtime as reduces
from frisket.engine.store.staleness import sheet_freshness
from frisket.execution.attempt import abandon_stale_dispatching_attempts
from test_reduce_effect_checkpoint import (
    project,  # noqa: F401 - shared real project fixture
    _action,
    _CountingSummaryAdapter,
    _project_key_router,
    _run,
)


def test_group_replay_keeps_original_consumed_capture_after_equal_text_edit(
    project, monkeypatch  # noqa: F811 - imported pytest fixture
):
    provider = _CountingSummaryAdapter()
    router = _project_key_router(provider)
    action = _action(project._sheet_id, key="group-capture-recovery")
    write = reduces._write_reduce_group_summary_result
    crashed_once = False

    def crash_before_publication(*args, **kwargs):
        nonlocal crashed_once
        if not crashed_once:
            crashed_once = True
            raise RuntimeError("injected result publication crash")
        return write(*args, **kwargs)

    monkeypatch.setattr(
        reduces, "_write_reduce_group_summary_result", crash_before_publication
    )
    crashed = _run(project, action, router)
    assert crashed.status == "failed"
    assert len(provider.requests) == 2
    checkpoint = project.db.execute(
        "SELECT payload FROM effect_checkpoints WHERE state='returned' LIMIT 1"
    ).fetchone()
    saved = json.loads(checkpoint[0])["computation"]["consumed_capture"]
    column = next(
        col for col in project.columns(project._sheet_id) if col["name"] == "story"
    )
    row = project.visible_row_ids(project._sheet_id)[0]
    original = project.get_values(project._sheet_id, column["id"])[row]
    project.apply_edits([{"row_id": row, "column_id": column["id"], "value": original}])
    run_id = int(project.db.execute("SELECT max(id) FROM runs").fetchone()[0])
    project.db.execute(
        "UPDATE execution_attempts SET created_at=datetime('now','-7 hours') WHERE run_id=?",
        (run_id,),
    )
    project.db.commit()
    assert abandon_stale_dispatching_attempts(project, run_id) == 1
    resumed = _run(project, action, router)
    assert resumed.status == "completed", resumed.errors
    assert len(provider.requests) == 2
    bundle = project.db.execute(
        "SELECT output_sheet_id,capture_json FROM consumed_source_bundles WHERE run_id=?",
        (run_id,),
    ).fetchone()
    assert json.loads(bundle["capture_json"])["scopes"] == saved["scopes"]
    assert sheet_freshness(project, bundle["output_sheet_id"]) == "stale"
    project.undo()
    assert sheet_freshness(project, bundle["output_sheet_id"]) == "synced"
