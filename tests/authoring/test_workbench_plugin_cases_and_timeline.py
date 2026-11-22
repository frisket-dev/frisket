"""Workbench plugin case-files column type, NDJSON cases importer, and timeline projection tests."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from http_test_helpers import (
    post_cell_edit_as_v1_action,
    post_column_set_type_as_v1_action,
)

from frisket.ai.llm import ModelRouter
from frisket.authoring.column_types import get_column_type, parse_value, validate_value
from frisket.authoring.plugin_registry import (
    _reset_default_registry_for_tests,
    default_registry,
)
from frisket.authoring.workbench import plugin_subprocess, plugin_subprocess_importers
from frisket.authoring.workbench.plugin_subprocess_importers import (
    _ImporterPlanBuilder,
)
from frisket.engine.sandbox.shim import SandboxResult
from frisket.execution.purchases import LOCAL_PURCHASE_POLICY
from frisket.server.app import create_app

# --- case files ---

ROOT = Path(__file__).resolve().parents[2]
CASE_FILES_PLUGIN_ID = "demo.case_files"
PLUGIN_CAPABILITY = "plugin:trusted_local_backend"
CASE_FILES_PLUGIN_MANIFEST = (
    ROOT / "tests/fixtures/local_plugins/demo_case_files/plugin.json"
)


def _case_files_client(tmp_path: Path) -> TestClient:
    router = ModelRouter(
        model_call_policy=LOCAL_PURCHASE_POLICY, cache=None, cache_mode="off"
    )
    return TestClient(create_app(tmp_path / "ws", router=router))


def _load_enable_and_activate_backend(client: TestClient, project_id: str) -> str:
    # install-local writes the workspace catalog identity that /activate
    # reads (a raw plugin.load action no longer populates it).
    installed = client.post(
        f"/api/projects/{project_id}/workbench/plugins/{CASE_FILES_PLUGIN_ID}/install-local",
        json={
            "source": {"kind": "localPath", "value": str(CASE_FILES_PLUGIN_MANIFEST)},
            "arbitraryPackageLoadAllowed": False,
        },
    )
    assert installed.status_code == 200, installed.text
    receipt_id = installed.json()["receiptId"]
    assert receipt_id

    enabled = client.post(
        f"/api/projects/{project_id}/workbench/plugins/{CASE_FILES_PLUGIN_ID}/activate",
        json={
            "receiptId": receipt_id,
            "trustAcknowledged": True,
            "permissionsAccepted": [PLUGIN_CAPABILITY],
            "arbitraryPackageLoadAllowed": False,
        },
    )
    assert enabled.status_code == 200, enabled.text

    backend = client.post(
        f"/api/projects/{project_id}/workbench/plugins/{CASE_FILES_PLUGIN_ID}/backend/activate",
        json={"trustAcknowledged": True, "arbitraryPackageLoadAllowed": False},
    )
    assert backend.status_code == 200, backend.text
    assert backend.json()["registeredBackendContributions"]["columnTypes"] == [
        "case_id"
    ]
    return receipt_id


def _import_cases(
    client: TestClient,
    project_id: str,
    csv: str = "title,case_id\nA,CASE-0001\nB,CASE-0002\n",
) -> tuple[int, dict[str, Any]]:
    response = client.post(
        f"/api/projects/{project_id}/import/csv",
        files={"file": ("cases.csv", csv, "text/csv")},
    )
    assert response.status_code == 200, response.text
    sheet_id = int(response.json()["sheet_id"])
    data = client.get(f"/api/projects/{project_id}/sheets/{sheet_id}/data").json()
    case_id = next(column for column in data["columns"] if column["name"] == "case_id")
    return sheet_id, case_id


def test_demo_case_files_registers_case_id_metadata_parser_and_visibility(
    tmp_path: Path,
) -> None:
    _reset_default_registry_for_tests()
    try:
        client = _case_files_client(tmp_path)
        project_id = client.post("/api/projects", json={"name": "Case Files"}).json()[
            "id"
        ]

        before = client.get("/api/column-types")
        assert before.status_code == 200
        assert "case_id" not in {item["name"] for item in before.json()}

        _load_enable_and_activate_backend(client, project_id)

        spec = get_column_type("case_id")
        assert spec is not None
        assert spec.core is False
        assert spec.plugin == CASE_FILES_PLUGIN_ID
        assert spec.presentation == {
            "base": "text",
            "owner": "plugin",
            "plugin": CASE_FILES_PLUGIN_ID,
        }
        assert validate_value("case_id", "CASE-0000")
        assert validate_value("case_id", "CASE-0042")
        assert not validate_value("case_id", "case-0042")
        assert not validate_value("case_id", "CASE-42")
        assert parse_value("case_id", "case 42") == "CASE-0042"
        assert parse_value("case_id", "") is None
        with pytest.raises(ValueError):
            parse_value("case_id", "not a case")

        global_registry = client.get("/api/column-types")
        assert global_registry.status_code == 200
        assert "case_id" not in {item["name"] for item in global_registry.json()}

        registry = client.get(f"/api/projects/{project_id}/column-types")
        assert registry.status_code == 200
        case_type = next(item for item in registry.json() if item["name"] == "case_id")
        assert case_type["core"] is False
        assert case_type["plugin"] == CASE_FILES_PLUGIN_ID
        assert case_type["has_validator"] is True
        assert case_type["has_parser"] is True
        assert case_type["presentation"] == spec.presentation

        project_b = client.post(
            "/api/projects", json={"name": "Case Files disabled"}
        ).json()["id"]
        disabled_registry = client.get(f"/api/projects/{project_b}/column-types")
        assert disabled_registry.status_code == 200
        assert "case_id" not in {item["name"] for item in disabled_registry.json()}
        _sheet_b, case_b = _import_cases(client, project_b)
        rejected = post_column_set_type_as_v1_action(
            client, project_b, case_b["id"], "case_id"
        )
        assert rejected.status_code == 400, rejected.text
        assert rejected.json()["errors"][0]["code"] == "invalid_column_type"

        disabled_import = client.post(
            f"/api/projects/{project_b}/actions/v1/run",
            json={
                "action_id": "import.rows",
                "scope": {"kind": "project"},
                "sheet_name": "Disabled Direct Cases",
                "params": {
                    "columns": [
                        {"name": "title", "type": "text"},
                        {"name": "case_id", "type": "case_id"},
                    ],
                    "rows": [{"title": "Gamma", "case_id": "case 42"}],
                    "source": {
                        "kind": "inline",
                        "label": "disabled case import",
                        "fingerprint": "sha256:disabled-case-import",
                    },
                },
                "idempotency_key": "plugin-case-id-disabled-import@sha256:v1",
            },
        )
        assert disabled_import.status_code == 400, disabled_import.text
        assert disabled_import.json()["errors"][0]["code"] == "invalid_column_type"
    finally:
        _reset_default_registry_for_tests()


def test_case_id_set_type_and_cell_edit_use_parser_without_retyping_rewrites(
    tmp_path: Path,
) -> None:
    _reset_default_registry_for_tests()
    try:
        client = _case_files_client(tmp_path)
        project_id = client.post(
            "/api/projects", json={"name": "Case Files values"}
        ).json()["id"]
        _load_enable_and_activate_backend(client, project_id)
        sheet_id, case_id = _import_cases(client, project_id)

        changed = post_column_set_type_as_v1_action(
            client, project_id, case_id["id"], "case_id"
        )
        assert changed.status_code == 200, changed.text
        assert changed.json()["status"] == "completed"

        data = client.get(
            f"/api/projects/{project_id}/sheets/{sheet_id}/data?offset=0&limit=2"
        ).json()
        assert (
            next(column for column in data["columns"] if column["name"] == "case_id")[
                "type"
            ]
            == "case_id"
        )
        first_row_id = int(data["rows"][0]["id"])

        valid_edit = post_cell_edit_as_v1_action(
            client,
            project_id,
            [
                {
                    "row_id": first_row_id,
                    "column_id": case_id["id"],
                    "value": "case_42",
                }
            ],
        )
        assert valid_edit.status_code == 200, valid_edit.text
        assert valid_edit.json()["status"] == "completed"

        project = client.app.state.workspace.get(project_id)
        assert project.get_values(sheet_id, case_id["id"])[first_row_id] == "CASE-0042"

        direct_import = client.post(
            f"/api/projects/{project_id}/actions/v1/run",
            json={
                "action_id": "import.rows",
                "scope": {"kind": "project"},
                "sheet_name": "Direct Cases",
                "params": {
                    "columns": [
                        {"name": "title", "type": "text"},
                        {"name": "case_id", "type": "case_id"},
                    ],
                    "rows": [{"title": "Gamma", "case_id": "case 43"}],
                    "source": {
                        "kind": "inline",
                        "label": "direct case import",
                        "fingerprint": "sha256:direct-case-import",
                    },
                },
                "idempotency_key": "plugin-case-id-direct-import@sha256:v1",
            },
        )
        assert direct_import.status_code == 200, direct_import.text
        direct_result = direct_import.json()
        direct_sheet_id = next(
            output["sheet_id"]
            for output in direct_result["outputs"]
            if output["kind"] == "sheet"
        )
        direct_data = client.get(
            f"/api/projects/{project_id}/sheets/{direct_sheet_id}/data?offset=0&limit=1"
        ).json()
        direct_case_column = next(
            column for column in direct_data["columns"] if column["name"] == "case_id"
        )
        assert direct_case_column["type"] == "case_id"
        assert (
            direct_data["rows"][0]["cells"][str(direct_case_column["id"])]
            == "CASE-0043"
        )

        csv_path = tmp_path / "typed_cases.csv"
        csv_path.write_text("title,case_id\nDelta,case 44\n", encoding="utf-8")
        csv_import = client.post(
            f"/api/projects/{project_id}/actions/v1/run",
            json={
                "action_id": "import.csv",
                "scope": {"kind": "project"},
                "sheet_name": "CSV Cases",
                "params": {
                    "sources": [{"path": str(csv_path), "label": "typed_cases.csv"}],
                    "columns": [
                        {"name": "title", "type": "text"},
                        {"name": "case_id", "type": "case_id"},
                    ],
                },
                "idempotency_key": "plugin-case-id-csv-import@sha256:v1",
            },
        )
        assert csv_import.status_code == 200, csv_import.text
        csv_result = csv_import.json()
        csv_sheet_id = next(
            output["sheet_id"]
            for output in csv_result["outputs"]
            if output["kind"] == "sheet"
        )
        csv_data = client.get(
            f"/api/projects/{project_id}/sheets/{csv_sheet_id}/data?offset=0&limit=1"
        ).json()
        csv_case_column = next(
            column for column in csv_data["columns"] if column["name"] == "case_id"
        )
        assert csv_case_column["type"] == "case_id"
        assert csv_data["rows"][0]["cells"][str(csv_case_column["id"])] == "CASE-0044"

        invalid_edit = post_cell_edit_as_v1_action(
            client,
            project_id,
            [
                {
                    "row_id": first_row_id,
                    "column_id": case_id["id"],
                    "value": "not a case",
                }
            ],
            idempotency_key="plugin-case-id-invalid-edit@sha256:v1",
        )
        assert invalid_edit.status_code == 400, invalid_edit.text
        assert (
            invalid_edit.json()["errors"][0]["code"] == "column_value_validation_failed"
        )
        assert project.get_values(sheet_id, case_id["id"])[first_row_id] == "CASE-0042"

        dirty_sheet_id = project.add_sheet("Dirty Cases")
        dirty_case_id = project.add_column(dirty_sheet_id, "case_id", type="text")
        project.add_rows(
            dirty_sheet_id,
            [{"case_id": "case-0003"}, {"case_id": "CASE-0004"}],
            {"case_id": dirty_case_id},
        )
        project.db.commit()
        retyped = post_column_set_type_as_v1_action(
            client, project_id, dirty_case_id, "case_id"
        )
        assert retyped.status_code == 200, retyped.text
        dirty_data = client.get(
            f"/api/projects/{project_id}/sheets/{dirty_sheet_id}/data?offset=0&limit=2"
        ).json()
        dirty_column = next(
            column for column in dirty_data["columns"] if column["name"] == "case_id"
        )
        assert dirty_column["type"] == "case_id"
        assert dirty_data["rows"][0]["cells"][str(dirty_case_id)] == "case-0003"
        assert dirty_data["rows"][0]["meta"][str(dirty_case_id)]["invalid"] is True
    finally:
        _reset_default_registry_for_tests()


# --- ndjson cases ---

FIXTURE_ROOT = ROOT / "tests/fixtures/local_plugins"
CASE_FILES_PLUGIN_ID = "demo.case_files"
CASE_FILES_PLUGIN_ROOT = FIXTURE_ROOT / "demo_case_files"
NDJSON_PLUGIN_ID = "demo.ndjson_cases"
NDJSON_PLUGIN_ROOT = FIXTURE_ROOT / "demo_ndjson_cases"
IMPORTER_KIND = "demo.ndjson_cases.importer.cases"
NDJSON_CASES_HANDLER_KEY = "demo.ndjson_cases:cases"


@pytest.fixture(autouse=True)
def reset_registry() -> None:
    _reset_default_registry_for_tests()
    yield
    _reset_default_registry_for_tests()


def _ndjson_cases_client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(tmp_path / "workspace"))


def _install_activate_plugin(
    client: TestClient,
    project_id: str,
    *,
    plugin_id: str,
    plugin_root: Path,
    executable: bool = False,
) -> dict[str, Any]:
    installed = client.post(
        f"/api/projects/{project_id}/workbench/plugins/{plugin_id}/install-local",
        json={
            "source": {"kind": "localPath", "value": str(plugin_root)},
            "arbitraryPackageLoadAllowed": False,
        },
    )
    assert installed.status_code == 200, installed.text
    receipt_id = installed.json()["receiptId"]

    activated = client.post(
        f"/api/projects/{project_id}/workbench/plugins/{plugin_id}/activate",
        json={
            "receiptId": receipt_id,
            "trustAcknowledged": True,
            "permissionsAccepted": [PLUGIN_CAPABILITY],
            "arbitraryPackageLoadAllowed": False,
        },
    )
    assert activated.status_code == 200, activated.text

    backend = client.post(
        f"/api/projects/{project_id}/workbench/plugins/{plugin_id}/backend/activate",
        json={
            "trustAcknowledged": True,
            "arbitraryPackageLoadAllowed": False,
            "executableHandlersAllowed": executable,
        },
    )
    assert backend.status_code == 200, backend.text
    return backend.json()


def _activate_case_importer_stack(client: TestClient, project_id: str) -> None:
    case_backend = _install_activate_plugin(
        client,
        project_id,
        plugin_id=CASE_FILES_PLUGIN_ID,
        plugin_root=CASE_FILES_PLUGIN_ROOT,
    )
    assert case_backend["registeredBackendContributions"]["columnTypes"] == ["case_id"]
    ndjson_backend = _install_activate_plugin(
        client,
        project_id,
        plugin_id=NDJSON_PLUGIN_ID,
        plugin_root=NDJSON_PLUGIN_ROOT,
        executable=True,
    )
    assert ndjson_backend["registeredRuntimeBindings"]["importers"] == [IMPORTER_KIND]
    binding = next(
        spec
        for spec in default_registry().runtime_binding_specs("importers")
        if spec.kind == IMPORTER_KIND
    )
    assert binding.handler_api == "plugin_importer_subprocess"


def _write_cases(path: Path, records: list[dict[str, Any] | str]) -> Path:
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            if isinstance(record, str):
                handle.write(record)
            else:
                handle.write(json.dumps(record, separators=(",", ":")))
            handle.write("\n")
    return path


def _import_runtime_action(source_path: Path, *, key: str) -> dict[str, Any]:
    return {
        "action_id": "import.runtime",
        "scope": {"kind": "project"},
        "sheet_name": "Case Import",
        "params": {
            "importer_kind": IMPORTER_KIND,
            "source": {
                "kind": "runtime",
                "label": source_path.name,
                "fingerprint": f"sha256:test-{source_path.stat().st_size}",
            },
            "handler_params": {"path": str(source_path)},
        },
        "idempotency_key": key,
    }


def _run_import(
    client: TestClient,
    project_id: str,
    source_path: Path,
    *,
    key: str,
    expected_status: int = 200,
) -> dict[str, Any]:
    response = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json=_import_runtime_action(source_path, key=key),
    )
    assert response.status_code == expected_status, response.text
    return response.json()


def _receipt(client: TestClient, project_id: str, receipt_id: str) -> dict[str, Any]:
    response = client.get(
        f"/api/projects/{project_id}/actions/v1/receipts/{receipt_id}"
    )
    assert response.status_code == 200, response.text
    return response.json()


def _ndjson_cases_sheet_data(
    client: TestClient, project_id: str, sheet_id: int
) -> dict[str, Any]:
    response = client.get(
        f"/api/projects/{project_id}/sheets/{sheet_id}/data?offset=0&limit=100"
    )
    assert response.status_code == 200, response.text
    return response.json()


def _values_by_column(data: dict[str, Any]) -> dict[str, list[Any]]:
    columns = {int(column["id"]): column for column in data["columns"]}
    values = {str(column["name"]): [] for column in columns.values()}
    for row in data["rows"]:
        for column_id, column in columns.items():
            values[str(column["name"])].append(row["cells"].get(str(column_id)))
    return values


def test_demo_ndjson_cases_importer_streams_casts_and_records_diagnostics(
    tmp_path: Path,
) -> None:
    client = _ndjson_cases_client(tmp_path)
    project_id = client.post("/api/projects", json={"name": "Cases"}).json()["id"]
    _activate_case_importer_stack(client, project_id)
    source_path = _write_cases(
        tmp_path / "cases.ndjson",
        [
            {
                "id": "case 1",
                "title": "Alpha",
                "summary": "First case",
                "priority": "2",
            },
            {"case_id": "CASE-0002", "title": "Beta", "priority": 3},
            "",
            {
                "case_id": "case_3",
                "title": "Gamma",
                "summary": "Third case",
                "priority": 1,
            },
        ],
    )

    result = _run_import(
        client,
        project_id,
        source_path,
        key="demo-ndjson-cases-success@sha256:v1",
    )

    assert result["status"] == "completed"
    sheet_ref = next(
        output["ref"] for output in result["outputs"] if output["name"] == "Case Import"
    )
    data = _ndjson_cases_sheet_data(client, project_id, int(sheet_ref["sheet_id"]))
    values = _values_by_column(data)
    assert values["case_id"] == ["CASE-0001", "CASE-0002", "CASE-0003"]
    assert values["title"] == ["Alpha", "Beta", "Gamma"]
    assert values["summary"] == ["First case", "", "Third case"]
    assert values["priority"] == [2, 3, 1]

    receipt = _receipt(client, project_id, result["receipt_id"])
    runtime_ref = next(
        item["ref"]
        for item in receipt["inputs"]
        if item["ref"]["kind"] == "workbench_runtime_binding"
    )
    assert runtime_ref["handler_key"] == NDJSON_CASES_HANDLER_KEY
    assert runtime_ref["streaming"]["spooled"] is True
    assert runtime_ref["streaming"]["row_count"] == 3
    assert "rows" not in runtime_ref["streaming"]
    assert {
        (
            diagnostic["level"],
            diagnostic["code"],
            diagnostic["attach"]["line"],
            diagnostic["attach"].get("field"),
            diagnostic["attach"].get("object"),
        )
        for diagnostic in runtime_ref["diagnostics"]
    } == {
        ("warning", "missing_summary", 2, "summary", "CASE-0002"),
        ("warning", "blank_line", 3, None, None),
    }

    replay = _run_import(
        client,
        project_id,
        source_path,
        key="demo-ndjson-cases-success@sha256:v1",
    )
    assert replay["receipt_id"] == result["receipt_id"]


def test_demo_ndjson_cases_importer_failure_is_all_or_nothing(
    tmp_path: Path,
) -> None:
    client = _ndjson_cases_client(tmp_path)
    project_id = client.post("/api/projects", json={"name": "Bad Cases"}).json()["id"]
    _activate_case_importer_stack(client, project_id)
    source_path = _write_cases(
        tmp_path / "bad-cases.ndjson",
        [
            {
                "case_id": "case 1",
                "title": "Alpha",
                "summary": "First case",
                "priority": 2,
            },
            {"case_id": "nope", "title": "Bad", "priority": 3},
        ],
    )

    result = _run_import(
        client,
        project_id,
        source_path,
        key="demo-ndjson-cases-failed@sha256:v1",
        expected_status=400,
    )

    assert result["status"] == "failed"
    assert result["errors"][0]["code"] == "invalid_case_id"
    diagnostics = result["errors"][0]["details"]["diagnostics"]
    assert diagnostics == [
        {
            "level": "error",
            "code": "invalid_case_id",
            "message": "Case id must look like CASE-0000",
            "attach": {
                "kind": "source_line",
                "line": 2,
                "field": "case_id",
                "object": "nope",
            },
        }
    ]
    project = client.app.state.workspace.get(project_id)
    assert project.db.execute("SELECT COUNT(*) FROM sheets").fetchone()[0] == 0
    assert (
        project.db.execute(
            "SELECT COUNT(*) FROM receipts WHERE action_kind='import.runtime'"
        ).fetchone()[0]
        == 0
    )


def test_demo_ndjson_cases_requires_project_case_id_type(tmp_path: Path) -> None:
    client = _ndjson_cases_client(tmp_path)
    project_id = client.post("/api/projects", json={"name": "No Case Type"}).json()[
        "id"
    ]
    _install_activate_plugin(
        client,
        project_id,
        plugin_id=NDJSON_PLUGIN_ID,
        plugin_root=NDJSON_PLUGIN_ROOT,
        executable=True,
    )
    source_path = _write_cases(
        tmp_path / "cases-no-type.ndjson",
        [{"case_id": "case 1", "title": "Alpha", "summary": "First", "priority": 1}],
    )

    result = _run_import(
        client,
        project_id,
        source_path,
        key="demo-ndjson-cases-missing-type@sha256:v1",
        expected_status=400,
    )

    assert result["status"] == "failed"
    assert result["errors"][0]["code"] == "plugin_importer_schema_invalid"
    assert "case_id" in json.dumps(result["errors"][0])


def test_plugin_importer_subprocess_frame_adapter_spools_and_validates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _ndjson_cases_client(tmp_path)
    project_id = client.post("/api/projects", json={"name": "Frame Cases"}).json()["id"]
    _activate_case_importer_stack(client, project_id)
    source_path = _write_cases(tmp_path / "captured.ndjson", [])
    captured: dict[str, Any] = {}

    async def fake_run_sandboxed_stdout_lines(
        argv: list[str],
        *,
        stdin_data: bytes,
        on_stdout_line: Any,
        **_kwargs: Any,
    ) -> SandboxResult:
        assert argv[-2] == "--importer"
        request = json.loads(stdin_data.decode("utf-8"))
        captured["request"] = request
        stdout = "\n".join(
            [
                json.dumps(
                    {
                        "type": "schema",
                        "columns": [
                            {"name": "case_id", "type": "case_id"},
                            {"name": "title", "type": "text"},
                            {"name": "summary", "type": "text"},
                            {"name": "priority", "type": "integer"},
                        ],
                    }
                ),
                *[
                    json.dumps(
                        {
                            "type": "row",
                            "row": {
                                "case_id": f"case {index}",
                                "title": f"Case {index}",
                                "summary": "",
                                "priority": index,
                            },
                        }
                    )
                    for index in range(1, 6)
                ],
                json.dumps(
                    {
                        "type": "done",
                        "row_count": 5,
                    }
                ),
            ]
        )
        for line in stdout.splitlines():
            on_stdout_line(line)
        return SandboxResult(returncode=0, stdout="", stderr="")

    def fail_buffered_runner(*_args: Any, **_kwargs: Any) -> SandboxResult:
        raise AssertionError("plugin importers must use streaming sandbox stdout")

    monkeypatch.setattr(plugin_subprocess, "run_sandboxed", fail_buffered_runner)
    monkeypatch.setattr(
        plugin_subprocess,
        "run_sandboxed_stdout_lines",
        fake_run_sandboxed_stdout_lines,
    )
    result = _run_import(
        client,
        project_id,
        source_path,
        key="demo-ndjson-cases-frame-adapter@sha256:v1",
    )

    assert result["status"] == "completed"
    assert "rows" not in captured["request"]
    assert captured["request"]["handlerKey"] == NDJSON_CASES_HANDLER_KEY
    receipt = _receipt(client, project_id, result["receipt_id"])
    runtime_ref = next(
        item["ref"]
        for item in receipt["inputs"]
        if item["ref"]["kind"] == "workbench_runtime_binding"
    )
    assert runtime_ref["streaming"]["spooled"] is True
    assert runtime_ref["streaming"]["row_count"] == 5

    project = client.app.state.workspace.get(project_id)
    sheet = project.db.execute(
        "SELECT id FROM sheets WHERE name='Case Import'"
    ).fetchone()
    assert sheet is not None
    column = project.db.execute(
        "SELECT id FROM columns WHERE sheet_id=? AND name='case_id'",
        (sheet["id"],),
    ).fetchone()
    values = project.get_values(int(sheet["id"]), int(column["id"]))
    assert list(values.values()) == [
        "CASE-0001",
        "CASE-0002",
        "CASE-0003",
        "CASE-0004",
        "CASE-0005",
    ]


def test_trusted_local_importer_frames_have_no_fixed_10000_row_ceiling(
    make_project,
) -> None:
    """Trusted-local importers keep their own streaming adapter semantics.

    This does not require them to share the regular action implementation; it
    only forbids the adapter's former hard total-row refusal.
    """

    project = make_project("Plugin frames")
    builder = _ImporterPlanBuilder(
        project,
        binding=SimpleNamespace(
            plugin="trusted.local",
            kind="trusted.local.importer",
            handler_key="trusted.local:rows",
        ),
        source_label="generated.ndjson",
    )
    builder.handle_frame(
        {
            "type": "schema",
            "columns": [{"name": "name", "type": "text"}],
        }
    )
    for index in range(10_001):
        builder.handle_frame({"type": "row", "row": {"name": f"record-{index}"}})
    builder.handle_frame({"type": "done", "row_count": 10_001})

    plan = builder.finish()

    assert plan.get("status") != "failed", plan
    assert plan["streaming"]["row_count"] == 10_001
    assert iter(plan["rows"]) is plan["rows"]
    rows = list(plan["rows"])
    assert len(rows) == 10_001
    assert rows[-1] == {"name": "record-10000"}
    assert builder.stream.closed


def test_plugin_importer_malformed_frame_returns_bounded_domain_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _ndjson_cases_client(tmp_path)
    project_id = client.post("/api/projects", json={"name": "Bad Frames"}).json()["id"]
    _activate_case_importer_stack(client, project_id)
    source_path = _write_cases(tmp_path / "malformed.ndjson", [])

    spools = []
    original_spool = plugin_subprocess_importers.tempfile.SpooledTemporaryFile

    def track_spool(*args, **kwargs):
        stream = original_spool(*args, **kwargs)
        spools.append(stream)
        return stream

    monkeypatch.setattr(
        plugin_subprocess_importers.tempfile, "SpooledTemporaryFile", track_spool
    )

    async def malformed_stdout(
        _argv: list[str],
        *,
        on_stdout_line: Any,
        **_kwargs: Any,
    ) -> SandboxResult:
        on_stdout_line(
            json.dumps(
                {"type": "schema", "columns": [{"name": "value", "type": "integer"}]}
            )
        )
        for value in range(501):
            on_stdout_line(json.dumps({"type": "row", "row": {"value": value}}))
        on_stdout_line(json.dumps({"type": "future_frame"}))
        return SandboxResult(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(
        plugin_subprocess,
        "run_sandboxed_stdout_lines",
        malformed_stdout,
    )
    result = _run_import(
        client,
        project_id,
        source_path,
        key="demo-ndjson-cases-malformed-frame@sha256:v1",
        expected_status=400,
    )

    assert result["status"] == "failed"
    assert spools and all(stream.closed for stream in spools)
    assert client.app.state.workspace.get(project_id).sheets(include_hidden=True) == []
    assert result["outputs"] == []
    assert result["errors"] == [
        {
            "schema_version": "frisket.action_error.v1",
            "code": "plugin_subprocess_invalid_response",
            "message": "Trusted-local plugin importer returned invalid frame",
            "action_kind": "import.runtime",
            "field": "response",
            "details": {},
            "needs_confirmation": False,
        }
    ]


# --- timeline ---

TIMELINE_PLUGIN_ID = "demo.timeline"
PROJECTION_KIND = "demo.timeline.projection.timeline"
VIEW_ID = "demo.timeline.view.timeline"
TIMELINE_HANDLER_KEY = "demo.timeline:timeline"
PLUGIN_ROOT = ROOT / "tests/fixtures/local_plugins/demo_timeline"
TIMELINE_PLUGIN_MANIFEST = PLUGIN_ROOT / "plugin.json"
TRUSTED_LOCAL_BACKEND_CAPABILITY = "plugin:trusted_local_backend"


def _timeline_client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(tmp_path / "workspace"))


def _run_action(
    client: TestClient,
    project_id: str,
    spec: dict[str, Any],
) -> dict[str, Any]:
    response = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json=spec,
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == "completed", result
    return result


def _project_with_timeline_rows(client: TestClient) -> tuple[str, int, dict[str, int]]:
    project_id = client.post("/api/projects", json={"name": "Timeline plugin"}).json()[
        "id"
    ]
    imported = client.post(
        f"/api/projects/{project_id}/import/csv",
        files={
            "file": (
                "cases.csv",
                "case_id,event_date,title\n"
                "CASE-2,2026-03-02,Second event\n"
                "CASE-1,2026-01-15,First event\n"
                "CASE-3,,No date skipped\n",
                "text/csv",
            )
        },
    )
    assert imported.status_code == 200, imported.text
    sheet_id = int(imported.json()["sheet_id"])
    data = _timeline_sheet_data(client, project_id, sheet_id)
    columns = {column["name"]: int(column["id"]) for column in data["columns"]}
    _run_action(
        client,
        project_id,
        {
            "action_id": "column.set_type",
            "scope": {"kind": "project"},
            "params": {"column_id": columns["event_date"], "type": "date"},
            "idempotency_key": "timeline-date-column@sha256:v1",
        },
    )
    return project_id, sheet_id, columns


def _timeline_sheet_data(
    client: TestClient, project_id: str, sheet_id: int
) -> dict[str, Any]:
    response = client.get(
        f"/api/projects/{project_id}/sheets/{sheet_id}/data?offset=0&limit=20"
    )
    assert response.status_code == 200, response.text
    return response.json()


def _activate_timeline_plugin(client: TestClient, project_id: str) -> None:
    installed = client.post(
        f"/api/projects/{project_id}/workbench/plugins/{TIMELINE_PLUGIN_ID}/install-local",
        json={
            "source": {"kind": "localPath", "value": str(PLUGIN_ROOT)},
            "arbitraryPackageLoadAllowed": False,
        },
    )
    assert installed.status_code == 200, installed.text
    receipt_id = installed.json()["receiptId"]
    assert receipt_id
    activated = client.post(
        f"/api/projects/{project_id}/workbench/plugins/{TIMELINE_PLUGIN_ID}/activate",
        json={
            "receiptId": receipt_id,
            "trustAcknowledged": True,
            "permissionsAccepted": [TRUSTED_LOCAL_BACKEND_CAPABILITY],
            "arbitraryPackageLoadAllowed": False,
        },
    )
    assert activated.status_code == 200, activated.text
    backend = client.post(
        f"/api/projects/{project_id}/workbench/plugins/{TIMELINE_PLUGIN_ID}/backend/activate",
        json={
            "trustAcknowledged": True,
            "arbitraryPackageLoadAllowed": False,
            "executableHandlersAllowed": True,
        },
    )
    assert backend.status_code == 200, backend.text
    body = backend.json()
    assert body["registeredRuntimeBindings"]["projections"] == [PROJECTION_KIND]


def _projection_request(
    sheet_id: int,
    columns: dict[str, int],
    *,
    artifact_id: str | None = None,
    mode: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "projectionKind": PROJECTION_KIND,
        "target": {
            "sheetId": sheet_id,
            "dateColumnId": columns["event_date"],
            "titleColumnId": columns["title"],
            "caseColumnId": columns["case_id"],
        },
        "params": {},
    }
    if artifact_id is not None:
        body["artifactId"] = artifact_id
    if mode is not None:
        body["mode"] = mode
    return body


def _post_projection(
    client: TestClient,
    project_id: str,
    endpoint: str,
    body: dict[str, Any],
) -> dict[str, Any]:
    response = client.post(
        f"/api/projects/{project_id}/projections/runtime/{endpoint}",
        json=body,
    )
    assert response.status_code == 200, response.text
    return response.json()


def _host_modules_loaded_from(root: Path) -> list[str]:
    root = root.resolve()
    loaded: list[str] = []
    for module_name, module in list(sys.modules.items()):
        filename = getattr(module, "__file__", None)
        if not filename:
            continue
        try:
            path = Path(filename).resolve()
        except OSError:
            continue
        if path.is_relative_to(root):
            loaded.append(module_name)
    return sorted(loaded)


def test_timeline_projection_builds_host_owned_artifact_without_mutating_source(
    tmp_path: Path,
) -> None:
    client = _timeline_client(tmp_path)
    project_id, sheet_id, columns = _project_with_timeline_rows(client)
    before = _timeline_sheet_data(client, project_id, sheet_id)
    before_row_count = len(before["rows"])
    before_column_ids = [column["id"] for column in before["columns"]]

    _activate_timeline_plugin(client, project_id)
    binding = default_registry().runtime_binding_specs("projections")[0]
    assert binding.kind == PROJECTION_KIND
    assert binding.handler_key == TIMELINE_HANDLER_KEY
    assert binding.handler_api == "plugin_projection_subprocess"
    assert _host_modules_loaded_from(PLUGIN_ROOT) == []

    runtime_index = client.get(f"/api/projects/{project_id}/workbench/plugins").json()
    plugin = next(
        item
        for item in runtime_index["plugins"]
        if item["pluginId"] == TIMELINE_PLUGIN_ID
    )
    assert plugin["contributionSummary"] == [
        {"kind": "workbench_view", "count": 1, "ids": [VIEW_ID]},
        {"kind": "projection", "count": 1, "ids": [PROJECTION_KIND]},
    ]
    descriptor = plugin["workbenchDescriptorManifests"][0]
    assert descriptor["id"] == VIEW_ID
    assert descriptor["projectionKind"] == PROJECTION_KIND
    assert descriptor["dataRequirements"] == [
        {"kind": "activeSheet"},
        {"kind": "sheetHasColumnType", "columnType": "date"},
    ]
    assert "componentKey" not in descriptor

    status = _post_projection(
        client,
        project_id,
        "status",
        _projection_request(sheet_id, columns),
    )
    assert status["status"] == "missing"
    assert status["freshness"]["state"] == "missing"
    assert status["outputs"]["artifactRefs"] == []
    first_generation = status["freshness"]["generation"]

    build = _post_projection(
        client,
        project_id,
        "build",
        _projection_request(sheet_id, columns, mode="refresh"),
    )
    assert build["status"] == "accepted"
    assert build["build"]["operation"] == "refresh"
    artifact_ref = build["outputs"]["artifactRefs"][0]
    assert artifact_ref["kind"] == "projection_artifact"
    assert artifact_ref["projectionKind"] == PROJECTION_KIND
    artifact_id = artifact_ref["artifactId"]
    assert artifact_id.startswith("projection://demo.timeline.projection.timeline/")
    assert not artifact_id.startswith(("/", "file:", "sqlite:", "sql:"))

    artifact = _post_projection(
        client,
        project_id,
        "artifact",
        _projection_request(sheet_id, columns, artifact_id=artifact_id),
    )
    assert artifact["schemaVersion"] == "frisket.timeline_projection_artifact.v1"
    assert artifact["projectionKind"] == PROJECTION_KIND
    assert artifact["generation"] == first_generation
    assert artifact["metrics"]["sourceRowCount"] == 3
    assert artifact["metrics"]["timelineItemCount"] == 2
    assert [
        (item["date"], item["title"], item["caseId"]) for item in artifact["items"]
    ] == [
        ("2026-01-15", "First event", "CASE-1"),
        ("2026-03-02", "Second event", "CASE-2"),
    ]
    assert _host_modules_loaded_from(PLUGIN_ROOT) == []

    after = _timeline_sheet_data(client, project_id, sheet_id)
    assert len(after["rows"]) == before_row_count
    assert [column["id"] for column in after["columns"]] == before_column_ids

    ready = _post_projection(
        client,
        project_id,
        "status",
        _projection_request(sheet_id, columns),
    )
    assert ready["status"] == "ready"
    assert ready["freshness"]["state"] == "fresh"
    assert ready["outputs"]["artifactRefs"][0]["artifactId"] == artifact_id

    first_row_id = int(after["rows"][0]["id"])
    _run_action(
        client,
        project_id,
        {
            "action_id": "cell.edit",
            "scope": {"kind": "project"},
            "params": {
                "edits": [
                    {
                        "row_id": first_row_id,
                        "column_id": columns["title"],
                        "value": "Second event edited",
                    }
                ]
            },
            "idempotency_key": "timeline-source-edit@sha256:v1",
        },
    )
    stale = _post_projection(
        client,
        project_id,
        "status",
        _projection_request(sheet_id, columns),
    )
    assert stale["status"] == "stale"
    assert stale["freshness"]["state"] == "stale"
    assert stale["freshness"]["generation"] != first_generation
    assert stale["outputs"]["artifactRefs"][0]["artifactId"] == artifact_id

    rebuild = _post_projection(
        client,
        project_id,
        "build",
        _projection_request(sheet_id, columns, mode="rebuild"),
    )
    assert rebuild["status"] == "accepted"
    assert rebuild["build"]["operation"] == "rebuild"
    rebuilt_artifact_id = rebuild["outputs"]["artifactRefs"][0]["artifactId"]
    assert rebuilt_artifact_id != artifact_id

    rebuilt_artifact = _post_projection(
        client,
        project_id,
        "artifact",
        _projection_request(sheet_id, columns, artifact_id=rebuilt_artifact_id),
    )
    assert [item["title"] for item in rebuilt_artifact["items"]] == [
        "First event",
        "Second event edited",
    ]
    assert _host_modules_loaded_from(PLUGIN_ROOT) == []
