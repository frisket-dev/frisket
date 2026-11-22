from __future__ import annotations


from frisket.engine.jobs.ports import (
    JobHandlerContext,
)
from frisket.engine.jobs.runs import RUN_PROJECT_KIND, register_project_run_handler
from frisket.engine.jobs.worker import HandlerRegistry
from frisket.engine.store import Project
from frisket.engine.store.runs import RunResultStore


def test_already_cancelled_run_returns_its_terminal_posture(tmp_path) -> None:
    """A queued replay preserves the already-terminal run without dispatch."""
    project_id = "cancelled-settle-once"
    project = Project.create(tmp_path / f"{project_id}.frisket")
    sheet_id = project.add_sheet("Source")
    change_id = project.append_change("map.template", {"action_kind": "map.template"})
    run_id = RunResultStore(project).start_run(
        change_id,
        sheet_id,
        "map.template",
        params={"action_kind": "map.template"},
        total_rows=0,
    )
    project.db.execute(
        "UPDATE runs SET status='cancelled', finished_at=datetime('now') WHERE id=?",
        (run_id,),
    )
    project.db.commit()
    project.close()

    registry = HandlerRegistry()
    register_project_run_handler(
        registry,
        workspace_root=tmp_path,
    )
    handler = registry.get(RUN_PROJECT_KIND)
    assert handler is not None

    result = handler(
        {
            "project_id": project_id,
            "run_id": run_id,
            "spec": {"action_kind": "map.template"},
            "v1_cache_mode": "replay",
        },
        JobHandlerContext.without_job_row(),
    )

    assert result["status"] == "cancelled"
    assert result["skipped"] is True
