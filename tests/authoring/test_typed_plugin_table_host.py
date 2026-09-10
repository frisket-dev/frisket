"""Installed ordinary CreateSheet actions use the shared native table host."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from frisket.authoring.plugin_registry import _reset_default_registry_for_tests
from frisket.authoring.workbench import plugin_runtime, plugin_runtime_status
from frisket.authoring.workbench.installed_actions import resolve_installed_action
from frisket.engine.executor.actions import run_action_spec
from frisket.plugins.manifest_generate import generate_manifest_text
from frisket.server.app import create_app
from tests.deterministic_time import controlled_time


PLUGIN = """
from pydantic import BaseModel, Field
from frisket.actions.core import action, create_sheet, ActionCategory
from frisket.actions.types import ActionParams, TableResult, TableRow, DynamicTableResult, DynamicOutput, TableColumn
from frisket.plugins.sdk import Plugin

class Params(ActionParams):
    count: int = Field(default=3, ge=0)
    key: str = "value"
    fail_at: int | None = None
    invalid: bool = False

class Output(BaseModel):
    value: int = Field(ge=0)

calls = []
closed = []
on_prepare = lambda: None
on_close = lambda: None

def values(params):
    try:
        for number in range(params.count):
            if number == params.fail_at:
                raise ValueError("row refused")
            yield number
    finally:
        closed.append(True)
        on_close()

def static(params: Params) -> TableResult[Output]:
    calls.append("static")
    on_prepare()
    return TableResult(rows=(TableRow(output=Output.model_construct(value=-1) if params.invalid else Output(value=n)) for n in values(params)))

def dynamic(params: Params) -> TableResult[DynamicOutput]:
    calls.append("dynamic")
    on_prepare()
    return TableResult(rows=(TableRow(output=DynamicOutput({params.key: n})) for n in values(params)))

async def runtime(params: Params) -> DynamicTableResult:
    calls.append("runtime")
    on_prepare()
    async def rows():
        for n in values(params):
            yield TableRow(output=DynamicOutput({params.key: n}))
    return DynamicTableResult(schema=(TableColumn(params.key, "number", hidden=True),), rows=rows())

