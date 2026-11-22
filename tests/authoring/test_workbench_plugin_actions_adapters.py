"""Workbench plugin action catalog, scalar params, shadowing, generated map results, runtime adapters, and rowset table ops."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from plugin_clean_cutover_assertions import (
    assert_internal_shadow_manifest_field_rejected,
    assert_op_declaration_materialization_rejected,
    assert_public_action_collision_rejected,
)
from workbench_runtime_test_helpers import activate_runtime_plugin_for_project

from frisket.actions.system import validate_root_action
from frisket.authoring.plugin_registry import (
    _reset_default_registry_for_tests,
    default_registry,
    register_trusted_backend_handler,
    unregister_trusted_backend_handler,
)
from frisket.authoring.project_ask_actions import prepare_validated_project_ask_draft
from frisket.contracts.action import (
    CURRENT_ACTION_AUTHORING_CONTRACT_VERSION,
    ActionResult,
)
from frisket.engine.executor.actions import run_action_spec
from frisket.engine.jobs import Worker
from frisket.engine.projections.runtime import (
    ProjectionRuntimeError,
    projection_runtime_binding,
    runtime_projection_build,
    runtime_projection_status,
)
from frisket.engine.store import Project
from frisket.engine.store.value_codec import decode_stored_value
from frisket.querysets import SheetRowSetError, resolve_sheet_filter_rows
from frisket.server.app import create_app

# --- action catalog ---

TRUSTED_LOCAL_BACKEND_CAPABILITY = "plugin:trusted_local_backend"
ACTION_CATALOG_PLUGIN_ID = "demo.receipt_stamp"
ACTION_CATALOG_ACTION_KIND = "demo.receipt_stamp.stamp"
ACTION_CATALOG_PLUGIN_ROOT = (
    Path(__file__).parent.parent / "fixtures" / "local_plugins" / "demo_receipt_stamp"
)


@pytest.fixture(autouse=True)
def reset_registry() -> None:
    _reset_default_registry_for_tests()
    yield
    _reset_default_registry_for_tests()


def _catalog_kinds(client: TestClient, path: str) -> set[str]:
    response = client.get(path)
    assert response.status_code == 200, response.text
    return {str(entry["kind"]) for entry in response.json()["actions"]}


def _install_and_enable(client: TestClient, project_id: str) -> str:
    installed = client.post(
        f"/api/projects/{project_id}/workbench/plugins/{ACTION_CATALOG_PLUGIN_ID}/install-local",
        json={
            "source": {"kind": "localPath", "value": str(ACTION_CATALOG_PLUGIN_ROOT)},
            "arbitraryPackageLoadAllowed": False,
        },
    )
    assert installed.status_code == 200, installed.text
    receipt_id = str(installed.json()["receiptId"])
    activated = client.post(
        f"/api/projects/{project_id}/workbench/plugins/{ACTION_CATALOG_PLUGIN_ID}/activate",
        json={
            "receiptId": receipt_id,
            "trustAcknowledged": True,
            "permissionsAccepted": [TRUSTED_LOCAL_BACKEND_CAPABILITY],
            "arbitraryPackageLoadAllowed": False,
        },
    )
    assert activated.status_code == 200, activated.text
    return receipt_id


def _activate_backend(client: TestClient, project_id: str) -> dict[str, Any]:
    response = client.post(
        f"/api/projects/{project_id}/workbench/plugins/{ACTION_CATALOG_PLUGIN_ID}/backend/activate",
        json={
            "trustAcknowledged": True,
            "arbitraryPackageLoadAllowed": False,
            "executableHandlersAllowed": True,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_project_action_catalog_merges_enabled_plugin_actions_only(
    client,
) -> None:
    """An installed Action reaches exactly the project that admitted it.

    The published entry is the Action's own canonical catalog entry — the same
    projection a builtin publishes — so there is no plugin-shaped hint dialect
    (`form_params`, `default_outputs`) beside it: every parameter is described
    by `input_schema`, and the outputs by `ui_hints.logical_outputs`.
    """
    project_a = client.post(
        "/api/projects", json={"name": "Plugin action catalog A"}
    ).json()["id"]
    project_b = client.post(
        "/api/projects", json={"name": "Plugin action catalog B"}
    ).json()["id"]

    assert ACTION_CATALOG_ACTION_KIND not in _catalog_kinds(
        client, "/api/actions/v1/catalog"
    )
    assert ACTION_CATALOG_ACTION_KIND not in _catalog_kinds(
        client, f"/api/projects/{project_a}/actions/v1/catalog"
    )

    _install_and_enable(client, project_a)
    assert ACTION_CATALOG_ACTION_KIND not in _catalog_kinds(
        client, f"/api/projects/{project_a}/actions/v1/catalog"
    )

    activation = _activate_backend(client, project_a)
    assert activation["registeredRuntimeBindings"]["actions"] == [
        ACTION_CATALOG_ACTION_KIND
    ]

    response = client.get(f"/api/projects/{project_a}/actions/v1/catalog")
    assert response.status_code == 200, response.text
    catalog = response.json()
    entry = next(
        item
        for item in catalog["actions"]
        if item["kind"] == ACTION_CATALOG_ACTION_KIND
    )
    assert entry["authoring_contract_version"] == (
        CURRENT_ACTION_AUTHORING_CONTRACT_VERSION
    )
    assert entry["title"] == "Stamp receipt proof"
    assert entry["receipt_policy"] == "writes_receipt"
    # Installation, not the catalog entry, is where the trusted-local grant is
    # spent: the Action itself asks only for what any row-writing builtin asks.
    assert entry["required_capabilities"] == ["project:write"]
    assert entry["ui_hints"]["form"] == "generated"
    assert entry["ui_hints"]["category"] == "text"
    assert list(entry["input_schema"]["properties"]) == ["name"]
    assert entry["ui_hints"]["semantic_controls"] == {"name": "column"}
    assert entry["ui_hints"]["source_requirements"] == [
        {
            "id": "name",
            "param": "name",
            "label": "Name",
            "accepted_column_types": ["text"],
            "mode": "column",
            "min": 1,
        }
    ]
    assert entry["ui_hints"]["logical_outputs"] == [
        {"key": "receipt_stamp", "column_type": "text"}
    ]
    assert "form_params" not in entry["ui_hints"]
    assert "default_outputs" not in entry["ui_hints"]

    assert "plugin.load" in {item["kind"] for item in catalog["actions"]}
    project = client.app.state.workspace.get(project_a)
    sheet_id = project.add_sheet("Receipts")
    project.add_column(sheet_id, "Name")
    prepared = prepare_validated_project_ask_draft(
        project,
        {
            "action_id": ACTION_CATALOG_ACTION_KIND,
            "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
            "params": {"name": "Name"},
            "output_names": {},
        },
        catalog_payload=catalog,
    )
    assert prepared.draft["action_id"] == ACTION_CATALOG_ACTION_KIND
    identity = prepared.implementation_identity
    assert identity is not None
    assert {
        key: identity[key] for key in ("plugin_id", "handler_key", "module_path")
    } == {
        "plugin_id": ACTION_CATALOG_PLUGIN_ID,
        "handler_key": f"{ACTION_CATALOG_PLUGIN_ID}:stamp",
        "module_path": "plugin.py",
    }
    assert ACTION_CATALOG_ACTION_KIND not in _catalog_kinds(
        client, f"/api/projects/{project_b}/actions/v1/catalog"
    )
    assert ACTION_CATALOG_ACTION_KIND not in _catalog_kinds(
        client, "/api/actions/v1/catalog"
    )


# --- scalar params ---

SCALAR_PARAMS_PLUGIN_ID = "demo.scalar_params"
FORMAT_ACTION_KIND = "demo.scalar_params.format_text"
COMBINE_ACTION_KIND = "demo.scalar_params.combine_columns"
TEMPLATE_ACTION_KIND = "demo.scalar_params.template_sources"
FIXTURE_ROOT = Path(__file__).parent.parent / "fixtures" / "local_plugins"
SCALAR_PARAMS_PLUGIN_ROOT = FIXTURE_ROOT / "demo_scalar_params"


def _client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(tmp_path / "workspace"))


def _project_with_rows(client: TestClient) -> tuple[str, int, list[int]]:
    project_id = client.post("/api/projects", json={"name": "Scalar params"}).json()[
        "id"
    ]
    imported = client.post(
        f"/api/projects/{project_id}/import/csv",
        files={
            "file": (
                "rows.csv",
                "text,extra,city,state\nalpha,one,Boston,MA\nbeta,two,Austin,TX\n",
                "text/csv",
            )
        },
    )
    assert imported.status_code == 200, imported.text
    sheet_id = int(imported.json()["sheet_id"])
    data = _scalar_params_sheet_data(client, project_id, sheet_id)
    return project_id, sheet_id, [int(row["id"]) for row in data["rows"]]


def _scalar_params_install_activate_backend(
    client: TestClient, project_id: str
) -> None:
    installed = client.post(
        f"/api/projects/{project_id}/workbench/plugins/{SCALAR_PARAMS_PLUGIN_ID}/install-local",
        json={
            "source": {"kind": "localPath", "value": str(SCALAR_PARAMS_PLUGIN_ROOT)},
            "arbitraryPackageLoadAllowed": False,
        },
    )
    assert installed.status_code == 200, installed.text
    receipt_id = installed.json()["receiptId"]

    activated = client.post(
        f"/api/projects/{project_id}/workbench/plugins/{SCALAR_PARAMS_PLUGIN_ID}/activate",
        json={
            "receiptId": receipt_id,
            "trustAcknowledged": True,
            "permissionsAccepted": [TRUSTED_LOCAL_BACKEND_CAPABILITY],
            "arbitraryPackageLoadAllowed": False,
        },
    )
    assert activated.status_code == 200, activated.text

    backend = client.post(
        f"/api/projects/{project_id}/workbench/plugins/{SCALAR_PARAMS_PLUGIN_ID}/backend/activate",
        json={
            "trustAcknowledged": True,
            "arbitraryPackageLoadAllowed": False,
            "executableHandlersAllowed": True,
        },
    )
    assert backend.status_code == 200, backend.text
    assert set(backend.json()["registeredRuntimeBindings"]["actions"]) == {
        FORMAT_ACTION_KIND,
        COMBINE_ACTION_KIND,
        TEMPLATE_ACTION_KIND,
    }


def _run_action(
    client: TestClient,
    project_id: str,
    *,
    kind: str,
    sheet_id: int,
    row_ids: list[int] | None = None,
    params: dict[str, Any],
    key: str,
    expected_status: int = 200,
) -> dict[str, Any]:
    """An installed Action is an ordinary Action, so it takes the ordinary
    request envelope: selection lives in `scope`, never in `params`."""
    scope: dict[str, Any] = {"kind": "sheet_rows", "sheet_id": sheet_id}
    if row_ids is not None:
        scope["row_ids"] = row_ids
    response = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json={
            "action_id": kind,
            "scope": scope,
            "params": params,
            "idempotency_key": key,
        },
    )
    assert response.status_code == expected_status, response.text
    return response.json()


def _scalar_params_sheet_data(
    client: TestClient,
    project_id: str,
    sheet_id: int,
) -> dict[str, Any]:
    response = client.get(
        f"/api/projects/{project_id}/sheets/{sheet_id}/data?offset=0&limit=20"
    )
    assert response.status_code == 200, response.text
    return response.json()


def _cell_values(
    client: TestClient,
    project_id: str,
    sheet_id: int,
    column_name: str,
) -> dict[int, Any]:
    data = _scalar_params_sheet_data(client, project_id, sheet_id)
    column = next(item for item in data["columns"] if item["name"] == column_name)
    column_id = str(column["id"])
    return {int(row["id"]): row["cells"].get(column_id) for row in data["rows"]}


def _drain_queue(client: TestClient, *, worker_id: str = "scalar-param-worker") -> None:
    ws = client.app.state.workspace
    worker = Worker(ws.queue, ws.registry, worker_id=worker_id, poll_interval=0.01)
    while worker.run_once():
        pass


def _receipt(client: TestClient, project_id: str, receipt_id: str) -> dict[str, Any]:
    response = client.get(
        f"/api/projects/{project_id}/actions/v1/receipts/{receipt_id}"
    )
    assert response.status_code == 200, response.text
    return response.json()


def _catalog_entry(
    client: TestClient,
    project_id: str,
    kind: str,
) -> dict[str, Any]:
    response = client.get(f"/api/projects/{project_id}/actions/v1/catalog")
    assert response.status_code == 200, response.text
    return next(item for item in response.json()["actions"] if item["kind"] == kind)


def test_scalar_params_reach_inline_child_without_control_fields(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    project_id, sheet_id, row_ids = _project_with_rows(client)
    _scalar_params_install_activate_backend(client, project_id)

    result = _run_action(
        client,
        project_id,
        kind=FORMAT_ACTION_KIND,
        sheet_id=sheet_id,
        row_ids=[row_ids[0]],
        params={
            "text": "text",
            "prefix": "pre",
            "uppercase": True,
            "repeat": 2,
        },
        key="scalar-inline",
    )

    assert result["status"] == "completed"
    values = _cell_values(client, project_id, sheet_id, "formatted")
    assert values[row_ids[0]] == "pre:ALPHA,ALPHA:control=none"
    assert values[row_ids[1]] is None


def test_catalog_keeps_scalar_params_out_of_source_requirements(tmp_path: Path) -> None:
    """Scalar knobs and source columns are declared in one Params model, and
    the catalog still separates them: only the source-shaped fields become
    source_requirements, while every field (scalar included) is described by
    the input schema, which is the single authority."""
    client = _client(tmp_path)
    project_id, _sheet_id, _row_ids = _project_with_rows(client)
    _scalar_params_install_activate_backend(client, project_id)

    format_entry = _catalog_entry(client, project_id, FORMAT_ACTION_KIND)
    assert sorted(format_entry["input_schema"]["properties"]) == [
        "prefix",
        "repeat",
        "text",
        "uppercase",
    ]
    assert format_entry["ui_hints"]["semantic_controls"] == {"text": "column"}
    assert format_entry["ui_hints"]["source_requirements"] == [
        {
            "id": "text",
            "param": "text",
            "label": "Text",
            "accepted_column_types": ["text"],
            "mode": "column",
            "min": 1,
        }
    ]

    combine_entry = _catalog_entry(client, project_id, COMBINE_ACTION_KIND)
    assert combine_entry["ui_hints"]["semantic_controls"] == {"sources": "columns"}
    assert combine_entry["ui_hints"]["source_requirements"][0] == {
        "id": "sources",
        "param": "sources",
        "label": "Sources",
        "accepted_column_types": ["text"],
        "mode": "columns",
        "min": 2,
        "max": 2,
    }
    assert combine_entry["input_schema"]["properties"]["sources"]["maxItems"] == 2

    template_entry = _catalog_entry(client, project_id, TEMPLATE_ACTION_KIND)
    assert list(template_entry["input_schema"]["properties"]) == ["template"]
    assert template_entry["ui_hints"]["semantic_controls"] == {"template": "template"}
    assert template_entry["ui_hints"]["source_requirements"] == [
        {
            "id": "template",
            "param": "template",
            "label": "Template",
            "mode": "template",
            "min": 0,
            "template_columns": "union",
        }
    ]


def test_many_and_template_source_columns_reach_child(tmp_path: Path) -> None:
    client = _client(tmp_path)
    project_id, sheet_id, row_ids = _project_with_rows(client)
    _scalar_params_install_activate_backend(client, project_id)

    combined = _run_action(
        client,
        project_id,
        kind=COMBINE_ACTION_KIND,
        sheet_id=sheet_id,
        row_ids=[row_ids[0]],
        params={
            "sources": ["text", "extra"],
            "separator": "|",
        },
        key="combine-columns",
    )
    assert combined["status"] == "completed"
    assert _cell_values(client, project_id, sheet_id, "combined")[row_ids[0]] == (
        "alpha|one"
    )
    # Provenance is the request's own params now, not a separate
    # plugin_action_source_bindings evidence fact: the selected columns ARE
    # the typed parameter, so the receipt records them with the action.
    combined_receipt = _receipt(client, project_id, str(combined["receipt_id"]))
    assert combined_receipt["action_kind"] == COMBINE_ACTION_KIND
    assert combined_receipt["status"] == "completed"

    templated = _run_action(
        client,
        project_id,
        kind=TEMPLATE_ACTION_KIND,
        sheet_id=sheet_id,
        row_ids=[row_ids[1]],
        params={
            "template": {"text": "{{city}}/{{state}}"},
        },
        key="template-sources",
    )
    assert templated["status"] == "completed"
    assert (
        _cell_values(client, project_id, sheet_id, "template_summary")[row_ids[1]]
        == "{{city}}/{{state}} -> city=Austin;state=TX"
    )
    # The template's own text names the columns it read, and the summary above
    # proves both reached the handler in declaration order.
    templated_receipt = _receipt(client, project_id, str(templated["receipt_id"]))
    assert templated_receipt["action_kind"] == TEMPLATE_ACTION_KIND
    assert templated_receipt["status"] == "completed"


def test_non_scalar_param_is_rejected_before_the_handler_runs(tmp_path: Path) -> None:
    client = _client(tmp_path)
    project_id, sheet_id, _row_ids = _project_with_rows(client)
    _scalar_params_install_activate_backend(client, project_id)

    result = _run_action(
        client,
        project_id,
        kind=FORMAT_ACTION_KIND,
        sheet_id=sheet_id,
        params={
            "text": "text",
            "prefix": {"unsafe": "not-scalar"},
        },
        key="bad-scalar-param",
        expected_status=400,
    )

    assert result["status"] == "failed"
    assert result["errors"][0]["code"] == "invalid_params"
    assert result["errors"][0]["field"] == "params"
    assert result["errors"][0]["details"]["errors"][0]["loc"] == ["prefix"]


# --- action shadowing ---


def test_internal_shadow_manifest_field_is_no_longer_part_of_plugin_contract() -> None:
    assert_internal_shadow_manifest_field_rejected()


def test_public_action_kind_shadowing_fails_closed_for_plugins(tmp_path: Path) -> None:
    assert_public_action_collision_rejected(tmp_path)


# --- generated map ---


def test_generated_map_plugins_must_declare_plugin_owned_output_schema() -> None:
    assert_op_declaration_materialization_rejected()


# --- importer adapter ---

IMPORTER_ADAPTER_PLUGIN_ID = "frisket-runtime-demo"
IMPORTER_KIND = "frisket_runtime_demo_import"
IMPORTER_ADAPTER_HANDLER_KEY = f"{IMPORTER_ADAPTER_PLUGIN_ID}:importer"


def _runtime_import_action(
    *,
    importer_kind: str = IMPORTER_KIND,
    key: str = "runtime-import@sha256:v1",
) -> dict[str, Any]:
    return {
        "action_id": "import.runtime",
        "scope": {"kind": "project"},
        "sheet_name": "Runtime Import",
        "params": {
            "importer_kind": importer_kind,
            "source": {
                "kind": "runtime",
                "label": "runtime-demo.fixture",
                "fingerprint": "sha256:runtime-demo",
            },
            "handler_params": {"city": "New York"},
        },
        "idempotency_key": key,
    }


def test_trusted_importer_runtime_binding_validates_materializes_and_replays(
    tmp_path: Path,
    make_project,
) -> None:
    _reset_default_registry_for_tests()
    calls: list[dict[str, Any]] = []

    def trusted_importer_handler(payload: dict[str, Any]) -> dict[str, Any]:
        calls.append(payload)
        return {
            "schemaVersion": "frisket.runtime_importer_plan.v1",
            "columns": [
                {"name": "name", "type": "text"},
                {"name": "score", "type": "integer"},
            ],
            "rows": [
                {"name": payload["handlerParams"]["city"], "score": 9},
                {"name": "Boston", "score": 7},
            ],
            "warnings": ["runtime importer handled by trusted host binding"],
        }

    register_trusted_backend_handler(
        IMPORTER_ADAPTER_HANDLER_KEY, trusted_importer_handler
    )
    try:
        default_registry().register_runtime_binding(
            "importers",
            IMPORTER_KIND,
            handler_key=IMPORTER_ADAPTER_HANDLER_KEY,
            handler=trusted_importer_handler,
            plugin=IMPORTER_ADAPTER_PLUGIN_ID,
        )
        project = make_project("Runtime")
        activate_runtime_plugin_for_project(
            project,
            tmp_path,
            plugin_id=IMPORTER_ADAPTER_PLUGIN_ID,
            runtime_bindings={
                "importers": {IMPORTER_KIND: IMPORTER_ADAPTER_HANDLER_KEY}
            },
            project_id="runtime-import-project",
        )
        action = _runtime_import_action()

        validation = validate_root_action(action)
        assert validation.ok is True
        assert validation.action is not None
        assert validation.action.action_id == "import.runtime"

        result = run_action_spec(project, action, project_id="runtime-import-project")

        assert isinstance(result, ActionResult)
        assert result.status == "completed"
        assert result.action.kind == "import.runtime"
        assert result.receipt_id is not None
        assert len(calls) == 1
        assert calls[0]["schemaVersion"] == "frisket.runtime_importer_request.v1"
        assert calls[0]["projectId"] == "runtime-import-project"
        assert calls[0]["pluginId"] == IMPORTER_ADAPTER_PLUGIN_ID
        assert calls[0]["handlerKey"] == IMPORTER_ADAPTER_HANDLER_KEY
        assert calls[0]["importerKind"] == IMPORTER_KIND
        assert calls[0]["sheetName"] == "Runtime Import"
        assert calls[0]["source"]["fingerprint"] == "sha256:runtime-demo"
        assert calls[0]["handlerParams"] == {"city": "New York"}

        sheet = project.db.execute(
            "SELECT id, name FROM sheets WHERE name='Runtime Import'"
        ).fetchone()
        assert sheet is not None
        values = project.db.execute(
            """
            SELECT c.name AS column_name, cells.value_kind, cells.value
            FROM cells
            JOIN columns c ON c.id = cells.column_id
            JOIN rows r ON r.id = cells.row_id
            WHERE r.sheet_id=?
            ORDER BY r.position, c.position
            """,
            (sheet["id"],),
        ).fetchall()
        assert [
            (
                row["column_name"],
                decode_stored_value(row["value_kind"], row["value"]),
            )
            for row in values
        ] == [
            ("name", "New York"),
            ("score", 9),
            ("name", "Boston"),
            ("score", 7),
        ]

        row = project.db.execute(
            "SELECT action_kind, status, body FROM receipts WHERE id=?",
            (result.receipt_id,),
        ).fetchone()
        assert row is not None
        assert row["action_kind"] == "import.runtime"
        assert row["status"] == "completed"
        receipt = json.loads(row["body"])
        evidence_refs = [item["ref"] for item in receipt["inputs"]]
        runtime_ref = next(
            ref for ref in evidence_refs if ref["kind"] == "workbench_runtime_binding"
        )
        assert {
            key: runtime_ref[key]
            for key in (
                "kind",
                "schema_version",
                "binding_type",
                "binding_kind",
                "plugin",
                "handler_key",
            )
        } == {
            "kind": "workbench_runtime_binding",
            "schema_version": "frisket.workbench_runtime_binding_evidence.v1",
            "binding_type": "importers",
            "binding_kind": IMPORTER_KIND,
            "plugin": IMPORTER_ADAPTER_PLUGIN_ID,
            "handler_key": IMPORTER_ADAPTER_HANDLER_KEY,
        }

        replay = run_action_spec(project, action, project_id="runtime-import-project")
        assert replay.status == "completed"
        assert replay.receipt_id == result.receipt_id
        assert len(calls) == 1
    finally:
        unregister_trusted_backend_handler(IMPORTER_ADAPTER_HANDLER_KEY)
        _reset_default_registry_for_tests()


def test_unbound_runtime_importer_kind_stays_unsupported(project) -> None:
    _reset_default_registry_for_tests()
    try:
        action = _runtime_import_action(importer_kind="frisket_runtime_demo_unbound")
        validation = validate_root_action(action)
        assert (
            validation.ok is True
        )  # Binding trust is checked with the target project.

        result = run_action_spec(project, action, project_id="runtime-import-project")
        assert result.status == "failed"
        assert result.errors[0].code == "unsupported_runtime_importer"
    finally:
        _reset_default_registry_for_tests()


def test_runtime_importer_invalid_plan_does_not_write_rows(
    tmp_path: Path,
    project,
) -> None:
    _reset_default_registry_for_tests()

    def invalid_handler(_payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "schemaVersion": "frisket.runtime_importer_plan.v1",
            "columns": [{"name": "name", "type": "text"}],
            "rows": [{"other": "bad"}],
        }

    register_trusted_backend_handler(IMPORTER_ADAPTER_HANDLER_KEY, invalid_handler)
    try:
        default_registry().register_runtime_binding(
            "importers",
            IMPORTER_KIND,
            handler_key=IMPORTER_ADAPTER_HANDLER_KEY,
            handler=invalid_handler,
            plugin=IMPORTER_ADAPTER_PLUGIN_ID,
        )
        activate_runtime_plugin_for_project(
            project,
            tmp_path,
            plugin_id=IMPORTER_ADAPTER_PLUGIN_ID,
            runtime_bindings={
                "importers": {IMPORTER_KIND: IMPORTER_ADAPTER_HANDLER_KEY}
            },
            project_id="runtime-import-project",
        )

        result = run_action_spec(
            project,
            _runtime_import_action(key="runtime-import-bad@sha256:v1"),
            project_id="runtime-import-project",
        )

        assert result.status == "failed"
        assert result.errors[0].code == "plugin_importer_row_invalid"
        assert project.db.execute("SELECT COUNT(*) FROM sheets").fetchone()[0] == 0
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM receipts WHERE action_kind='import.runtime'"
            ).fetchone()[0]
            == 0
        )
    finally:
        unregister_trusted_backend_handler(IMPORTER_ADAPTER_HANDLER_KEY)
        _reset_default_registry_for_tests()


# --- operator adapter ---

OPERATOR_ADAPTER_PLUGIN_ID = "frisket-runtime-demo"
OPERATOR_KIND = "frisket.runtime_demo.operator"
OPERATOR_ADAPTER_HANDLER_KEY = f"{OPERATOR_ADAPTER_PLUGIN_ID}:operator"


def _filter(kind: str = OPERATOR_KIND, value: Any | None = None) -> str:
    return json.dumps({"status": {kind: value if value is not None else "done"}})


def _seed_project(project: Project) -> tuple[int, dict[str, int], dict[str, int]]:
    sheet_id = project.add_sheet("tasks")
    columns = {
        "title": project.add_column(sheet_id, "title"),
        "status": project.add_column(sheet_id, "status"),
    }
    row_ids = project.add_rows(
        sheet_id,
        [
            {"title": "A start", "status": "todo"},
            {"title": "B done", "status": "done"},
            {"title": "C done", "status": "done"},
        ],
        columns,
    )
    return (
        sheet_id,
        {"a": row_ids[0], "b": row_ids[1], "c": row_ids[2]},
        columns,
    )


def test_trusted_operator_runtime_binding_filters_rowset_and_grid(
    tmp_path: Path,
) -> None:
    _reset_default_registry_for_tests()
    calls: list[dict[str, Any]] = []

    def trusted_operator_handler(payload: dict[str, Any]) -> dict[str, Any]:
        calls.append(payload)
        project = payload["project"]
        values = project.get_values(
            payload["sheetId"],
            payload["column"]["id"],
            row_ids=payload["candidateRowIds"],
        )
        matched = [
            row_id
            for row_id, value in values.items()
            if str(value).lower() == str(payload["value"]).lower()
        ]
        return {
            "schemaVersion": "frisket.runtime_operator_plan.v1",
            "rowIds": matched,
        }

    register_trusted_backend_handler(
        OPERATOR_ADAPTER_HANDLER_KEY, trusted_operator_handler
    )
    try:
        default_registry().register_runtime_binding(
            "operators",
            OPERATOR_KIND,
            handler_key=OPERATOR_ADAPTER_HANDLER_KEY,
            handler=trusted_operator_handler,
            plugin=OPERATOR_ADAPTER_PLUGIN_ID,
        )
        client = TestClient(create_app(tmp_path / "ws"))
        pid = client.post("/api/projects", json={"name": "Runtime"}).json()["id"]
        project = client.app.state.workspace.get(pid)
        activate_runtime_plugin_for_project(
            project,
            tmp_path,
            plugin_id=OPERATOR_ADAPTER_PLUGIN_ID,
            runtime_bindings={
                "operators": {OPERATOR_KIND: OPERATOR_ADAPTER_HANDLER_KEY}
            },
            project_id=pid,
        )
        sheet_id, rows, columns = _seed_project(project)

        rowset = resolve_sheet_filter_rows(
            project,
            sheet_id,
            filter_=_filter(),
            limit=50,
        )

        assert rowset.row_ids == [rows["b"], rows["c"]]
        assert rowset.total == 2
        assert len(calls) == 1
        assert calls[0]["schemaVersion"] == "frisket.runtime_operator_request.v1"
        assert calls[0]["pluginId"] == OPERATOR_ADAPTER_PLUGIN_ID
        assert calls[0]["handlerKey"] == OPERATOR_ADAPTER_HANDLER_KEY
        assert calls[0]["operatorKind"] == OPERATOR_KIND
        assert calls[0]["sheetId"] == sheet_id
        assert calls[0]["column"]["id"] == columns["status"]
        assert calls[0]["column"]["name"] == "status"
        assert calls[0]["column"]["type"] == "text"
        assert calls[0]["value"] == "done"
        assert calls[0]["candidateRowIds"] == [rows["a"], rows["b"], rows["c"]]

        grid = client.get(
            f"/api/projects/{pid}/sheets/{sheet_id}/data",
            params={"filter": _filter()},
        )

        assert grid.status_code == 200, grid.text
        assert [row["id"] for row in grid.json()["rows"]] == rowset.row_ids
    finally:
        unregister_trusted_backend_handler(OPERATOR_ADAPTER_HANDLER_KEY)
        _reset_default_registry_for_tests()


def test_unbound_runtime_operator_stays_unsupported(project: Project) -> None:
    _reset_default_registry_for_tests()
    try:
        sheet_id, _rows, _columns = _seed_project(project)

        with pytest.raises(SheetRowSetError, match="unsupported filter"):
            resolve_sheet_filter_rows(
                project,
                sheet_id,
                filter_=_filter(kind="frisket.runtime_demo.unbound"),
            )
    finally:
        _reset_default_registry_for_tests()


def test_runtime_operator_handler_failures_fail_closed(
    tmp_path: Path, project: Project
) -> None:
    _reset_default_registry_for_tests()

    def invalid_handler(_payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "schemaVersion": "frisket.runtime_operator_plan.v1",
            "whereSql": "1=1",
        }

    register_trusted_backend_handler(OPERATOR_ADAPTER_HANDLER_KEY, invalid_handler)
    try:
        default_registry().register_runtime_binding(
            "operators",
            OPERATOR_KIND,
            handler_key=OPERATOR_ADAPTER_HANDLER_KEY,
            handler=invalid_handler,
            plugin=OPERATOR_ADAPTER_PLUGIN_ID,
        )
        activate_runtime_plugin_for_project(
            project,
            tmp_path,
            plugin_id=OPERATOR_ADAPTER_PLUGIN_ID,
            runtime_bindings={
                "operators": {OPERATOR_KIND: OPERATOR_ADAPTER_HANDLER_KEY}
            },
            project_id="bad-runtime-operator",
        )
        sheet_id, _rows, _columns = _seed_project(project)

        with pytest.raises(SheetRowSetError, match="invalid runtime operator plan"):
            resolve_sheet_filter_rows(project, sheet_id, filter_=_filter())
    finally:
        unregister_trusted_backend_handler(OPERATOR_ADAPTER_HANDLER_KEY)
        _reset_default_registry_for_tests()


# --- projection contract ---

PROJECTION_CONTRACT_PLUGIN_ID = "frisket-runtime-demo"
PROJECTION_KIND = "frisket.runtime_demo.projection"
PROJECTION_CONTRACT_HANDLER_KEY = f"{PROJECTION_CONTRACT_PLUGIN_ID}:projection"


def test_projection_runtime_contract_calls_trusted_status_and_build_handler(
    tmp_path: Path,
    project,
) -> None:
    _reset_default_registry_for_tests()
    calls: list[dict[str, Any]] = []

    def trusted_projection_handler(payload: dict[str, Any]) -> dict[str, Any]:
        calls.append(payload)
        if payload["schemaVersion"] == "frisket.runtime_projection_status_request.v1":
            return {
                "schemaVersion": "frisket.runtime_projection_status.v1",
                "status": "ready",
                "freshness": {
                    "state": "fresh",
                    "generation": "gen-1",
                    "transient": False,
                },
                "outputs": {
                    "artifactRefs": [
                        {
                            "kind": "projection_artifact",
                            "projectionKind": PROJECTION_KIND,
                            "artifactId": "projection://demo/gen-1",
                        }
                    ],
                    "metrics": {"validRows": 2},
                },
                "warnings": ["status handled by trusted host binding"],
            }
        return {
            "schemaVersion": "frisket.runtime_projection_build_plan.v1",
            "status": "accepted",
            "build": {
                "operation": "refresh",
                "idempotencyKey": "projection-build@sha256:v1",
            },
            "outputs": {
                "artifactRefs": [
                    {
                        "kind": "projection_artifact",
                        "projectionKind": PROJECTION_KIND,
                        "artifactId": "projection://demo/gen-2",
                    }
                ],
                "metrics": {"validRows": 3},
            },
        }

    register_trusted_backend_handler(
        PROJECTION_CONTRACT_HANDLER_KEY, trusted_projection_handler
    )
    try:
        default_registry().register_runtime_binding(
            "projections",
            PROJECTION_KIND,
            handler_key=PROJECTION_CONTRACT_HANDLER_KEY,
            handler=trusted_projection_handler,
            plugin=PROJECTION_CONTRACT_PLUGIN_ID,
        )
        activate_runtime_plugin_for_project(
            project,
            tmp_path,
            plugin_id=PROJECTION_CONTRACT_PLUGIN_ID,
            runtime_bindings={
                "projections": {PROJECTION_KIND: PROJECTION_CONTRACT_HANDLER_KEY}
            },
            project_id="runtime-projection-project",
        )

        binding = projection_runtime_binding(PROJECTION_KIND)
        assert binding.kind == PROJECTION_KIND
        assert binding.handler_key == PROJECTION_CONTRACT_HANDLER_KEY

        status = runtime_projection_status(
            project,
            projection_kind=PROJECTION_KIND,
            project_id="runtime-projection-project",
            target={"sheetId": 7, "columnId": 11},
            params={"bbox": [-74, 40, -73, 41]},
        )
        assert status.schema_version == "frisket.runtime_projection_status.v1"
        assert status.status == "ready"
        assert status.freshness.generation == "gen-1"
        assert status.outputs.metrics == {"validRows": 2}
        assert status.warnings == ["status handled by trusted host binding"]

        build = runtime_projection_build(
            project,
            projection_kind=PROJECTION_KIND,
            project_id="runtime-projection-project",
            target={"sheetId": 7, "columnId": 11},
            params={"bbox": [-74, 40, -73, 41]},
            mode="refresh",
        )
        assert build.schema_version == "frisket.runtime_projection_build_plan.v1"
        assert build.status == "accepted"
        assert build.build.operation == "refresh"
        assert build.build.idempotency_key == "projection-build@sha256:v1"

        assert [call["schemaVersion"] for call in calls] == [
            "frisket.runtime_projection_status_request.v1",
            "frisket.runtime_projection_build_request.v1",
        ]
        assert calls[0]["projectId"] == "runtime-projection-project"
        assert calls[0]["pluginId"] == PROJECTION_CONTRACT_PLUGIN_ID
        assert calls[0]["handlerKey"] == PROJECTION_CONTRACT_HANDLER_KEY
        assert calls[0]["projectionKind"] == PROJECTION_KIND
        assert calls[0]["target"] == {"sheetId": 7, "columnId": 11}
        assert calls[0]["params"] == {"bbox": [-74, 40, -73, 41]}
        assert calls[1]["mode"] == "refresh"
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM receipts WHERE action_kind!='plugin.load'"
            ).fetchone()[0]
            == 0
        )
        assert project.db.execute("SELECT COUNT(*) FROM sheets").fetchone()[0] == 0
    finally:
        unregister_trusted_backend_handler(PROJECTION_CONTRACT_HANDLER_KEY)
        _reset_default_registry_for_tests()


def test_projection_runtime_contract_fails_closed_for_unbound_kind(
    make_project,
) -> None:
    _reset_default_registry_for_tests()
    project = make_project("unbound-projection")

    assert projection_runtime_binding("frisket.runtime_demo.unbound") is None

    try:
        runtime_projection_status(
            project,
            projection_kind="frisket.runtime_demo.unbound",
            project_id="runtime-projection-project",
            target={"sheetId": 7},
            params={},
        )
    except ProjectionRuntimeError as exc:
        assert exc.code == "unsupported_runtime_projection"
    else:
        raise AssertionError("unbound projection kind should fail closed")


def test_projection_runtime_contract_fails_closed_without_handler(
    project,
) -> None:
    _reset_default_registry_for_tests()
    try:
        default_registry().register_runtime_binding(
            "projections",
            PROJECTION_KIND,
            handler_key=PROJECTION_CONTRACT_HANDLER_KEY,
            handler=None,
            plugin=PROJECTION_CONTRACT_PLUGIN_ID,
        )

        assert projection_runtime_binding(PROJECTION_KIND) is None

        try:
            runtime_projection_build(
                project,
                projection_kind=PROJECTION_KIND,
                project_id="runtime-projection-project",
                target={"sheetId": 7},
                params={},
                mode="refresh",
            )
        except ProjectionRuntimeError as exc:
            assert exc.code == "unsupported_runtime_projection"
        else:
            raise AssertionError("missing projection handler should fail closed")
    finally:
        _reset_default_registry_for_tests()


def test_projection_runtime_contract_rejects_invalid_handler_response(
    tmp_path: Path,
    project,
) -> None:
    _reset_default_registry_for_tests()

    def invalid_handler(_payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "schemaVersion": "frisket.runtime_projection_status.v1",
            "status": "ready",
            "outputs": {"rawSql": "SELECT * FROM rows"},
        }

    register_trusted_backend_handler(PROJECTION_CONTRACT_HANDLER_KEY, invalid_handler)
    try:
        default_registry().register_runtime_binding(
            "projections",
            PROJECTION_KIND,
            handler_key=PROJECTION_CONTRACT_HANDLER_KEY,
            handler=invalid_handler,
            plugin=PROJECTION_CONTRACT_PLUGIN_ID,
        )
        activate_runtime_plugin_for_project(
            project,
            tmp_path,
            plugin_id=PROJECTION_CONTRACT_PLUGIN_ID,
            runtime_bindings={
                "projections": {PROJECTION_KIND: PROJECTION_CONTRACT_HANDLER_KEY}
            },
            project_id="runtime-projection-project",
        )

        try:
            runtime_projection_status(
                project,
                projection_kind=PROJECTION_KIND,
                project_id="runtime-projection-project",
                target={"sheetId": 7},
                params={},
            )
        except ProjectionRuntimeError as exc:
            assert exc.code == "invalid_runtime_projection_status"
        else:
            raise AssertionError("invalid projection status should fail closed")

        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM receipts WHERE action_kind!='plugin.load'"
            ).fetchone()[0]
            == 0
        )
        assert project.db.execute("SELECT COUNT(*) FROM sheets").fetchone()[0] == 0
    finally:
        unregister_trusted_backend_handler(PROJECTION_CONTRACT_HANDLER_KEY)
        _reset_default_registry_for_tests()


# --- rowset table op ---

ROWSET_TABLE_OP_PLUGIN_ID = "demo.star_summary"
ROWSET_TABLE_OP_ACTION_KIND = "demo.star_summary.summarize_stars"
ROWSET_TABLE_OP_PLUGIN_ROOT = FIXTURE_ROOT / "demo_star_summary"
SUMMARY_COLUMNS = [
    "constellation",
    "star_count",
    "average_rating",
    "top_star",
    "source_row_count",
]


def _project_with_stars(
    client: TestClient,
    *,
    count: int = 130,
    group_count: int = 4,
) -> tuple[str, int, list[int], list[dict[str, Any]]]:
    project_id = client.post("/api/projects", json={"name": "Star summary"}).json()[
        "id"
    ]
    rows = _star_rows(count, group_count=group_count)
    csv = "\n".join(
        [
            "star,constellation,rating",
            *[
                f'"{row["star"]}","{row["constellation"]}",{row["rating"]}'
                for row in rows
            ],
        ]
    )
    imported = client.post(
        f"/api/projects/{project_id}/import/csv",
        files={"file": ("stars.csv", csv, "text/csv")},
    )
    assert imported.status_code == 200, imported.text
    sheet_id = int(imported.json()["sheet_id"])
    data = _rowset_table_op_sheet_data(client, project_id, sheet_id, limit=count)
    row_ids = [int(row["id"]) for row in data["rows"]]
    assert len(row_ids) == count
    return project_id, sheet_id, row_ids, rows


def _star_rows(count: int, *, group_count: int = 4) -> list[dict[str, Any]]:
    named = ["Lyra", "Orion", "Cygnus", "Draco"]
    names = (
        named[:group_count]
        if group_count <= len(named)
        else [f"Constellation {index + 1:04d}" for index in range(group_count)]
    )
    base = [4.9, 3.4, 4.2, 2.6]
    return [
        {
            "star": f"Star {index + 1:03d}",
            "constellation": names[index % len(names)],
            "rating": base[index % len(base)] + (index % 3) * 0.1,
        }
        for index in range(count)
    ]


def _expected_summary(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row["constellation"]), []).append(row)
    out: dict[str, dict[str, Any]] = {}
    for constellation, items in grouped.items():
        top = sorted(items, key=lambda item: (-float(item["rating"]), item["star"]))[0]
        average = round(
            sum(float(item["rating"]) for item in items) / len(items),
            2,
        )
        out[constellation] = {
            "star_count": len(items),
            "average_rating": average,
            "top_star": top["star"],
            "source_row_count": len(items),
        }
    return out


def _rowset_table_op_install_activate_backend(
    client: TestClient, project_id: str
) -> None:
    installed = client.post(
        f"/api/projects/{project_id}/workbench/plugins/{ROWSET_TABLE_OP_PLUGIN_ID}/install-local",
        json={
            "source": {"kind": "localPath", "value": str(ROWSET_TABLE_OP_PLUGIN_ROOT)},
            "arbitraryPackageLoadAllowed": False,
        },
    )
    assert installed.status_code == 200, installed.text
    receipt_id = installed.json()["receiptId"]

    activated = client.post(
        f"/api/projects/{project_id}/workbench/plugins/{ROWSET_TABLE_OP_PLUGIN_ID}/activate",
        json={
            "receiptId": receipt_id,
            "trustAcknowledged": True,
            "permissionsAccepted": [TRUSTED_LOCAL_BACKEND_CAPABILITY],
            "arbitraryPackageLoadAllowed": False,
        },
    )
    assert activated.status_code == 200, activated.text

    backend = client.post(
        f"/api/projects/{project_id}/workbench/plugins/{ROWSET_TABLE_OP_PLUGIN_ID}/backend/activate",
        json={
            "trustAcknowledged": True,
            "arbitraryPackageLoadAllowed": False,
            "executableHandlersAllowed": True,
        },
    )
    assert backend.status_code == 200, backend.text
    assert backend.json()["registeredRuntimeBindings"]["actions"] == [
        ROWSET_TABLE_OP_ACTION_KIND
    ]


def _rowset_action(
    *,
    sheet_id: int,
    key: str,
    target_sheet_name: str = "Star summary",
    row_ids: list[int] | None = None,
    extra_params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Selection lives in the request scope, and the child sheet's name is a
    request field. Params declare only which columns to read."""
    scope: dict[str, Any] = {"kind": "sheet_rows", "sheet_id": sheet_id}
    if row_ids is not None:
        scope["row_ids"] = row_ids
    return {
        "action_id": ROWSET_TABLE_OP_ACTION_KIND,
        "scope": scope,
        "params": {
            "group_by": "constellation",
            "value_column": "rating",
            "label_column": "star",
            **(extra_params or {}),
        },
        "sheet_name": target_sheet_name,
        "idempotency_key": key,
    }


