from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from frisket.engine.executor import ImportWorkloadLimits
from frisket.server.app import create_app
from frisket.server.services.import_bulk import BulkImportLimits
from frisket.server.services import import_csv_analysis


def _raw_chunked_csv_upload(
    app: Any, *, pid: str, path: str, body: bytes, boundary: str
) -> tuple[int, int]:
    """Send a multipart CSV request in ASGI chunks without Content-Length."""

    async def invoke() -> tuple[int, int]:
        chunks = [body[:256], body[256:800], body[800:]]
        chunks = [chunk for chunk in chunks if chunk]
        body_reads = 0
        status = 0

        async def receive() -> dict[str, Any]:
            nonlocal body_reads
            if not chunks:
                return {"type": "http.disconnect"}
            body_reads += 1
            chunk = chunks.pop(0)
            return {
                "type": "http.request",
                "body": chunk,
                "more_body": bool(chunks),
            }

        async def send(message: dict[str, Any]) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])

        request_path = f"/api/projects/{pid}/import/csv{path}"
        await app(
            {
                "type": "http",
                "asgi": {"version": "3.0", "spec_version": "2.3"},
                "http_version": "1.1",
                "method": "POST",
                "scheme": "http",
                "path": request_path,
                "raw_path": request_path.encode(),
                "query_string": b"",
                "root_path": "",
                "headers": [
                    (
                        b"content-type",
                        f"multipart/form-data; boundary={boundary}".encode(),
                    ),
                    (b"transfer-encoding", b"chunked"),
                ],
                "client": ("127.0.0.1", 12345),
                "server": ("testserver", 80),
                "state": {},
            },
            receive,
            send,
        )
        return status, body_reads

    return asyncio.run(invoke())


def _multipart_csv_body(
    *, boundary: str, filename: str, content: bytes = b"x"
) -> bytes:
    return b"".join(
        [
            f"--{boundary}\r\n".encode(),
            (
                b'Content-Disposition: form-data; name="file"; filename="'
                + filename.encode()
                + b'"\r\n'
            ),
            b"Content-Type: text/csv\r\n\r\n",
            content,
            b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ]
    )


def test_csv_upload_appends_to_explicit_compatible_sheet_and_allows_intentional_repeat(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    project_id = client.post("/api/projects", json={"name": "Append CSV"}).json()["id"]
    created = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json={
            "action_id": "import.rows",
            "scope": {"kind": "project"},
            "sheet_name": "People",
            "params": {
                "columns": [{"name": "name", "type": "text"}],
                "rows": [{"name": "Ada"}],
            },
            "idempotency_key": "seed-people",
        },
    ).json()
    sheet_id = created["outputs"][0]["sheet_id"]

    for request_key in ("first", "first", "second"):
        response = client.post(
            f"/api/projects/{project_id}/import/csv/append",
            data={
                "destination_sheet_id": sheet_id,
                "append_request_key": request_key,
                "column_mapping": json.dumps({"name": "name"}),
            },
            files={"file": ("people.csv", b"name\nGrace\n", "text/csv")},
        )
        assert response.status_code == 200, response.text
        assert response.json()["sheet_id"] == sheet_id
        assert response.json()["rows"] == 1
    project = client.app.state.workspace.get(project_id)
    assert project.row_count(sheet_id) == 3
    project.undo()
    undone_replay = client.post(
        f"/api/projects/{project_id}/import/csv/append",
        data={
            "destination_sheet_id": sheet_id,
            "append_request_key": "second",
            "column_mapping": json.dumps({"name": "name"}),
        },
        files={"file": ("people.csv", b"name\nGrace\n", "text/csv")},
    )
    assert undone_replay.status_code == 409, undone_replay.text
    assert undone_replay.json()["errors"][0]["code"] == "stale_replay"
    assert project.row_count(sheet_id) == 2
    project.redo()
    conflicting_retry = client.post(
        f"/api/projects/{project_id}/import/csv/append",
        data={
            "destination_sheet_id": sheet_id,
            "append_request_key": "first",
            "column_mapping": json.dumps({"name": "name"}),
        },
        files={"file": ("people.csv", b"name\nKatherine\n", "text/csv")},
    )
    assert conflicting_retry.status_code == 409, conflicting_retry.text
    assert project.row_count(sheet_id) == 3


