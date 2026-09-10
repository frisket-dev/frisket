from __future__ import annotations

import json

from fastapi.testclient import TestClient

from action_test_helpers import typed_map_request
from frisket.authoring.actions import ACTION_SCHEMA_VERSION, stable_json
from frisket.contracts.action import ActionResult
from frisket.ai.llm import ModelRouter
from frisket.server.app import create_app


def _client(tmp_path) -> TestClient:
    return TestClient(
        create_app(
            tmp_path / "workspace",
            router=ModelRouter(cache=None, cache_mode="off"),
        )
    )


def _seed(client: TestClient) -> tuple[str, int]:
    pid = client.post("/api/projects", json={"name": "Agent Contract"}).json()["id"]
    project = client.app.state.workspace.get(pid)
    sheet = project.add_sheet("people")
    cols = {
        "name": project.add_column(sheet, "name", type="text"),
        "age": project.add_column(sheet, "age", type="integer"),
    }
    project.add_rows(
        sheet,
        [{"name": "Ada", "age": 36}, {"name": "Grace", "age": 85}],
        cols,
    )
    return pid, sheet


def test_action_schema_and_project_read_contract(tmp_path):
    client = _client(tmp_path)
    pid, sheet = _seed(client)

    schema = client.get("/api/actions/schema").json()
    assert schema["schema_version"] == ACTION_SCHEMA_VERSION
    assert {"describe_project", "read_range", "run_action", "export_project"} <= {
        action["name"] for action in schema["actions"]
    }
    assert "run_recipe" not in {action["name"] for action in schema["actions"]}

    described = client.get(f"/api/projects/{pid}/actions/describe").json()
    assert described["action"] == "describe_project"
    assert described["project"]["name"] == "Agent Contract"
    assert described["sheets"][0]["columns"][0]["name"] == "name"

    page = client.post(
        f"/api/projects/{pid}/actions/read-range",
        json={"sheet_id": sheet, "columns": ["name"], "limit": 1},
    ).json()
    assert page["schema_version"] == ACTION_SCHEMA_VERSION
    assert page["action"] == "read_range"
    assert page["rows"] == [{"row_id": 1, "row_index": 1, "cells": {"name": "Ada"}}]
    assert "age" not in page["rows"][0]["cells"]


def test_action_run_status_trace_and_export_contract(tmp_path):
    client = _client(tmp_path)
    pid, sheet = _seed(client)
    action = typed_map_request(
        "map.template",
        sheet,
        params={"template": {"text": "{{name}}"}},
        output_names={"rendered": "name_copy"},
        idempotency_key="programmatic-template-run@sha256:test",
    )

    estimate = client.post(
        f"/api/projects/{pid}/actions/v1/estimate",
        json={
            "action": action
            | {"idempotency_key": "programmatic-template-estimate@sha256:test"}
        },
    )
    assert estimate.status_code == 200
    assert estimate.json()["action"]["kind"] == "map.template"

    run = client.post(
        f"/api/projects/{pid}/actions/v1/run",
        json=action,
    )
    assert run.status_code == 200, run.text
    run_body = ActionResult.model_validate(run.json())
    assert run_body.schema_version == "frisket.action_result.v1"
    assert run_body.status == "completed"
    assert run_body.action.kind == "map.template"
    assert run_body.run_id is not None
    run_id = run_body.run_id

    status = client.get(f"/api/projects/{pid}/actions/runs/{run_id}/status").json()
    assert status["action"] == "run_status"
    assert status["run"]["id"] == run_id
    assert status["run"]["status"] in {"queued", "running", "completed"}

    trace = client.get(f"/api/projects/{pid}/actions/runs/{run_id}/trace")
    assert trace.status_code == 200
    assert trace.json()["action"] == "run_trace"
    assert trace.json()["run_id"] == run_id

    exports = client.get(f"/api/projects/{pid}/actions/export").json()
    assert exports["exports"]["bundle"].endswith("/export")
    assert exports["exports"]["work_log_html"].endswith("/work-log.html")


def test_stable_json_is_deterministic():
    assert stable_json({"b": 2, "a": 1}) == json.dumps(
        {"a": 1, "b": 2},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
