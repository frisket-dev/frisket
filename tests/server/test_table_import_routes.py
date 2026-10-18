from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from frisket.server.route_errors import register_route_error_handler
from frisket.server.routes.table_imports import register_table_import_routes


class _Service:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.calls = []

    def create(self, pid, upload, **kwargs):
        self.calls.append(("create", pid, upload.filename, kwargs))
        return self.snapshot

    def status(self, pid, draft_id):
        self.calls.append(("status", pid, draft_id))
        return self.snapshot


def _snapshot():
    return {
        "schema_version": "frisket.table_import.v1",
        "id": "import-" + "1" * 32,
        "draft_id": "import-" + "1" * 32,
        "state": "analyzing",
        "revision": 1,
        "created_at": "2026-10-18T00:00:00Z",
        "updated_at": "2026-10-18T00:00:00Z",
        "source": {
            "input_kind": "file",
            "filename": "people.csv",
            "size": 8,
            "sha256": "a" * 64,
            "format": "delimited",
            "sheets": [],
            "can_rerun_workbook": False,
        },
        "settings": {
            "revision": 0,
            "requested": {
                "delimiter": None,
                "encoding": None,
                "decimal_separator": "auto",
                "thousands_separator": "auto",
                "worksheet": None,
                "skip_rows_start": 0,
                "first_row_is_header": True,
                "skip_rows_end": 0,
            },
            "resolved": {
                "kind": "delimited",
                "delimiter": ",",
                "encoding": "utf-8",
                "decimal_separator": ".",
                "thousands_separator": ",",
                "worksheet": None,
                "skip_rows_start": 0,
                "first_row_is_header": True,
                "skip_rows_end": 0,
            },
        },
        "analysis": {
            "state": "running",
            "job_id": None,
            "scanned_rows": 1,
            "estimated_rows": None,
            "exact_rows": None,
            "complete": False,
            "issue_counts": {
                "extra_values": 0,
                "missing_values": 0,
                "malformed_records": 0,
                "unreadable_cells": 0,
            },
            "error": None,
        },
        "preview": None,
        "columns": [],
        "destination": None,
        "can_commit": False,
        "commit_blocked_reason": "analyzing",
        "commit": None,
    }


def test_file_and_paste_use_distinct_typed_operations() -> None:
    app = FastAPI()
    service = _Service(_snapshot())
    register_table_import_routes(app, service=service)
    register_route_error_handler(app)
    client = TestClient(app)

    file_response = client.post(
        "/api/projects/p/import/table-imports/file",
        files={"file": ("people.csv", b"a,b\n1,2\n", "text/csv")},
    )
    paste_response = client.post(
        "/api/projects/p/import/table-imports/paste",
        files={"file": ("Pasted rows.csv", b"a\tb\n1\t2\n", "text/csv")},
        data={"large_paste_confirmed": "true"},
    )

    assert file_response.status_code == 201
    assert paste_response.status_code == 201
    assert service.calls[0][3] == {
        "input_kind": "file",
        "large_paste_confirmed": False,
    }
    assert service.calls[1][3] == {
        "input_kind": "paste",
        "large_paste_confirmed": True,
    }
    operation_ids = {
        route.operation_id
        for route in app.routes
        if getattr(route, "operation_id", None)
    }
    assert {
        "createTableImportFromFile",
        "createTableImportFromPaste",
        "getTableImport",
        "unlockTableImport",
        "updateTableImportSettings",
        "updateTableImportColumns",
        "updateTableImportDestination",
        "commitTableImport",
        "discardTableImport",
        "rerunTableImportWorksheet",
    }.issubset(operation_ids)