plugin = Plugin(id="test.tables", version="1.0.0", capabilities=["plugin:trusted_local_backend"], actions=(
    action(name="static", title="Static", description="Produce rows", category=ActionCategory.SOURCES, run=create_sheet(static)),
    action(name="dynamic", title="Dynamic", description="Produce rows", category=ActionCategory.SOURCES, run=create_sheet(dynamic, columns_from=lambda p: (TableColumn(p.key, "number"),))),
    action(name="runtime", title="Runtime", description="Produce rows", category=ActionCategory.SOURCES, run=create_sheet(runtime)),
))
"""


@pytest.fixture
def installed_table(tmp_path, monkeypatch):
    root = tmp_path / "bundled"
    package = root / "test.tables"
    package.mkdir(parents=True)
    (package / "plugin.py").write_text(PLUGIN)
    (package / "plugin.json").write_text(generate_manifest_text(package))
    monkeypatch.setattr(plugin_runtime, "_bundled_plugins_root", lambda: root)
    monkeypatch.setattr(plugin_runtime_status, "_bundled_plugins_root", lambda: root)
    _reset_default_registry_for_tests()
    app = create_app(
        tmp_path / "workspace",
        enable_provider_config=False,
        edition="team",
        serve_spa=False,
    )
    project_id = app.state.workspace.create("Plugin tables")["id"]
    project = app.state.workspace.get(project_id)
    client = TestClient(app)
    registered, _ = resolve_installed_action(project, "test.tables.static")
    namespace = registered.definition.run.handler.__globals__
    calls = namespace["calls"]
    calls.clear()
    namespace["closed"].clear()
    namespace["on_prepare"] = lambda: None
    namespace["on_close"] = lambda: None
    try:
        yield client, project, project_id, package, calls
    finally:
        client.close()
        _reset_default_registry_for_tests()


def request(kind="static", **params):
    return {
        "action_id": f"test.tables.{kind}",
        "scope": {"kind": "project"},
        "sheet_name": "Produced",
        "params": params,
        "output_names": {params.get("key", "value"): "Renamed"},
        "idempotency_key": "table-once",
    }


@pytest.mark.parametrize("kind", ["static", "dynamic", "runtime"])
def test_native_table_uses_shared_publication_and_replay(installed_table, kind):
    client, project, project_id, _package, calls = installed_table
    body = request(kind)
    response = client.post(f"/api/projects/{project_id}/actions/v1/run", json=body)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == "completed", result
    sheet_id = project.db.execute(
        "SELECT id FROM sheets WHERE name='Produced'"
    ).fetchone()[0]
    columns = project.columns(sheet_id, include_hidden=True)
    assert [column["name"] for column in columns] == ["Renamed"]
    assert bool(columns[0]["hidden"]) is (kind == "runtime")
    assert list(project.get_values(sheet_id, columns[0]["id"]).values()) == [0, 1, 2]
    assert calls == [kind]
    repeated = client.post(f"/api/projects/{project_id}/actions/v1/run", json=body)
    assert repeated.json() == result
    assert calls == [kind]  # Replay never reruns the producer.
    op_spec = json.loads(project.db.execute("SELECT spec FROM ops").fetchone()[0])
    assert op_spec["action_id"] == body["action_id"]
    assert op_spec["params"] == {}  # Keep supplied-only Params in the saved intent.


@pytest.mark.parametrize("kind", ["static", "dynamic", "runtime"])
def test_generated_form_schema_and_bounded_preview_share_table_source(
    installed_table, kind
):
    client, project, project_id, _package, calls = installed_table
    body = request(kind, count=25, fail_at=20)
    validated = client.post(
        f"/api/projects/{project_id}/actions/v1/validate-params", json={"action": body}
    )
    assert validated.status_code == 200, validated.text
    assert validated.json()["logical_outputs"] == [
        {"key": "value", "column_type": "integer" if kind == "static" else "number"}
    ]
    started = client.post(f"/api/projects/{project_id}/actions/v1/preview", json=body)
    assert started.status_code == 202, started.text
    preview_id = started.json()["preview_id"]
    payload = None

    def finished():
        nonlocal payload
        payload = client.get(
            f"/api/projects/{project_id}/actions/v1/preview/{preview_id}"
        ).json()
        return payload["status"] != "running"

    with controlled_time(timeout=10) as clock:
        clock.wait_until(finished)
    assert payload["status"] == "done", payload
    result = payload["result"]
    assert result["sampled"] == 20 and result["total"] is None
    assert result["rows"] == [{"Renamed": {"value": n}} for n in range(20)]
    for table in ("sheets", "ops", "receipts", "runs"):
        assert project.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


def test_late_native_failure_aborts_hidden_batches(installed_table):
    _client, project, project_id, _package, _calls = installed_table
    result = run_action_spec(
        project, request(count=503, fail_at=501), project_id=project_id
    )
    assert result.status == "failed", result
    assert result.errors[0].code == "invalid_params"
    for table in ("sheets", "columns", "rows", "cells", "ops", "receipts"):
        assert project.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


def test_existing_sheet_refuses_before_producer_invocation(installed_table):
    _client, project, project_id, _package, calls = installed_table
    project.add_sheet("Produced")
    result = run_action_spec(project, request("runtime"), project_id=project_id)
    assert result.status == "failed"
    assert result.errors[0].code == "duplicate_sheet_name"
    assert calls == []


def test_package_change_refuses_before_native_handler_or_writes(installed_table):
    _client, project, project_id, package, calls = installed_table
    module = package / "plugin.py"
    module.write_text(module.read_text() + "\n# changed after admission\n")
    result = run_action_spec(project, request(), project_id=project_id)
    assert result.status == "failed"
    assert calls == []
    assert project.db.execute("SELECT count(*) FROM sheets").fetchone()[0] == 0


def test_read_only_invocation_does_not_prepare_or_execute_handler(installed_table):
    from frisket.team.enforcement import protect_core_app

    client, project, project_id, _package, calls = installed_table
    required = []

    class ViewerAccess:
        def resolve_project_for_user_id(self, user_id, slug, **kwargs):
            return {"org_id": 1, "storage_org_id": 1, "slug": slug}

        def can_on_project(self, org_id, slug, user_id, need):
            required.append(need)
            return need == "viewer"

    protect_core_app(
        app=client.app,
        resolve_user=lambda request: {
            "id": 7,
            "email": "reader@example.com",
            "org_id": 1,
            "auth": "session",
        },
        admin_emails=lambda: set(),
        project_access=ViewerAccess(),
    )
    response = client.post(f"/api/projects/{project_id}/actions/v1/run", json=request())
    assert response.status_code == 403, response.text
    assert required == ["editor"] and calls == []
    assert project.db.execute("SELECT count(*) FROM receipts").fetchone()[0] == 0


def test_model_instance_cannot_bypass_host_output_schema(installed_table):
    _client, project, project_id, _package, _calls = installed_table
    result = run_action_spec(project, request(invalid=True), project_id=project_id)
    assert result.status == "failed", result
    assert project.db.execute("SELECT count(*) FROM sheets").fetchone()[0] == 0
    assert project.db.execute("SELECT count(*) FROM receipts").fetchone()[0] == 0
