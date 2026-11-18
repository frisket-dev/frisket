"""Authored selection identity and frozen full cohorts across both job transports."""

from copy import deepcopy

import pytest

from frisket.actions.system import typed_action_for_request
from frisket.actions.types import SheetRows
from frisket.engine.executor.actions import run_action_spec
from frisket.engine.executor.action_jobs import (
    ActionJobEnvelope,
    bind_typed_action_job,
    reserve_typed_action_job,
)
from frisket.engine.executor.map_rows_action import typed_request_hash
from frisket.engine.executor.queued_actions import queued_v1_payload_envelope
from frisket.server.services.action_runs import ActionRunService
from frisket.server.workspace import Workspace


def seed(project, count=701):
    sheet = project.add_sheet("Rows")
    cols = {
        "note": project.add_column(sheet, "note", type="text"),
        "rank": project.add_column(sheet, "rank", type="integer"),
    }
    rows = project.add_rows(
        sheet, [{"note": f"value {i}", "rank": i} for i in range(count)], cols
    )
    selection = {
        "scope": {"kind": "sheet", "sheet_id": sheet},
        "filters": [{"column_id": cols["rank"], "condition": {"gte": "100"}}],
        "order": [{"column_id": cols["rank"], "dir": "desc"}],
        "limit": 600,
    }
    body = {
        "action_id": "map.regex_extract",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet, "selection": selection},
        "params": {"input_columns": ["note"], "pattern": r"\d+"},
        "output_names": {"extracted": "number"},
        "idempotency_key": "selected",
    }
    return sheet, cols, rows, body


def forbid_selection(*args, **kwargs):
    raise AssertionError("A frozen or replayed request must not reevaluate selection")


def test_direct_selection_full_cohort_and_authored_replay(make_project, monkeypatch):
    project = make_project("selected")
    sheet, cols, rows, body = seed(project)
    first = run_action_spec(project, body, project_id="p")
    assert first.status == "completed", first
    cohort = [
        r[0]
        for r in project.db.execute(
            "SELECT row_id FROM run_rows WHERE run_id=? ORDER BY row_id",
            (first.run_id,),
        )
    ]
    assert cohort == rows[101:]
    project.add_rows(sheet, [{"note": "new 900", "rank": 900}], cols)
    monkeypatch.setattr(
        "frisket.engine.views.evaluate.open_selection", forbid_selection
    )
    replay = run_action_spec(project, body, project_id="p")
    assert replay.receipt_id == first.receipt_id
    changed = deepcopy(body)
    changed["scope"]["selection"]["limit"] = 599
    conflict = run_action_spec(project, changed, project_id="p")
    assert any(error.code == "idempotency_conflict" for error in conflict.errors)
    # Planning a changed draft must not inherit an unrelated old cohort.
    monkeypatch.undo()
    from frisket.engine.executor.selection_scope import resolve_action_selection

    planned = resolve_action_selection(project, typed_action_for_request(changed))
    assert len(planned.request.scope.row_ids) == 599
    assert max(planned.request.scope.row_ids) > rows[-1]


def test_project_run_queue_freezes_selection_without_worker_query(
    tmp_path, monkeypatch
):
    workspace = Workspace(tmp_path / "workspace", enable_local_model_pull=False)
    workspace.create("Selection", project_id="p")
    project = workspace.get("p")
    sheet, cols, rows, body = seed(project)
    response = ActionRunService(workspace).run_action("p", body)
    assert response.status_code == 200, response.payload
    assert response.payload["status"] == "queued", response.payload
    job = workspace.queue.get(response.payload["job_id"])
    assert job.payload["v1_action"]["scope"]["selection"] == body["scope"]["selection"]
    assert job.payload["spec"]["row_ids"] == rows[101:]
    project.add_rows(sheet, [{"note": "new", "rank": 999}], cols)
    deleted = run_action_spec(
        project,
        {
            "action_id": "row.delete",
            "scope": {"kind": "project"},
            "params": {"sheet_id": sheet, "row_ids": [rows[-1]]},
            "idempotency_key": "delete-selected",
        },
        project_id="p",
    )
    assert deleted.status == "completed", deleted
    monkeypatch.setattr(
        "frisket.engine.views.evaluate.open_selection", forbid_selection
    )
    envelope = queued_v1_payload_envelope(job.payload)
    assert envelope is not None
    assert envelope.runner_spec["row_ids"] == rows[101:]
    replay = ActionRunService(workspace).run_action("p", body)
    assert replay.payload["receipt_id"] == response.payload["receipt_id"]


def test_generic_action_job_freezes_selection_and_keeps_authored_hash(
    make_project, monkeypatch
):
    project = make_project("formula")
    sheet, cols, rows, body = seed(project)
    body.update(
        action_id="map.formula",
        params={"formula": {"text": "{{rank}} * 2"}},
        output_names={"result": "doubled"},
    )
    authored = typed_action_for_request(body)
    envelope = reserve_typed_action_job(project, "p", authored)
    assert isinstance(envelope, ActionJobEnvelope), envelope
    assert envelope.params_hash == typed_request_hash(authored)
    project.add_rows(sheet, [{"note": "new", "rank": 999}], cols)
    monkeypatch.setattr(
        "frisket.engine.views.evaluate.open_selection", forbid_selection
    )
    restored = bind_typed_action_job(
        ActionJobEnvelope.from_json(envelope.to_json()), project=project
    )
    assert list(restored.request.scope.row_ids) == rows[101:]
    assert restored.request.scope.selection is None
    assert restored.authored_scope.selection is not None
    assert typed_request_hash(restored) == envelope.params_hash


def test_selection_cannot_silently_widen_to_all_rows():
    selection = {"scope": {"kind": "sheet", "sheet_id": 1}}
    with pytest.raises(ValueError, match="not both"):
        SheetRows(sheet_id=1, row_ids=[1], selection=selection)
    with pytest.raises(ValueError, match="sheet"):
        SheetRows(sheet_id=2, selection=selection)