def test_csv_append_refuses_duplicate_or_missing_destination_mapping(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(tmp_path / "workspace-mapping"))
    project_id = client.post("/api/projects", json={"name": "CSV mapping"}).json()["id"]
    seeded = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json={
            "action_id": "import.rows",
            "scope": {"kind": "project"},
            "sheet_name": "People",
            "params": {
                "columns": [
                    {"name": "id", "type": "text"},
                    {"name": "name", "type": "text"},
                ],
                "rows": [{"id": "a", "name": "Ada"}],
            },
            "idempotency_key": "seed-csv-mapping",
        },
    ).json()
    sheet_id = seeded["outputs"][0]["sheet_id"]

    for request_key, mapping in (
        ("duplicate", {"id": "id", "name": "id"}),
        ("missing", {"id": "missing", "name": "name"}),
    ):
        response = client.post(
            f"/api/projects/{project_id}/import/csv/append",
            data={
                "destination_sheet_id": sheet_id,
                "append_request_key": request_key,
                "column_mapping": json.dumps(mapping),
            },
            files={"file": ("people.csv", b"id,name\nb,Grace\n", "text/csv")},
        )
        assert response.status_code == 400, response.text
    assert client.app.state.workspace.get(project_id).row_count(sheet_id) == 1


def test_csv_update_preview_binds_file_mapping_and_live_values(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path / "workspace-update"))
    project_id = client.post("/api/projects", json={"name": "Update CSV"}).json()["id"]
    created = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json={
            "action_id": "import.rows",
            "scope": {"kind": "project"},
            "sheet_name": "People",
            "params": {
                "columns": [
                    {"name": "id", "type": "text"},
                    {"name": "name", "type": "text"},
                ],
                "rows": [{"id": "a", "name": "Ada"}],
            },
            "idempotency_key": "seed-update-people",
        },
    ).json()
    sheet_id = created["outputs"][0]["sheet_id"]
    data = {
        "destination_sheet_id": sheet_id,
        "column_mapping": json.dumps({"id": "id", "name": "name"}),
        "key_columns": json.dumps(["id"]),
        "keep_existing_on_blank": "false",
    }
    uploaded = {
        "file": ("people.csv", b"id,name\na,Augusta\nmissing,Nobody\n", "text/csv")
    }
    preview = client.post(
        f"/api/projects/{project_id}/import/csv/update/preview",
        data=data,
        files=uploaded,
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["matched"] == 1
    assert preview.json()["unmatched"] == 1
    assert preview.json()["changed_cells"] == 1

    applied = client.post(
        f"/api/projects/{project_id}/import/csv/update",
        data={
            **data,
            "update_request_key": "update-csv-1",
            "confirmation": preview.json()["confirmation"],
        },
        files=uploaded,
    )
    assert applied.status_code == 200, applied.text
    project = client.app.state.workspace.get(project_id)
    name_column = next(
        row for row in project.columns(sheet_id) if row["name"] == "name"
    )
    assert list(project.get_values(sheet_id, name_column["id"]).values()) == ["Augusta"]
    project.undo()
    assert list(project.get_values(sheet_id, name_column["id"]).values()) == ["Ada"]


def test_csv_update_workload_refusal_reports_update_action(tmp_path: Path) -> None:
    client = TestClient(
        create_app(
            tmp_path / "workspace-update-limit",
            import_workload_limits=ImportWorkloadLimits(max_rows=1),
        )
    )
    project_id = client.post("/api/projects", json={"name": "Update limit"}).json()[
        "id"
    ]
    created = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json={
            "action_id": "import.rows",
            "scope": {"kind": "project"},
            "sheet_name": "People",
            "params": {
                "columns": [
                    {"name": "id", "type": "text"},
                    {"name": "name", "type": "text"},
                ],
                "rows": [{"id": "a", "name": "Ada"}],
            },
            "idempotency_key": "seed-update-limit",
        },
    ).json()
    data = {
        "destination_sheet_id": created["outputs"][0]["sheet_id"],
        "column_mapping": json.dumps({"id": "id", "name": "name"}),
        "key_columns": json.dumps(["id"]),
    }
    uploaded = {"file": ("people.csv", b"id,name\na,Augusta\nb,Bob\n", "text/csv")}
    preview = client.post(
        f"/api/projects/{project_id}/import/csv/update/preview",
        data=data,
        files=uploaded,
    )
    assert preview.status_code == 400
    assert "import.update_csv row count exceeds" in preview.json()["detail"]

    response = client.post(
        f"/api/projects/{project_id}/import/csv/update",
        data={
            **data,
            "confirmation": "sha256:not-reached",
            "update_request_key": "over-limit-update",
        },
        files=uploaded,
    )

    assert response.status_code == 400
    assert response.json()["action"]["kind"] == "import.update_csv"
    assert response.json()["errors"][0]["code"] == "import_workload_limit_exceeded"


