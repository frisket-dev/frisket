"""Installed Python definitions use native binding and the shared callable host."""

import json
import sys
import time

import pytest
from fastapi.testclient import TestClient

from frisket.actions.system import ACTION_REGISTRY
from frisket.actions.types import ActionRequest
from frisket.authoring.plugin_registry import _reset_default_registry_for_tests
from frisket.authoring.workbench import plugin_runtime, plugin_runtime_status
from frisket.authoring.workbench.installed_actions import (
    bind_installed_action,
    resolve_installed_action,
)
from frisket.engine.executor.actions import run_action_spec
from frisket.plugins.action_loader import load_action_module
from frisket.plugins.manifest_generate import generate_manifest_text
from frisket.server.app import create_app


SOURCE = """
from pydantic import BaseModel
from frisket.actions.core import action, ActionCategory, map_rows, create_sheet
from frisket.actions.types import (ActionParams, InvocationContext, QueryPreviewer,
    ColumnRef, Row, RowResult, DynamicTableResult, DynamicOutput, TableRow, TableColumn)
from frisket.plugins.sdk import Plugin
from .sibling import MARKER

calls = []
table_rows = []
class Params(ActionParams):
    sheet_id: int
    fail: bool = False
class Output(BaseModel):
    count: int
    marker: str
def report(params: Params, context: InvocationContext, query: QueryPreviewer) -> Output:
    context.check_cancelled()
    calls.append(params.sheet_id)
    selected = query.preview(query={
        "schema_version": "frisket.query.v1", "kind": "sheet.filter",
        "scope": {"kind": "sheet", "sheet_id": params.sheet_id}, "filter": {}
    }, limit=10, offset=0)
    if params.fail:
        raise ValueError("private author exception")
    return Output(count=selected.row_count, marker=MARKER)
class RowParams(ActionParams):
    text: ColumnRef[str]
    invalid: bool = False
class RowOutput(BaseModel):
    upper: str
def upper(params: RowParams, row: Row) -> RowResult[RowOutput]:
    return RowResult(output=RowOutput.model_construct(upper=1) if params.invalid
        else RowOutput(upper=params.text.read(row).upper()))
class TableParams(ActionParams):
    key: str = "value"
async def table(params: TableParams) -> DynamicTableResult:
    async def rows():
        for value in (1, 2):
            table_rows.append(value)
            yield TableRow(output=DynamicOutput({params.key: value}))
    return DynamicTableResult(schema=(TableColumn(params.key, "number"),), rows=rows())
plugin = Plugin(id="example.native", version="1.0.0", auto_enable=True,
    capabilities=["plugin:trusted_local_backend"],
    actions=(action(name="report", title="Report", description="Native report",
        category=ActionCategory.CONVERT, run=report),
        action(name="upper", title="Upper", description="Native row",
            category=ActionCategory.CONVERT, run=map_rows(upper)),
        action(name="table", title="Table", description="Native table",
            category=ActionCategory.SOURCES, run=create_sheet(table)),))
"""


@pytest.fixture
def installed(tmp_path, monkeypatch):
    root = tmp_path / "bundled"
    package = root / "example.native"
    package.mkdir(parents=True)
    (package / "plugin.py").write_text(SOURCE)
    (package / "sibling.py").write_text('MARKER = "native"\n')
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
    with TestClient(app) as client:
        project_id = app.state.workspace.create("native plugin")["id"]
        project = app.state.workspace.get(project_id)
        sheet = project.add_sheet("Data")
        column = project.add_column(sheet, "Name")
        project.add_rows(sheet, [{"Name": "Ada"}], {"Name": column})
        response = client.get(f"/api/projects/{project_id}/actions/v1/catalog")
        assert response.status_code == 200, response.text
        entries = {entry["kind"]: entry for entry in response.json()["actions"]}
        for action_id in ("report", "upper", "table"):
            entry = entries[f"example.native.{action_id}"]
            assert entry["authoring_contract_version"] == 1
            assert entry["ui_hints"]["form"] == "generated"
        yield client, project, project_id, sheet, package
    _reset_default_registry_for_tests()


def _request(sheet, **params):
    return {
        "action_id": "example.native.report",
        "scope": {"kind": "project"},
        "params": {"sheet_id": sheet, **params},
        "idempotency_key": "report",
    }


def test_package_imports_are_isolated_and_persistent(tmp_path):
    before = list(sys.path)
    modules = []
    for name in ("one", "two"):
        package = tmp_path / name
        package.mkdir()
        (package / "sibling.py").write_text(f"VALUE = {name!r}\n")
        module_path = package / "plugin.py"
        module_path.write_text("from .sibling import VALUE\nstate = []\n")
        module = load_action_module(package, module_path, package_identity=name)
        module.state.append("persisted")
        assert load_action_module(package, module_path, package_identity=name) is module
        modules.append(module)
    assert [module.VALUE for module in modules] == ["one", "two"]
    assert modules[0].state == ["persisted"]
    assert modules[0].__package__ != modules[1].__package__
    assert sys.path == before
    with pytest.raises(ValueError):
        load_action_module(tmp_path / "one", tmp_path / "two/plugin.py")


