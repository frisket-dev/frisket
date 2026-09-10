"""Installed native actions consume only their plugin-scoped project secrets."""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from frisket.authoring.plugin_registry import (
    RuntimeBindingSpec,
    _reset_default_registry_for_tests,
)
from frisket.authoring.workbench import plugin_runtime, plugin_runtime_status
from frisket.engine.executor.actions import run_action_spec
from frisket.authoring.workbench.native_plugin_secrets import HostPluginSecrets
from frisket.engine.store import Project
from frisket.plugins.manifest_generate import generate_manifest_text
from frisket.server.app import create_app
from frisket.team.security.secrets import encrypt_secret


SECRET_NAME = "DEMO_API_KEY"
SECRET_VALUE = "native-project-secret"

SOURCE = """
from pydantic import BaseModel

from frisket.sdk import (
    ActionCategory, ActionParams, ColumnRef, DynamicOutput, DynamicTableResult,
    PluginSecrets, Row, RowResult, Rows, TableColumn, TableResult, TableRow,
    action, create_sheet, map_batch, map_rows,
)
from frisket.plugins.sdk import Plugin

observed = {}

class Empty(ActionParams):
    pass

class RowsParams(ActionParams):
    source: ColumnRef[str]

class Output(BaseModel):
    status: str

def one_row(params: RowsParams, row: Row, secrets: PluginSecrets) -> RowResult[Output]:
    observed.setdefault("map_rows", []).append(secrets.require("DEMO_API_KEY"))
    return RowResult(output=Output(status="configured"))

def whole_batch(
    params: RowsParams, rows: Rows, secrets: PluginSecrets
) -> dict[int, RowResult[Output]]:
    observed.setdefault("map_batch", []).append(secrets.require("DEMO_API_KEY"))
    return {
        row_id: RowResult(output=Output(status="configured"))
        for row_id in rows
    }

def callable_action(params: Empty, secrets: PluginSecrets) -> Output:
    observed.setdefault("callable", []).append(secrets.require("DEMO_API_KEY"))
    return Output(status="configured")

def table_action(params: Empty, secrets: PluginSecrets) -> TableResult[Output]:
    observed.setdefault("create_sheet", []).append(secrets.require("DEMO_API_KEY"))
    return TableResult(rows=[TableRow(output=Output(status="configured"))])

def dynamic_table_action(
    params: Empty, secrets: PluginSecrets
) -> DynamicTableResult:
    observed.setdefault("dynamic_table", []).append(secrets.require("DEMO_API_KEY"))
    return DynamicTableResult(
        schema=(TableColumn("configured", "text"),),
        rows=[TableRow(output=DynamicOutput({"configured": "yes"}))],
    )

plugin = Plugin(
    id="example.native_secrets",
    version="1.0.0",
    auto_enable=True,
    capabilities=["plugin:trusted_local_backend"],
    secrets=["DEMO_API_KEY"],
    actions=(
        action(name="row", title="Row", description="Read a secret per row",
            category=ActionCategory.CONVERT, run=map_rows(one_row)),
        action(name="batch", title="Batch", description="Read a secret per batch",
            category=ActionCategory.CONVERT, run=map_batch(whole_batch)),
        action(name="callable", title="Callable", description="Read a project secret",
            category=ActionCategory.CONVERT, run=callable_action),
        action(name="table", title="Table", description="Read a secret for a table",
            category=ActionCategory.SOURCES, run=create_sheet(table_action)),
        action(name="dynamic_table", title="Dynamic table",
            description="Discover a secret-backed table schema",
            category=ActionCategory.SOURCES, run=create_sheet(dynamic_table_action)),
    ),
)
"""


@pytest.fixture
def installed(tmp_path, monkeypatch):
    bundled = tmp_path / "bundled"
    package = bundled / "example.native_secrets"
    package.mkdir(parents=True)
    (package / "plugin.py").write_text(SOURCE)
    (package / "plugin.json").write_text(generate_manifest_text(package))
    monkeypatch.setattr(plugin_runtime, "_bundled_plugins_root", lambda: bundled)
    monkeypatch.setattr(plugin_runtime_status, "_bundled_plugins_root", lambda: bundled)
    _reset_default_registry_for_tests()
    app = create_app(
        tmp_path / "workspace",
        enable_provider_config=False,
        edition="team",
        serve_spa=False,
    )
    with TestClient(app) as client:
        project_id = app.state.workspace.create("native secrets")["id"]
        project = app.state.workspace.get(project_id)
        sheet_id = project.add_sheet("Source")
        column_id = project.add_column(sheet_id, "Name")
        project.add_rows(sheet_id, [{"Name": "Ada"}], {"Name": column_id})
        catalog = client.get(f"/api/projects/{project_id}/actions/v1/catalog")
        assert catalog.status_code == 200, catalog.text
        yield client, project, project_id, sheet_id
    _reset_default_registry_for_tests()


