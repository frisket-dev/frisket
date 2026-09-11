from __future__ import annotations

from pathlib import Path

import pytest

from frisket.engine.store.runs import RunResultStore
from frisket.server.services.action_runs import ActionRunService
from frisket.server.services.column_runs import (
    ColumnRunHistoryRouteError,
    ColumnRunHistoryService,
)
from frisket.server.workspace import Workspace


def _completed_template_run(root: Path):
    workspace = Workspace(root, enable_local_model_pull=False)
    workspace.create("Reviewed revision", project_id="reviewed-revision")
    project = workspace.get("reviewed-revision")
    sheet_id = project.add_sheet("People")
    source_id = project.add_column(sheet_id, "name")
    row_ids = project.add_rows(
        sheet_id,
        [{"name": "Ada"}, {"name": "Grace"}, {"name": "Katherine"}],
        {"name": source_id},
    )
    result = ActionRunService(workspace).run_action(
        "reviewed-revision",
        {
            "action_id": "map.template",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
            "params": {"template": {"text": "Hello {{name}}"}},
            "output_names": {"rendered": "greeting"},
            "idempotency_key": "reviewed-revision@1",
        },
    )
    assert result.status_code == 200, result.payload
    run_id = int(result.payload["run_id"])
    output_id = int(
        next(
            column
            for column in project.columns(sheet_id)
            if column["name"] == "greeting"
        )["id"]
    )
    return workspace, project, sheet_id, row_ids, run_id, output_id


def test_revision_reuses_config_on_exact_reviewed_rows_without_authorization(
    tmp_path: Path,
) -> None:
    workspace, project, sheet_id, row_ids, run_id, output_id = _completed_template_run(
        tmp_path / "workspace"
    )
    store = RunResultStore(project)
    store.set_result_review_metadata(run_id, row_ids[2], output_id, "reject", None)
    store.set_result_review_metadata(run_id, row_ids[0], output_id, "accept", None)

    response = ColumnRunHistoryService(workspace).reviewed_run_revision(
        "reviewed-revision", output_id, run_id
    )

    assert response == {
        "schema_version": "frisket.reviewed_run_revision.v1",
        "source_run_id": run_id,
        "reviewed_rows": 2,
        "draft": {
            "action_id": "map.template",
            "scope": {
                "kind": "sheet_rows",
                "sheet_id": sheet_id,
                "row_ids": [row_ids[0], row_ids[2]],
            },
            "params": {"template": {"text": "Hello {{name}}"}},
            "output_names": {"rendered": "greeting"},
        },
    }


def test_revision_requires_a_reviewed_result(tmp_path: Path) -> None:
    workspace, _project, _sheet_id, _rows, run_id, output_id = _completed_template_run(
        tmp_path / "workspace"
    )

    with pytest.raises(ColumnRunHistoryRouteError) as raised:
        ColumnRunHistoryService(workspace).reviewed_run_revision(
            "reviewed-revision", output_id, run_id
        )

    assert raised.value.status_code == 409