@pytest.mark.parametrize("fail", [False, True])
def test_installed_callable_shared_facts_replay_and_original_definition(
    installed, fail
):
    client, project, project_id, sheet, _package = installed
    request = _request(sheet, fail=fail)
    bound = bind_installed_action(project, ActionRequest.model_validate(request))
    assert bound is not None
    with pytest.raises(KeyError):
        ACTION_REGISTRY.get(request["action_id"])
    assert bound.action is resolve_installed_action(project, request["action_id"])[0]
    assert bound.implementation_identity["package_sha256"]
    assert bound.runtime_binding.handler_api == "plugin_typed_action_native"
    handler = bound.action.definition.run.handler
    previous_calls = list(handler.__globals__["calls"])
    result = run_action_spec(project, request, project_id=project_id)
    assert result.status == ("failed" if fail else "completed"), result.errors
    assert result.receipt_id
    assert result.outputs[0].kind == "query_preview"
    assert result.value == (None if fail else {"count": 1, "marker": "native"})
    assert "private author" not in result.model_dump_json()
    assert run_action_spec(project, request, project_id=project_id) == result
    assert handler.__globals__["calls"] == [*previous_calls, sheet]
    assert (
        client.get(f"/api/projects/{project_id}/actions/v1/catalog").status_code == 200
    )


@pytest.mark.parametrize("failure", ["package", "grant"])
def test_disabled_or_drifted_package_cannot_load_or_execute(installed, failure):
    _, project, project_id, sheet, package = installed
    request = _request(sheet)
    assert resolve_installed_action(project, request["action_id"])
    if failure == "package":
        (package / "sibling.py").write_text('MARKER = "changed"\n')
    else:
        plugin_runtime_status._record_backend_activation_executable_handlers_grant(
            project, plugin_id="example.native", executable_handlers_allowed=False
        )
        assert resolve_installed_action(project, request["action_id"]) is None
    result = run_action_spec(project, request, project_id=project_id)
    assert result.status == "failed"
    assert project.db.execute("SELECT count(*) FROM receipts").fetchone()[0] == 0


def test_http_package_drift_preserves_integrity_refusal(installed):
    client, project, project_id, sheet, package = installed
    request = _request(sheet)
    action, binding = resolve_installed_action(project, request["action_id"])
    handler = action.definition.run.handler
    before_calls = list(handler.__globals__["calls"])
    before_database = tuple(project.db.iterdump())

    (package / "sibling.py").write_text('MARKER = "changed"\n')
    install = plugin_runtime_status._workbench_plugin_install_state(
        project, plugin_id=binding.plugin
    )
    assert install is not None
    expected = plugin_runtime_status.runtime_binding_dispatch_error(
        binding,
        capabilities=[
            *json.loads(str(install["permissions_accepted"])),
            "project:write",
        ],
    )
    assert expected is not None
    assert expected["code"] == "plugin_code_integrity_mismatch"
    assert expected["details"]["expected_package_sha256"]
    assert expected["details"]["current_package_sha256"]

    response = client.post(f"/api/projects/{project_id}/actions/v1/run", json=request)

    assert response.status_code == 409, response.text
    error = response.json()["errors"][0]
    assert {key: error[key] for key in expected} == expected
    assert handler.__globals__["calls"] == before_calls
    assert tuple(project.db.iterdump()) == before_database


def test_http_missing_capability_preserves_dispatch_refusal(installed, monkeypatch):
    client, project, project_id, sheet, _package = installed
    request = _request(sheet)
    action, binding = resolve_installed_action(project, request["action_id"])
    handler = action.definition.run.handler
    before_calls = list(handler.__globals__["calls"])
    before_database = tuple(project.db.iterdump())
    expected = plugin_runtime_status.runtime_binding_dispatch_error(
        binding, capabilities=["project:write"]
    )
    assert expected is not None
    assert expected["code"] == "plugin_capability_required"
    assert expected["field"] == "capabilities"
    assert expected["details"]["plugin_id"] == "example.native"
    assert "plugin:trusted_local_backend" in expected["details"]["missing"]

    original_install_state = plugin_runtime_status._workbench_plugin_install_state

    def _revoked_install_state(project_arg, *, plugin_id):
        state = original_install_state(project_arg, plugin_id=plugin_id)
        assert state is not None
        return {**state, "permissions_accepted": "[]"}

    monkeypatch.setattr(
        "frisket.authoring.workbench.installed_actions._workbench_plugin_install_state",
        _revoked_install_state,
    )
    response = client.post(f"/api/projects/{project_id}/actions/v1/run", json=request)

    assert response.status_code == 400, response.text
    error = response.json()["errors"][0]
    assert {key: error[key] for key in expected} == expected
    assert handler.__globals__["calls"] == before_calls
    assert tuple(project.db.iterdump()) == before_database


def test_invalid_params_never_invokes_handler_or_reserves(installed):
    _, project, project_id, sheet, _ = installed
    request = _request(sheet, extra="forbidden")
    result = run_action_spec(project, request, project_id=project_id)
    assert result.status == "failed"
    assert project.db.execute("SELECT count(*) FROM receipts").fetchone()[0] == 0


