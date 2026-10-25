from __future__ import annotations

import pytest

from frisket.engine.runner import RunProgress
from frisket.engine.store import Project
from frisket.engine.store.runs import RunResultStore
from frisket.server.mcp import LocalBackend
from frisket.server.mcp.backends import _action_payload


def test_unknown_canonical_kind_is_refused_before_confirmation_injection() -> None:
    """An unknown kind cannot declare its own confirmation authority fields."""

    action = {
        "schema_version": "frisket.action.v2",
        "kind": "unknown.caller_controlled",
        "capabilities": ["project:write"],
        "params": {
            "confirmed": False,
            "consented_promise_set_hash": "embedded-untrusted-echo",
        },
    }

    with pytest.raises(ValueError, match="unknown canonical action kind"):
        _action_payload(
            action,
            confirmed=True,
            consented_promise_set_hash="tool-controlled-echo",
        )


def test_mcp_status_does_not_report_terminal_run_as_live(tmp_path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    project = Project.create(root / "demo.frisket", name="demo")
    sheet_id = project.add_sheet("data")
    op_id = project.append_op("map", {"recipe": "template"})
    run_id = RunResultStore(project).start_run(
        op_id,
        sheet_id,
        "map.template",
        total_rows=0,
    )
    RunResultStore(project).finish_run(run_id, "completed")
    project.close()

    backend = LocalBackend(root)
    backend.ws.active_runs[("demo", run_id)] = RunProgress(
        run_id=run_id,
        total=0,
    )

    assert backend.get_run_status("demo", run_id)["live"] is False


def test_mcp_status_uses_the_public_all_failed_projection(tmp_path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    project = Project.create(root / "demo.frisket", name="demo")
    sheet_id = project.add_sheet("data")
    op_id = project.append_op("map", {"recipe": "template"})
    run_id = RunResultStore(project).start_run(
        op_id,
        sheet_id,
        "map.template",
        total_rows=1,
    )
    project.db.execute(
        "UPDATE runs SET completed_rows=1, failed_rows=1 WHERE id=?",
        (run_id,),
    )
    RunResultStore(project).finish_run(run_id, "completed")
    project.close()

    status = LocalBackend(root).get_run_status("demo", run_id)

    assert status["status"] == "failed"
    assert status["total"] == 1
    assert status["completed"] == 1
    assert status["failed"] == 1
    assert status["attempted"] == 1
    assert status["succeeded"] == 0
    assert status["skipped"] == 0
    assert status["remaining"] == 0