def _requests(sheet_id: int):
    row_scope = {"kind": "sheet_rows", "sheet_id": sheet_id}
    return {
        "row": {
            "action_id": "example.native_secrets.row",
            "scope": row_scope,
            "params": {"source": "Name"},
            "output_names": {"status": "Row status"},
            "idempotency_key": "secret-row",
        },
        "batch": {
            "action_id": "example.native_secrets.batch",
            "scope": row_scope,
            "params": {"source": "Name"},
            "output_names": {"status": "Batch status"},
            "idempotency_key": "secret-batch",
        },
        "callable": {
            "action_id": "example.native_secrets.callable",
            "scope": {"kind": "project"},
            "params": {},
            "idempotency_key": "secret-callable",
        },
        "create_sheet": {
            "action_id": "example.native_secrets.table",
            "scope": {"kind": "project"},
            "params": {},
            "sheet_name": "Secret table",
            "idempotency_key": "secret-table",
        },
    }


def _configure(client: TestClient, project_id: str) -> None:
    response = client.post(
        f"/api/projects/{project_id}/workbench/plugins/example.native_secrets/env",
        json={"name": SECRET_NAME, "value": SECRET_VALUE},
    )
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("kind", ["row", "batch", "callable", "create_sheet"])
def test_missing_secret_refuses_before_any_native_publication(installed, kind):
    _client, project, project_id, sheet_id = installed
    before = {
        table: project.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in ("sheets", "columns", "runs", "results", "receipts")
    }
    result = run_action_spec(project, _requests(sheet_id)[kind], project_id=project_id)
    assert result.status == "failed"
    assert SECRET_NAME in result.errors[0].message
    assert SECRET_VALUE not in result.model_dump_json()
    after = {
        table: project.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in before
    }
    assert after == before


def test_configured_secret_reaches_every_native_host_without_ambient_mutation(
    installed,
):
    client, project, project_id, sheet_id = installed
    before_env = dict(os.environ)
    _configure(client, project_id)
    results = [
        run_action_spec(project, request, project_id=project_id)
        for request in _requests(sheet_id).values()
    ]
    assert all(result.status == "completed" for result in results), results
    assert all(SECRET_VALUE not in result.model_dump_json() for result in results)
    assert os.environ == before_env

    from frisket.authoring.workbench.installed_actions import resolve_installed_action

    action, _ = resolve_installed_action(project, "example.native_secrets.callable")
    observed = action.definition.run.handler.__globals__["observed"]
    assert observed == {
        "map_rows": [SECRET_VALUE],
        "map_batch": [SECRET_VALUE],
        "callable": [SECRET_VALUE],
        "create_sheet": [SECRET_VALUE],
    }


def test_validate_params_discovers_native_secret_backed_dynamic_table(installed):
    client, _project, project_id, _sheet_id = installed
    _configure(client, project_id)
    request = {
        "action_id": "example.native_secrets.dynamic_table",
        "scope": {"kind": "project"},
        "params": {},
        "sheet_name": "Dynamic secret table",
        "idempotency_key": "dynamic-secret-table",
    }
    response = client.post(
        f"/api/projects/{project_id}/actions/v1/validate-params",
        json={"action": request},
    )
    assert response.status_code == 200, response.text
    assert response.json()["diagnostics"] == {}
    assert response.json()["logical_outputs"] == [
        {"key": "configured", "column_type": "text"}
    ]


def _binding(plugin_id: str) -> RuntimeBindingSpec:
    return RuntimeBindingSpec(
        kind=f"{plugin_id}.action",
        handler_key=f"{plugin_id}:action",
        plugin=plugin_id,
        binding_type="actions",
        handler_api="plugin_typed_action_native",
        metadata={"requires_secrets": [SECRET_NAME]},
    )


def _store_secret(project: Project, plugin_id: str, value: str) -> None:
    project.db.execute(
        "INSERT INTO workbench_plugin_env_vars "
        "(plugin_id, name, encrypted, hint, updated_at) "
        "VALUES (?, ?, ?, '', datetime('now'))",
        (plugin_id, SECRET_NAME, encrypt_secret(value)),
    )
    project.db.commit()


def test_secret_lookup_is_scoped_and_refuses_undeclared_names(tmp_path, monkeypatch):
    monkeypatch.setenv("UNDECLARED_API_KEY", "ambient-value")
    projects = [Project.create(tmp_path / name) for name in ("one", "two")]
    try:
        _store_secret(projects[0], "plugin.one", "one-a")
        _store_secret(projects[0], "plugin.two", "one-b")
        _store_secret(projects[1], "plugin.one", "two-a")
        assert (
            HostPluginSecrets.from_binding(projects[0], _binding("plugin.one")).require(
                SECRET_NAME
            )
            == "one-a"
        )
        assert (
            HostPluginSecrets.from_binding(projects[0], _binding("plugin.two")).require(
                SECRET_NAME
            )
            == "one-b"
        )
        secrets = HostPluginSecrets.from_binding(projects[1], _binding("plugin.one"))
        assert secrets.require(SECRET_NAME) == "two-a"
        with pytest.raises(ValueError, match="not declared"):
            secrets.require("UNDECLARED_API_KEY")
        assert "two-a" not in repr(secrets)
        assert os.environ["UNDECLARED_API_KEY"] == "ambient-value"
    finally:
        for project in projects:
            project.close()


@pytest.mark.parametrize("requested", ["demo_api_key", " DeMo_ApI_KeY "])
def test_secret_lookup_normalizes_requested_name(tmp_path, requested):
    project = Project.create(tmp_path / "project")
    try:
        _store_secret(project, "plugin.one", SECRET_VALUE)
        secrets = HostPluginSecrets.from_binding(project, _binding("plugin.one"))
        assert secrets.require(requested) == SECRET_VALUE
    finally:
        project.close()