def test_csv_update_malformed_numeric_value_is_a_400_for_preview_and_apply(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(tmp_path / "workspace-update-invalid"))
    project_id = client.post("/api/projects", json={"name": "Invalid CSV"}).json()["id"]
    created = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json={
            "action_id": "import.rows",
            "scope": {"kind": "project"},
            "sheet_name": "Stock",
            "params": {
                "columns": [
                    {"name": "sku", "type": "text"},
                    {"name": "qty", "type": "integer"},
                ],
                "rows": [{"sku": "A1", "qty": 3}],
            },
            "idempotency_key": "seed-invalid-csv",
        },
    ).json()
    data = {
        "destination_sheet_id": created["outputs"][0]["sheet_id"],
        "column_mapping": json.dumps({"code": "sku", "amount": "qty"}),
        "key_columns": json.dumps(["sku"]),
    }
    uploaded = {"file": ("invalid.csv", b"code,amount\nA1,nope\n", "text/csv")}

    preview = client.post(
        f"/api/projects/{project_id}/import/csv/update/preview",
        data=data,
        files=uploaded,
    )
    assert preview.status_code == 400

    applied = client.post(
        f"/api/projects/{project_id}/import/csv/update",
        data={
            **data,
            "confirmation": "sha256:not-reached",
            "update_request_key": "invalid-csv-apply",
        },
        files=uploaded,
    )
    assert applied.status_code == 400
    assert applied.json()["errors"][0]["code"] == "invalid_csv_value"