def _run_rowset_action(
    client: TestClient,
    project_id: str,
    *,
    sheet_id: int,
    key: str,
    target_sheet_name: str = "Star summary",
    row_ids: list[int] | None = None,
    extra_params: dict[str, Any] | None = None,
    expected_status: int = 200,
) -> dict[str, Any]:
    response = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json=_rowset_action(
            sheet_id=sheet_id,
            key=key,
            target_sheet_name=target_sheet_name,
            row_ids=row_ids,
            extra_params=extra_params,
        ),
    )
    assert response.status_code == expected_status, response.text
    return response.json()


def _rowset_table_op_sheet_data(
    client: TestClient,
    project_id: str,
    sheet_id: int,
    *,
    limit: int = 100,
) -> dict[str, Any]:
    response = client.get(
        f"/api/projects/{project_id}/sheets/{sheet_id}/data?offset=0&limit={limit}"
    )
    assert response.status_code == 200, response.text
    return response.json()


def _child_sheet(client: TestClient, project_id: str, name: str) -> dict[str, Any]:
    response = client.get(f"/api/projects/{project_id}/sheets")
    assert response.status_code == 200, response.text
    sheets = response.json()
    if isinstance(sheets, dict):
        sheets = sheets["sheets"]
    for sheet in sheets:
        if sheet["name"] == name:
            return sheet
    raise AssertionError(f"child sheet not found: {name}")


