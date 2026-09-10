from __future__ import annotations

from typing import Any

from frisket.engine.jobs.ports import (
    JobHandlerContext,
    TrustedJobOrgUnavailable,
    WorkerPorts,
)
from frisket.engine.jobs.runs import RUN_PROJECT_KIND, register_project_run_handler
from frisket.engine.jobs.worker import HandlerRegistry
from frisket.engine.store import Project
from frisket.engine.store.runs import RunResultStore


class RecordingSettlement:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def settle_run(self, **kwargs: Any) -> None:
        self.calls.append(dict(kwargs))

    def settle_action_receipt(self, **_kwargs: Any) -> None:
        pass


def test_already_cancelled_run_settles_once_with_its_terminal_posture(tmp_path) -> None:
    """The worker's cancelled-run race preserves one settlement invocation.

    A retry may claim a job after another actor has already terminalized its
    run.  The fast path must settle exactly once and carry the trusted run-row
    status; omitting that fact lets the commerce adapter reinterpret a
    cancelled run as ordinary completion.
    """
    project_id = "cancelled-settle-once"
    project = Project.create(tmp_path / f"{project_id}.frisket")
    sheet_id = project.add_sheet("Source")
    op_id = project.append_op("map.template", {"action_kind": "map.template"})
    run_id = RunResultStore(project).start_run(
        op_id,
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

    settlement = RecordingSettlement()
    registry = HandlerRegistry()
    register_project_run_handler(
        registry,
        workspace_root=tmp_path,
        worker_ports=WorkerPorts(settlement_port=settlement),
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
    assert len(settlement.calls) == 1
    call = settlement.calls[0]
    assert call["project_id"] == project_id
    assert call["run_id"] == run_id
    assert call["trusted_job_org_id"] is TrustedJobOrgUnavailable.NO_JOB_ROW
    assert call["terminal_status"] == "cancelled"