def test_direct_csv_request_limit_rejects_multipart_envelopes_before_staging(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    app = create_app(
        workspace,
        bulk_import_limits=BulkImportLimits(max_request_bytes=512),
    )
    client = TestClient(app)
    pid = client.post("/api/projects", json={"name": "CSV envelope quota"}).json()["id"]

    response = client.post(
        f"/api/projects/{pid}/import/csv",
        files={"file": (f"{'m' * 1024}.csv", b"name\nAda\n", "text/csv")},
    )

    assert response.status_code == 413
    assert response.json() == {"detail": "CSV import request exceeds deployment limit"}
    assert app.state.workspace.get(pid).sheets(include_hidden=True) == []


def test_csv_preview_request_limit_rejects_chunked_body_before_parsing(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    app = create_app(
        workspace,
        bulk_import_limits=BulkImportLimits(max_request_bytes=512),
    )
    client = TestClient(app)
    pid = client.post(
        "/api/projects", json={"name": "CSV preview envelope quota"}
    ).json()["id"]
    boundary = "frisket-csv-envelope-boundary"
    body = _multipart_csv_body(boundary=boundary, filename=f"{'m' * 1024}.csv")
    assert len(body) > 800

    status, body_reads = _raw_chunked_csv_upload(
        app,
        pid=pid,
        path="/preview",
        body=body,
        boundary=boundary,
    )

    assert status == 413
    assert body_reads == 2, "request must stop reading as soon as the limit is crossed"


def test_direct_csv_publication_keeps_reversible_storage_and_public_receipt(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = client.post("/api/projects", json={"name": "CSV publication"}).json()["id"]

    imported = client.post(
        f"/api/projects/{pid}/import/csv",
        files={"file": ("records.csv", b"name\nAda\n", "text/csv")},
    )

    assert imported.status_code == 200, imported.text
    sheet_id = imported.json()["sheet_id"]
    project = client.app.state.workspace.get(pid)
    ops = project.history()
    assert len(ops) == 1
    op = ops[0]
    assert op["kind"] == "import.csv"
    assert op["barrier"] == 0
    assert json.loads(op["undo_info"]) == {"created_sheets": [sheet_id]}
    spec = json.loads(op["spec"])
    assert spec["action_id"] == "import.csv"
    assert spec["sheet_name"] == "records"
    assert spec["scope"] == {"kind": "project"}
    assert spec["reads"][0]["kind"] == "local_file_read"

    receipt_rows = project.db.execute(
        "SELECT action_kind, status, body FROM receipts"
    ).fetchall()
    assert len(receipt_rows) == 1
    receipt_row = receipt_rows[0]
    assert receipt_row["action_kind"] == "import.csv"
    assert receipt_row["status"] == "completed"
    receipt = json.loads(receipt_row["body"])
    assert receipt["action_kind"] == "import.csv"
    assert receipt["status"] == "completed"
    assert receipt["op_ids"] == [op["id"]]

    assert project.undo() == op["id"]
    assert client.get(f"/api/projects/{pid}/sheets").json() == []
    assert project.redo() == op["id"]
    assert [
        sheet["id"] for sheet in client.get(f"/api/projects/{pid}/sheets").json()
    ] == [sheet_id]


def test_direct_csv_retry_replays_and_malformed_upload_is_not_published(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    client = TestClient(create_app(workspace))
    pid = client.post("/api/projects", json={"name": "CSV retry"}).json()["id"]
    upload = {"file": ("rows.csv", b"value,note\n1,a\n,b\n2,c\n", "text/csv")}
    first = client.post(f"/api/projects/{pid}/import/csv", files=upload)
    second = client.post(f"/api/projects/{pid}/import/csv", files=upload)
    assert first.status_code == second.status_code == 200
    assert first.json()["sheet_id"] == second.json()["sheet_id"]
    assert len(client.get(f"/api/projects/{pid}/sheets").json()) == 1
    assert _sheet_cells(client, pid, first.json()["sheet_id"])[1]["value"] is None

    malformed = client.post(
        f"/api/projects/{pid}/import/csv",
        files={"file": ("broken.csv", b'name,amount\n"unterminated,1\n', "text/csv")},
    )
    assert malformed.status_code == 400


def test_direct_csv_blank_cells_remain_null_when_retyped_to_date(
    tmp_path: Path,
) -> None:
    """Streamed CSV keeps the legacy nullable-blank behavior for all spellings."""
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = client.post("/api/projects", json={"name": "CSV blank dates"}).json()["id"]
    imported = client.post(
        f"/api/projects/{pid}/import/csv",
        files={
            "file": (
                "events.csv",
                "event_date,title\n"
                "2026-01-15,dated\n"
                ",unquoted blank\n"
                '"",quoted blank\n',
                "text/csv",
            )
        },
    )
    assert imported.status_code == 200, imported.text
    sheet_id = int(imported.json()["sheet_id"])
    before = client.get(f"/api/projects/{pid}/sheets/{sheet_id}/data").json()
    event_date_id = next(
        int(column["id"])
        for column in before["columns"]
        if column["name"] == "event_date"
    )
    assert [row["cells"].get(str(event_date_id)) for row in before["rows"]] == [
        "2026-01-15",
        None,
        None,
    ]

    retyped = client.post(
        f"/api/projects/{pid}/actions/v1/run",
        json={
            "action_id": "column.set_type",
            "scope": {"kind": "project"},
            "params": {"column_id": event_date_id, "type": "date"},
            "idempotency_key": "csv-blank-date@sha256:v1",
        },
    )
    assert retyped.status_code == 200, retyped.text


def test_direct_csv_hidden_name_collision_and_encoding_are_in_idempotency_key(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = client.post("/api/projects", json={"name": "CSV identity"}).json()["id"]
    project = client.app.state.workspace.get(pid)
    hidden_id = project.add_sheet("rows")
    project.db.execute("UPDATE sheets SET hidden=1 WHERE id=?", (hidden_id,))
    project.db.commit()
    raw = b"name\n\x93hello\x94\n"

    windows = client.post(
        f"/api/projects/{pid}/import/csv?encoding=cp1252",
        files={"file": ("rows.csv", raw, "text/csv")},
    )
    macintosh = client.post(
        f"/api/projects/{pid}/import/csv?encoding=mac_roman",
        files={"file": ("rows.csv", raw, "text/csv")},
    )
    replay = client.post(
        f"/api/projects/{pid}/import/csv?encoding=mac_roman",
        files={"file": ("rows.csv", raw, "text/csv")},
    )
    assert windows.status_code == macintosh.status_code == replay.status_code == 200
    assert windows.json()["sheet_id"] != macintosh.json()["sheet_id"]
    assert replay.json()["sheet_id"] == macintosh.json()["sheet_id"]
    visible = project.sheets()
    assert [row["name"] for row in visible] == ["rows-2", "rows-3"]
    assert _sheet_cells(client, pid, windows.json()["sheet_id"])[0]["name"] == "“hello”"
    assert (
        _sheet_cells(client, pid, macintosh.json()["sheet_id"])[0]["name"] != "“hello”"
    )


def test_direct_csv_preserves_markdown_format_and_split_utf16_decode(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(import_csv_analysis, "_PATH_READ_BYTES", 3)
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = client.post("/api/projects", json={"name": "UTF16 CSV"}).json()["id"]
    raw = "value;notes\n1;# Lead\n;## Follow-up\n".encode("utf-16")
    response = client.post(
        f"/api/projects/{pid}/import/csv",
        files={"file": ("rows.csv", raw, "text/csv")},
    )
    assert response.status_code == 200, response.text
    data = client.get(
        f"/api/projects/{pid}/sheets/{response.json()['sheet_id']}/data"
    ).json()
    columns = {column["name"]: column for column in data["columns"]}
    assert columns["value"]["type"] == "integer"
    assert columns["notes"]["format"] == "markdown"
    assert _sheet_cells(client, pid, response.json()["sheet_id"])[1]["value"] is None


def test_import_csv_route_declares_current_multipart_transport_and_response(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = client.post("/api/projects", json={"name": "CSV Upload"}).json()["id"]

    operation = client.get("/openapi.json").json()["paths"][
        "/api/projects/{pid}/import/csv"
    ]["post"]
    assert operation.get("x-frisket-v1-transport") == {
        "state": "v1_product_transport",
        "transport": "multipart_upload",
        "action_kind": "import.csv",
        "v1_task": "v1-http-import-csv-upload-bridge",
        "canonical_action_route": "/api/projects/{pid}/actions/v1/run#import.csv",
    }

    response = client.post(
        f"/api/projects/{pid}/import/csv",
        files={
            "file": (
                "rows.csv",
                'Name,Score,Notes\nAda,1,"# Heading"\n',
                "text/csv",
            )
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["rows"] == 1
    assert body["columns"] == ["Name", "Score", "Notes"]
    assert isinstance(body["sheet_id"], int)


def test_import_csv_route_declares_its_multipart_and_json_contract(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))

    operation = client.get("/openapi.json").json()["paths"][
        "/api/projects/{pid}/import/csv"
    ]["post"]

    assert operation["requestBody"] == {
        "required": True,
        "content": {
            "multipart/form-data": {
                "schema": {
                    "$ref": "#/components/schemas/Body_import_csv_api_projects__pid__import_csv_post"
                }
            }
        },
    }
    assert operation["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ImportCsvResponse"
    }
    assert set(operation["responses"]) == {
        "200",
        "400",
        "401",
        "402",
        "403",
        "404",
        "409",
        "413",
        "422",
        "500",
    }


def test_import_csv_preview_declares_its_multipart_and_json_contract(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))

    operation = client.get("/openapi.json").json()["paths"][
        "/api/projects/{pid}/import/csv/preview"
    ]["post"]

    assert operation["requestBody"]["required"] is True
    assert set(operation["requestBody"]["content"]) == {"multipart/form-data"}
    assert operation["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ImportCsvPreviewResponse"
    }
    assert set(operation["responses"]) == {
        "200",
        "400",
        "401",
        "403",
        "404",
        "413",
        "422",
        "500",
    }


def test_import_csv_empty_file_validation_error_maps_to_400(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = client.post("/api/projects", json={"name": "CSV Upload"}).json()["id"]

    response = client.post(
        f"/api/projects/{pid}/import/csv",
        files={"file": ("empty.csv", "", "text/csv")},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "empty CSV"


# --- Encoding: the uploaded bytes are the user's data, not a draft ------------
#
# Excel on Windows and most municipal open-data portals emit cp1252, not
# UTF-8.  The multipart transport used to decode with errors="replace" and stage the
# *replaced* text, so "Muñoz" arrived as "Mu�oz" and the original
# bytes were gone -- with a 200 and a success payload.

CP1252_BODY = "name,city\nMuñoz,Cañon City\nO'Brien,Ñuñoa\n"


def _sheet_cells(client: TestClient, pid: str, sheet_id: int) -> list[dict]:
    data = client.get(f"/api/projects/{pid}/sheets/{sheet_id}/data").json()
    names = {column["id"]: column["name"] for column in data["columns"]}
    return [
        {names[int(cid)]: value for cid, value in row["cells"].items()}
        for row in data["rows"]
    ]


def test_import_csv_preview_is_non_mutating_and_matches_import_analysis(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path / "workspace"
    client = TestClient(create_app(workspace_root))
    pid = client.post("/api/projects", json={"name": "CSV Preview"}).json()["id"]
    raw = "name;score;notes\nMuñoz;1,5;# Lead\nAna;2,0;Plain\n".encode("cp1252")

    preview = client.post(
        f"/api/projects/{pid}/import/csv/preview",
        files={"file": ("permits.csv", raw, "text/csv")},
    )

    assert preview.status_code == 200, preview.text
    assert preview.json() == {
        "encoding": "cp1252",
        "delimiter": ";",
        "decimal_separator": ",",
        "row_count": 2,
        "columns": [
            {"name": "name", "type": "text", "format": None},
            {"name": "score", "type": "number", "format": None},
            {"name": "notes", "type": "text", "format": None},
        ],
        "preview_rows": [
            {"name": "Muñoz", "score": "1,5", "notes": "# Lead"},
            {"name": "Ana", "score": "2,0", "notes": "Plain"},
        ],
        "truncated": False,
    }

    imported = client.post(
        f"/api/projects/{pid}/import/csv",
        files={"file": ("permits.csv", raw, "text/csv")},
    )
    assert imported.status_code == 200, imported.text
    assert imported.json()["encoding"] == preview.json()["encoding"]
    assert imported.json()["columns"] == [
        column["name"] for column in preview.json()["columns"]
    ]


def test_import_csv_preview_reparses_when_encoding_changes(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = client.post("/api/projects", json={"name": "CSV Preview"}).json()["id"]
    raw = b'name,quote\nAna,"\x93hello\x94"\n'

    windows = client.post(
        f"/api/projects/{pid}/import/csv/preview?encoding=cp1252",
        files={"file": ("quotes.csv", raw, "text/csv")},
    )
    macintosh = client.post(
        f"/api/projects/{pid}/import/csv/preview?encoding=mac_roman",
        files={"file": ("quotes.csv", raw, "text/csv")},
    )

    assert windows.status_code == 200, windows.text
    assert macintosh.status_code == 200, macintosh.text
    assert windows.json()["preview_rows"][0]["quote"] == "“hello”"
    assert macintosh.json()["preview_rows"][0]["quote"] != "“hello”"
    assert windows.json()["encoding"] == "cp1252"
    assert macintosh.json()["encoding"] == "mac_roman"


def test_import_csv_upload_reads_cp1252_without_replacing_characters(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path / "workspace"
    client = TestClient(create_app(workspace_root))
    pid = client.post("/api/projects", json={"name": "CSV Upload"}).json()["id"]
    raw = CP1252_BODY.encode("cp1252")

    response = client.post(
        f"/api/projects/{pid}/import/csv",
        files={"file": ("permits.csv", raw, "text/csv")},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["encoding"] == "cp1252"
    rows = _sheet_cells(client, pid, body["sheet_id"])
    assert [row["name"] for row in rows] == ["Muñoz", "O'Brien"]
    assert [row["city"] for row in rows] == ["Cañon City", "Ñuñoa"]
    assert "�" not in str(rows)


def test_import_csv_upload_honors_an_explicit_encoding(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    client = TestClient(create_app(workspace_root))
    pid = client.post("/api/projects", json={"name": "CSV Upload"}).json()["id"]
    raw = "name\nJérôme\n".encode("iso-8859-2")

    response = client.post(
        f"/api/projects/{pid}/import/csv?encoding=iso-8859-2",
        files={"file": ("permits.csv", raw, "text/csv")},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["encoding"] == "iso-8859-2"
    assert _sheet_cells(client, pid, body["sheet_id"]) == [{"name": "Jérôme"}]


def test_import_csv_upload_refuses_undecodable_bytes_and_names_the_knob(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = client.post("/api/projects", json={"name": "CSV Upload"}).json()["id"]
    # 0x81 is unassigned in cp1252 and illegal in UTF-8: no ladder rung decodes it.
    raw = b"name\nA\x81B\n"

    response = client.post(
        f"/api/projects/{pid}/import/csv",
        files={"file": ("permits.csv", raw, "text/csv")},
    )

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "utf-8" in detail and "cp1252" in detail
    assert "encoding=" in detail


def test_import_csv_upload_refuses_a_wrong_explicit_encoding(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = client.post("/api/projects", json={"name": "CSV Upload"}).json()["id"]

    response = client.post(
        f"/api/projects/{pid}/import/csv?encoding=ascii",
        files={"file": ("permits.csv", CP1252_BODY.encode("cp1252"), "text/csv")},
    )

    assert response.status_code == 400
    assert "ascii" in response.json()["detail"]


def test_import_csv_upload_rejects_an_unknown_encoding_name(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = client.post("/api/projects", json={"name": "CSV Upload"}).json()["id"]

    response = client.post(
        f"/api/projects/{pid}/import/csv?encoding=not-a-codec",
        files={"file": ("permits.csv", b"name\nAda\n", "text/csv")},
    )

    assert response.status_code == 400
    assert "not-a-codec" in response.json()["detail"]


def test_import_csv_preview_and_upload_reject_non_text_codecs(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = client.post("/api/projects", json={"name": "CSV Upload"}).json()["id"]

    for route in ("preview", ""):
        suffix = f"/{route}" if route else ""
        response = client.post(
            f"/api/projects/{pid}/import/csv{suffix}?encoding=base64_codec",
            files={"file": ("rows.csv", b"name\nAda\n", "text/csv")},
        )
        assert response.status_code == 400
        detail = response.json()["detail"]
        assert "unsupported CSV encoding" in detail
        assert "base64_codec" in detail


def test_explicit_utf8_strips_the_bom_from_preview_and_import_headers(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = client.post("/api/projects", json={"name": "CSV Upload"}).json()["id"]
    raw = b"\xef\xbb\xbfname,city\nAda,London\n"

    preview = client.post(
        f"/api/projects/{pid}/import/csv/preview?encoding=utf-8",
        files={"file": ("people.csv", raw, "text/csv")},
    )
    imported = client.post(
        f"/api/projects/{pid}/import/csv?encoding=utf-8",
        files={"file": ("people.csv", raw, "text/csv")},
    )

    assert preview.status_code == 200, preview.text
    assert preview.json()["encoding"] == "utf-8-sig"
    assert [column["name"] for column in preview.json()["columns"]] == [
        "name",
        "city",
    ]
    assert imported.status_code == 200, imported.text
    assert imported.json()["encoding"] == "utf-8-sig"
    assert imported.json()["columns"] == ["name", "city"]


def test_import_csv_upload_reads_utf16_from_its_bom(tmp_path: Path) -> None:
    """Excel's "Unicode Text" export is UTF-16; its NUL bytes are legal UTF-8."""
    workspace_root = tmp_path / "workspace"
    client = TestClient(create_app(workspace_root))
    pid = client.post("/api/projects", json={"name": "CSV Upload"}).json()["id"]
    raw = "name\tcity\nMuñoz\tOslo\n".encode("utf-16")

    response = client.post(
        f"/api/projects/{pid}/import/csv",
        files={"file": ("permits.csv", raw, "text/csv")},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["encoding"] == "utf-16"
    assert _sheet_cells(client, pid, body["sheet_id"]) == [
        {"name": "Muñoz", "city": "Oslo"}
    ]


# --- Type inference must not renumber identifiers -----------------------------


def test_import_csv_upload_keeps_leading_zero_identifiers_intact(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = client.post("/api/projects", json={"name": "CSV Upload"}).json()["id"]
    body = (
        "name,zip,fips,phone,case_no,score\n"
        "Alice,01234,06075,+12125551234,00042,7\n"
        "Bob,02138,01001,+13475550000,00043,9\n"
    )

    response = client.post(
        f"/api/projects/{pid}/import/csv",
        files={"file": ("voters.csv", body.encode("utf-8"), "text/csv")},
    )

    assert response.status_code == 200, response.text
    sheet_id = response.json()["sheet_id"]
    data = client.get(f"/api/projects/{pid}/sheets/{sheet_id}/data").json()
    types = {column["name"]: column["type"] for column in data["columns"]}
    assert types == {
        "name": "text",
        "zip": "text",
        "fips": "text",
        "phone": "text",
        "case_no": "text",
        "score": "integer",
    }
    assert _sheet_cells(client, pid, sheet_id)[0] == {
        "name": "Alice",
        "zip": "01234",
        "fips": "06075",
        "phone": "+12125551234",
        "case_no": "00042",
        "score": 7,
    }