def _ref(receipt: dict[str, Any], section: str, kind: str) -> dict[str, Any]:
    return next(item["ref"] for item in receipt[section] if item["ref"]["kind"] == kind)


def _recorded_sources(receipt: dict[str, Any]) -> dict[int, set[int]]:
    """Contributor lineage as the host recorded it: one ``materialized_row_sources``
    membership row per (summary row, source row) pair, role ``aggregate_source``."""
    membership = _ref(receipt, "records", "materialized_row_sources")
    assert membership["row_count"] == len(membership["rows"])
    out: dict[int, set[int]] = {}
    for row in membership["rows"]:
        assert row["role"] == "aggregate_source", row
        out.setdefault(int(row["materialized_row_id"]), set()).add(
            int(row["source_row_id"])
        )
    return out


def _values_by_column(data: dict[str, Any]) -> dict[str, dict[int, Any]]:
    columns = {int(column["id"]): str(column["name"]) for column in data["columns"]}
    out = {name: {} for name in columns.values()}
    for row in data["rows"]:
        for column_id, value in row["cells"].items():
            out[columns[int(column_id)]][int(row["id"])] = value
    return out


def test_rowset_plugin_materializes_selected_child_sheet_with_membership(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    project_id, sheet_id, row_ids, rows = _project_with_stars(client, count=12)
    _rowset_table_op_install_activate_backend(client, project_id)
    selected_indexes = [0, 1, 4, 5, 8, 9]
    selected_row_ids = [row_ids[index] for index in selected_indexes]
    selected_rows = [rows[index] for index in selected_indexes]

    result = _run_rowset_action(
        client,
        project_id,
        sheet_id=sheet_id,
        key="selected@rowset",
        target_sheet_name="Selected star summary",
        row_ids=selected_row_ids,
    )

    assert result["status"] == "completed"
    child = _child_sheet(client, project_id, "Selected star summary")
    assert child["parent_sheet_id"] == sheet_id
    data = _rowset_table_op_sheet_data(client, project_id, int(child["id"]))
    values = _values_by_column(data)
    assert set(values) >= set(SUMMARY_COLUMNS)
    expected = _expected_summary(selected_rows)
    actual_groups = {
        str(group): row_id for row_id, group in values["constellation"].items()
    }
    assert set(actual_groups) == set(expected)
    for constellation, expected_row in expected.items():
        row_id = actual_groups[constellation]
        assert values["star_count"][row_id] == expected_row["star_count"]
        assert values["top_star"][row_id] == expected_row["top_star"]
        assert values["source_row_count"][row_id] == expected_row["source_row_count"]
        assert (
            abs(
                float(values["average_rating"][row_id])
                - float(expected_row["average_rating"])
            )
            <= 0.01
        )

    receipt = _receipt(client, project_id, result["receipt_id"])
    assert receipt["status"] == "completed"

    # The child sheet, its columns and its rows are all published as refs.
    sheet_ref = _ref(receipt, "outputs", "materialized_sheet")
    assert sheet_ref["sheet_id"] == int(child["id"])
    assert sheet_ref["parent_sheet_id"] == sheet_id
    assert sheet_ref["row_count"] == len(expected)
    column_refs = {
        item["ref"]["name"]: item["ref"]
        for item in receipt["outputs"]
        if item["ref"]["kind"] == "materialized_column"
    }
    assert set(column_refs) == set(SUMMARY_COLUMNS)
    assert all(ref["sheet_id"] == int(child["id"]) for ref in column_refs.values())
    rows_ref = _ref(receipt, "outputs", "materialized_rows")
    assert sorted(rows_ref["row_ids"]) == sorted(actual_groups.values())

    # The read evidence names exactly the rows the request selected, so the
    # summary cannot have been computed over the whole sheet.
    read_ref = _ref(receipt, "inputs", "sheet_rows_read")
    assert read_ref["sheet_id"] == sheet_id
    assert read_ref["row_ids"] == selected_row_ids
    assert [column["name"] for column in read_ref["columns"]] == [
        "constellation",
        "rating",
        "star",
    ]
    assert read_ref["source_values_hash"].startswith("sha256:")

    # Contributor lineage: one membership row per (summary row, source row).
    recorded = _recorded_sources(receipt)
    expected_membership = {
        actual_groups[constellation]: {
            selected_row_ids[position]
            for position, row in enumerate(selected_rows)
            if str(row["constellation"]) == constellation
        }
        for constellation in expected
    }
    assert recorded == expected_membership
    # Every selected row contributes, nothing outside the selection does, and
    # the groups genuinely differ instead of all sharing one source set.
    assert set().union(*recorded.values()) == set(selected_row_ids)
    assert len({frozenset(sources) for sources in recorded.values()}) == len(recorded)
    membership_ref = _ref(receipt, "records", "materialized_row_sources")
    assert {row["source_sheet_id"] for row in membership_ref["rows"]} == {sheet_id}


def test_rowset_plugin_catalog_declares_table_output(tmp_path: Path) -> None:
    client = _client(tmp_path)
    project_id, _sheet_id, _row_ids, _rows = _project_with_stars(client, count=2)
    _rowset_table_op_install_activate_backend(client, project_id)

    catalog = client.get(f"/api/projects/{project_id}/actions/v1/catalog")
    assert catalog.status_code == 200, catalog.text
    entry = next(
        item
        for item in catalog.json()["actions"]
        if item["kind"] == ROWSET_TABLE_OP_ACTION_KIND
    )
    hints = entry["ui_hints"]
    assert set(hints) == {
        "category",
        "form",
        "logical_outputs",
        "semantic_controls",
        "source_requirements",
        "typed_action",
    }
    assert hints["typed_action"] == {"creates_sheet": True}
    assert [output["key"] for output in hints["logical_outputs"]] == SUMMARY_COLUMNS
    assert [output["column_type"] for output in hints["logical_outputs"]] == [
        "text",
        "integer",
        "number",
        "text",
        "integer",
    ]
    assert [item["param"] for item in hints["source_requirements"]] == [
        "group_by",
        "value_column",
        "label_column",
    ]
    assert hints["semantic_controls"] == {
        "group_by": "column",
        "value_column": "column",
        "label_column": "column",
    }
    assert entry["row_scope_policy"] == {
        "kind": "sheet_rows",
        "selectors": ["all_rows", "exact_membership"],
    }
    assert "create_sheet" in entry["side_effects"]


def test_rowset_plugin_rejects_invalid_table_output(tmp_path: Path) -> None:
    client = _client(tmp_path)
    project_id, sheet_id, _row_ids, _rows = _project_with_stars(client, count=3)
    _rowset_table_op_install_activate_backend(client, project_id)

    # The handler emits a row whose declared integer output holds a string.
    result = _run_rowset_action(
        client,
        project_id,
        sheet_id=sheet_id,
        key="invalid-output@rowset",
        extra_params={"emit_invalid_row": True},
        expected_status=400,
    )

    assert result["status"] == "failed"
    error = result["errors"][0]
    assert error["code"] == "invalid_params"
    # The refusal names the offending output field, not merely the request.
    assert "star_count" in error["details"]["reason"]
    sheets = client.get(f"/api/projects/{project_id}/sheets").json()
    if isinstance(sheets, dict):
        sheets = sheets["sheets"]
    assert all(sheet["name"] != "Star summary" for sheet in sheets)
    assert [sheet["id"] for sheet in sheets] == [sheet_id]


def test_rowset_plugin_allows_output_over_legacy_total_cap(tmp_path: Path) -> None:
    client = _client(tmp_path)
    # One group per source row, so the produced table is larger than the 500-row
    # total the retired subprocess transport used to cap plugin output at.
    project_id, sheet_id, row_ids, _rows = _project_with_stars(
        client, count=501, group_count=501
    )
    _rowset_table_op_install_activate_backend(client, project_id)

    result = _run_rowset_action(
        client,
        project_id,
        sheet_id=sheet_id,
        key="over-legacy-total@rowset",
    )

    assert result["status"] == "completed"
    child = _child_sheet(client, project_id, "Star summary")
    data = _rowset_table_op_sheet_data(client, project_id, int(child["id"]), limit=501)
    assert data["total"] == 501
    receipt = _receipt(client, project_id, result["receipt_id"])
    assert _ref(receipt, "outputs", "materialized_sheet")["row_count"] == 501
    assert _ref(receipt, "inputs", "sheet_rows_read")["row_ids"] == row_ids
    assert len(_recorded_sources(receipt)) == 501
