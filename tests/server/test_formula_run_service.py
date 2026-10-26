from pathlib import Path

from fastapi.testclient import TestClient

from frisket.engine.jobs.queue import ACTION_RUN_KIND
from frisket.engine.jobs.worker import Worker
from frisket.server.app import create_app
from frisket.server.services.action_runs import ActionRunService
from frisket.server.workspace import Workspace


def test_formula_runs_through_normal_service_without_model_configuration(
    tmp_path: Path,
):
    workspace = Workspace(tmp_path / "workspace", enable_local_model_pull=False)
    workspace.create("Formula", project_id="formula")
    project = workspace.get("formula")
    sheet = project.add_sheet("Numbers")
    amount = project.add_column(sheet, "amount", type="number")
    rows = project.add_rows(sheet, [{"amount": 3}, {"amount": 8}], {"amount": amount})
    response = ActionRunService(workspace).run_action(
        "formula",
        {
            "action_id": "map.formula",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet, "row_ids": [rows[1]]},
            "params": {"formula": {"text": "{{amount}} * 2"}},
            "output_names": {"result": "doubled"},
            "idempotency_key": "formula-service",
        },
    )
    assert response.status_code == 200, response.payload
    assert response.payload["status"] == "queued", response.payload
    job_id = response.payload["job_id"]
    assert job_id is not None
    assert workspace.queue.get(job_id).kind == ACTION_RUN_KIND
    assert not any(column["name"] == "doubled" for column in project.columns(sheet))
    worker = Worker(workspace.queue, workspace.registry, worker_id="formula-service")
    for _ in range(10):
        if workspace.queue.get(job_id).status == "done":
            break
        assert worker.run_once() is True
    assert workspace.queue.get(job_id).status == "done"
    output = next(
        column for column in project.columns(sheet) if column["name"] == "doubled"
    )
    assert output["type"] == "number"
    values = project.get_values(sheet, output["id"], row_ids=rows)
    assert values[rows[1]] == 16
    assert values.get(rows[0]) is None


def test_formula_http_run_enqueues_worker_before_compute(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "http-workspace")) as client:
        project_id = client.post("/api/projects", json={"name": "Formula"}).json()["id"]
        workspace = client.app.state.workspace
        project = workspace.get(project_id)
        sheet = project.add_sheet("Numbers")
        amount = project.add_column(sheet, "amount", type="number")
        rows = project.add_rows(sheet, [{"amount": 3}], {"amount": amount})
        response = client.post(
            f"/api/projects/{project_id}/actions/v1/run",
            json={
                "action_id": "map.formula",
                "scope": {"kind": "sheet_rows", "sheet_id": sheet},
                "params": {"formula": {"text": "={{amount}} * 2"}},
                "output_names": {"result": "doubled"},
                "idempotency_key": "formula-http",
            },
        )
        assert response.status_code == 200, response.text
        launched = response.json()
        assert launched["status"] == "queued"
        assert launched["run_id"] is None
        assert not any(column["name"] == "doubled" for column in project.columns(sheet))
        job_id = launched["job_id"]
        assert workspace.queue.get(job_id).kind == ACTION_RUN_KIND
        worker = Worker(workspace.queue, workspace.registry, worker_id="formula-http")
        for _ in range(10):
            if workspace.queue.get(job_id).status == "done":
                break
            assert worker.run_once() is True
        assert workspace.queue.get(job_id).status == "done"
        column = next(
            column for column in project.columns(sheet) if column["name"] == "doubled"
        )
        assert project.get_values(sheet, column["id"], row_ids=rows)[rows[0]] == 6
