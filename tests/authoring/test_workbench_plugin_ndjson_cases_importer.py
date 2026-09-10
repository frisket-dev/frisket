from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from frisket.authoring.plugin_registry import (
    _reset_default_registry_for_tests,
    default_registry,
)
from frisket.engine.sandbox.shim import SandboxResult
from frisket.engine.store import Project
from frisket.server.app import create_app
from frisket.authoring.workbench import plugin_subprocess


ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = ROOT / "tests/fixtures/local_plugins"
CASE_FILES_PLUGIN_ID = "demo.case_files"
CASE_FILES_PLUGIN_ROOT = FIXTURE_ROOT / "demo_case_files"
NDJSON_PLUGIN_ID = "demo.ndjson_cases"
NDJSON_PLUGIN_ROOT = FIXTURE_ROOT / "demo_ndjson_cases"
IMPORTER_KIND = "demo.ndjson_cases.importer.cases"
HANDLER_KEY = "demo.ndjson_cases:cases"
PLUGIN_CAPABILITY = "plugin:trusted_local_backend"


@pytest.fixture(autouse=True)
def reset_registry() -> None:
    _reset_default_registry_for_tests()
    yield
    _reset_default_registry_for_tests()


def _client(tmp_path: Path) -> TestClient:
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


def _sheet_data(client: TestClient, project_id: str, sheet_id: int) -> dict[str, Any]:
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
    client = _client(tmp_path)
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
    data = _sheet_data(client, project_id, int(sheet_ref["sheet_id"]))
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
    assert runtime_ref["handler_key"] == HANDLER_KEY
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
    client = _client(tmp_path)
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
    client = _client(tmp_path)
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
    client = _client(tmp_path)
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
    assert captured["request"]["handlerKey"] == HANDLER_KEY
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
    tmp_path: Path,
) -> None:
    """Trusted-local importers keep their own streaming adapter semantics.

    This does not require them to share the regular action implementation; it
    only forbids the adapter's former hard total-row refusal.
    """
    from frisket.authoring.workbench.plugin_subprocess_importers import (
        _ImporterPlanBuilder,
    )

    project = Project.create(tmp_path / "plugin-frames.frisket", name="Plugin frames")
    try:
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
    finally:
        project.close()


def test_plugin_importer_malformed_frame_returns_bounded_domain_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client(tmp_path)
    project_id = client.post("/api/projects", json={"name": "Bad Frames"}).json()["id"]
    _activate_case_importer_stack(client, project_id)
    source_path = _write_cases(tmp_path / "malformed.ndjson", [])
    from frisket.authoring.workbench import plugin_subprocess_importers

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