def _preview(client, base, request):
    response = client.post(base + "/preview", json=request)
    assert response.status_code == 202, response.text
    # realtime: the existing preview registry runs its worker on another thread.
    deadline = time.monotonic() + 10
    while True:
        result = client.get(base + "/preview/" + response.json()["preview_id"])
        assert result.status_code == 200, result.text
        if result.json()["status"] not in {"queued", "running"}:
            assert result.json()["status"] == "done", result.text
            return result.json()["result"]
        assert time.monotonic() < deadline, result.text  # realtime: bounded worker wait
        time.sleep(0.01)  # realtime: yield to the real preview worker


def test_native_row_validation_estimate_and_preview(installed):
    client, project, pid, sheet, _ = installed
    base = f"/api/projects/{pid}/actions/v1"
    request = {
        "action_id": "example.native.upper",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet},
        "params": {"text": "Name"},
        "idempotency_key": "row",
    }
    validation = client.post(base + "/validate-params", json={"action": request})
    assert validation.status_code == 200, validation.text
    assert validation.json()["diagnostics"] == {}
    assert validation.json()["logical_outputs"] == [
        {"key": "upper", "column_type": "text"}
    ]
    estimate = client.post(base + "/estimate", json={"action": request})
    assert estimate.status_code == 200, estimate.text
    assert estimate.json()["estimate"]["cost"] == 0
    preview = _preview(client, base, request)
    assert preview["kind"] == "row_overlay"
    assert [row["upper"]["value"] for row in preview["rows"].values()] == ["ADA"]
    assert [column["name"] for column in project.columns(sheet)] == ["Name"]
    assert project.db.execute("SELECT count(*) FROM receipts").fetchone()[0] == 0


def test_native_row_runner_reconstruction_and_backfill_use_installed_definition(
    installed,
):
    _, project, project_id, sheet, _ = installed
    request = {
        "action_id": "example.native.upper",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet},
        "params": {"text": "Name"},
        "idempotency_key": "native-row",
    }
    first = run_action_spec(project, request, project_id=project_id)
    assert first.status == "completed", first.errors
    run_spec = project.db.execute(
        "SELECT params FROM runs WHERE id=?", (first.run_id,)
    ).fetchone()["params"]
    from frisket.engine.executor.map_rows_action import (
        bound_typed_program_request_from_runner_spec,
    )

    reconstructed = bound_typed_program_request_from_runner_spec(
        json.loads(run_spec), project=project
    )
    assert reconstructed is not None
    assert reconstructed.action.definition.run.handler.__name__ == "upper"
    columns = {column["name"]: column["id"] for column in project.columns(sheet)}
    added = project.add_rows(sheet, [{"Name": "Grace"}], {"Name": columns["Name"]})
    backfill = run_action_spec(
        project,
        {
            "action_id": "run.backfill",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet},
            "params": {"column": "upper"},
            "idempotency_key": "native-row-backfill",
        },
        project_id=project_id,
    )
    assert backfill.status == "completed", backfill.errors
    assert project.get_values(sheet, columns["upper"], row_ids=added) == {
        added[0]: "GRACE"
    }


def test_native_row_revalidates_constructed_output_before_publication(installed):
    _, project, project_id, sheet, _ = installed
    result = run_action_spec(
        project,
        {
            "action_id": "example.native.upper",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet},
            "params": {"text": "Name", "invalid": True},
            "idempotency_key": "invalid-native-row",
        },
        project_id=project_id,
    )
    assert result.status == "failed"
    assert [column["name"] for column in project.columns(sheet)] == ["Name"]


def test_native_runtime_table_schema_discovery_preview_and_callable_refusal(installed):
    client, project, pid, sheet, _ = installed
    base = f"/api/projects/{pid}/actions/v1"
    request = {
        "action_id": "example.native.table",
        "scope": {"kind": "project"},
        "params": {"key": "chosen"},
        "sheet_name": "Preview only",
        "idempotency_key": "table",
    }
    registered, _ = resolve_installed_action(project, request["action_id"])
    rows = registered.definition.run.handler.__globals__["table_rows"]
    before = list(rows)
    validation = client.post(base + "/validate-params", json={"action": request})
    assert validation.status_code == 200, validation.text
    assert validation.json()["diagnostics"] == {}
    assert validation.json()["creates_sheet"] is True
    assert validation.json()["logical_outputs"] == [
        {"key": "chosen", "column_type": "number"}
    ]
    assert rows == before
    preview = _preview(client, base, request)
    assert preview["kind"] == "table"
    assert [row["chosen"]["value"] for row in preview["rows"]] == [1, 2]
    assert rows == [*before, 1, 2]
    for endpoint in ("estimate", "preview"):
        body = (
            {"action": _request(sheet)} if endpoint == "estimate" else _request(sheet)
        )
        refused = client.post(base + "/" + endpoint, json=body)
        assert refused.status_code == 400, refused.text
    assert project.db.execute("SELECT count(*) FROM receipts").fetchone()[0] == 0
