from __future__ import annotations

from pathlib import Path

import pytest

from frisket.server.services.action_runs import ActionRunService
from frisket.server.workspace import Workspace


def test_prepared_recovery_rejects_recreated_output_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = Workspace(tmp_path / "workspace")
    project_id = workspace.create("Prepared recovery fence")["id"]
    project = workspace.get(project_id)
    sheet_id = project.add_sheet("People")
    source_id = project.add_column(sheet_id, "note", type="text")
    project.add_rows(
        sheet_id,
        [{"note": "Call 212-555-0123"}],
        {"note": source_id},
    )
    body = {
        "action_id": "map.regex_extract",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {
            "input_columns": ["note"],
            "pattern": r"\d{3}-\d{3}-\d{4}",
        },
        "output_names": {"extracted": "phone"},
        "idempotency_key": "prepared-recreated-output@1",
    }
    service = ActionRunService(workspace)
    original_enqueue = workspace.queue.enqueue

    def crash_before_enqueue(*_args, **_kwargs):
        raise RuntimeError("simulated crash before enqueue")

    monkeypatch.setattr(workspace.queue, "enqueue", crash_before_enqueue)
    with pytest.raises(RuntimeError, match="before enqueue"):
        service.run_action(project_id, body)
    monkeypatch.setattr(workspace.queue, "enqueue", original_enqueue)

    prepared_output_id = int(
        next(
            column["id"]
            for column in project.columns(sheet_id)
            if column["name"] == "phone"
        )
    )
    project.db.execute(
        "UPDATE columns SET name='phone (deleted)' WHERE id=?",
        (prepared_output_id,),
    )
    project.db.commit()
    replacement_id = project.add_column(sheet_id, "phone", type="text")
    assert replacement_id != prepared_output_id

    refused = service.run_action(project_id, body)

    assert refused.status_code == 400
    assert refused.payload["status"] == "failed"
    assert refused.payload["errors"][0]["code"] == "output_target_changed"
    assert (
        workspace.queue.list_project_jobs(project_id, kind="project.run", limit=20)
        == []
    )
