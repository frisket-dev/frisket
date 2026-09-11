from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from frisket.authoring.column_types import get_column_type, parse_value, validate_value
from frisket.ai.llm import ModelRouter
from frisket.authoring.plugin_registry import _reset_default_registry_for_tests
from frisket.server.app import create_app
from http_test_helpers import (
    post_cell_edit_as_v1_action,
    post_column_set_type_as_v1_action,
)


ROOT = Path(__file__).resolve().parents[2]
PLUGIN_ID = "demo.case_files"
PLUGIN_CAPABILITY = "plugin:trusted_local_backend"
PLUGIN_MANIFEST = ROOT / "tests/fixtures/local_plugins/demo_case_files/plugin.json"


def _client(tmp_path: Path) -> TestClient:
    router = ModelRouter(cache=None, cache_mode="off")
    return TestClient(create_app(tmp_path / "ws", router=router))


def _load_enable_and_activate_backend(client: TestClient, project_id: str) -> str:
    # install-local writes the workspace catalog identity that /activate
    # reads (a raw plugin.load action no longer populates it).
    installed = client.post(
        f"/api/projects/{project_id}/workbench/plugins/{PLUGIN_ID}/install-local",
        json={
            "source": {"kind": "localPath", "value": str(PLUGIN_MANIFEST)},
            "arbitraryPackageLoadAllowed": False,
        },
    )
    assert installed.status_code == 200, installed.text
    receipt_id = installed.json()["receiptId"]
    assert receipt_id

    enabled = client.post(
        f"/api/projects/{project_id}/workbench/plugins/{PLUGIN_ID}/activate",
        json={
            "receiptId": receipt_id,
            "trustAcknowledged": True,
            "permissionsAccepted": [PLUGIN_CAPABILITY],
            "arbitraryPackageLoadAllowed": False,
        },
    )
    assert enabled.status_code == 200, enabled.text

    backend = client.post(
        f"/api/projects/{project_id}/workbench/plugins/{PLUGIN_ID}/backend/activate",
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
        client = _client(tmp_path)
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
        assert spec.plugin == PLUGIN_ID
        assert spec.presentation == {
            "base": "text",
            "owner": "plugin",
            "plugin": PLUGIN_ID,
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
        assert case_type["plugin"] == PLUGIN_ID
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
        client = _client(tmp_path)
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
