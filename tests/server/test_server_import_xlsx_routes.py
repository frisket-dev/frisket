from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

from fastapi.testclient import TestClient
from openpyxl import Workbook

from frisket.server.app import create_app
from frisket.server.services import import_xlsx as import_xlsx_service
from frisket.engine.executor import ImportWorkloadLimits


def _xlsx_bytes(rows: list[list[object]]) -> bytes:
    wb = Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    wb.close()
    return buf.getvalue()


def test_import_xlsx_route_preserves_v1_transport_and_legacy_shape(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = client.post("/api/projects", json={"name": "XLSX Upload"}).json()["id"]

    operation = client.get("/openapi.json").json()["paths"][
        "/api/projects/{pid}/import/xlsx"
    ]["post"]
    assert operation.get("x-frisket-v1-transport") == {
        "state": "v1_product_transport",
        "transport": "multipart_upload",
        "action_kind": "import.xlsx",
        "v1_task": "v1-import-xlsx-action-and-http-bridge",
        "canonical_action_route": "/api/projects/{pid}/actions/v1/run#import.xlsx",
    }
    assert operation["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ImportXlsxResponse"
    }
    assert operation["responses"]["400"]["content"]["application/json"]["schema"][
        "anyOf"
    ] == [
        {"$ref": "#/components/schemas/HttpError"},
        {"$ref": "#/components/schemas/ActionResult"},
    ]

    response = client.post(
        f"/api/projects/{pid}/import/xlsx",
        files={
            "file": (
                "stock.xlsx",
                _xlsx_bytes([["sku", "qty", "notes"], ["A1", 3, "# Heading"]]),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["rows"] == 1
    assert body["columns"] == ["sku", "qty", "notes"]
    assert isinstance(body["sheet_id"], int)
    data = client.get(f"/api/projects/{pid}/sheets/{body['sheet_id']}/data").json()
    columns = {column["name"]: column for column in data["columns"]}
    assert columns["qty"]["type"] == "integer"
    assert columns["notes"]["format"] == "markdown"
    cells = data["rows"][0]["cells"]
    assert cells[str(columns["qty"]["id"])] == 3
    assert cells[str(columns["notes"]["id"])] == "# Heading"


def test_import_xlsx_invalid_file_validation_error_maps_to_400(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = client.post("/api/projects", json={"name": "XLSX Upload"}).json()["id"]

    response = client.post(
        f"/api/projects/{pid}/import/xlsx",
        files={"file": ("bad.xlsx", b"not an xlsx", "application/octet-stream")},
    )

    assert response.status_code == 400
    assert response.json()["detail"].startswith("invalid xlsx:")


def test_xlsx_import_obeys_the_request_composed_row_limit(tmp_path: Path) -> None:
    client = TestClient(
        create_app(
            tmp_path / "workspace",
            import_workload_limits=ImportWorkloadLimits(max_rows=1),
        )
    )
    pid = client.post("/api/projects", json={"name": "XLSX limit"}).json()["id"]
    response = client.post(
        f"/api/projects/{pid}/import/xlsx",
        files={"file": ("book.xlsx", _xlsx_bytes([["name"], ["Ada"], ["Grace"]]))},
    )
    assert response.status_code == 400, response.text
    assert response.json()["errors"][0]["code"] == "import_workload_limit_exceeded"


def test_xlsx_append_reviews_mapping_and_replays_only_the_same_attempt(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = client.post("/api/projects", json={"name": "XLSX append"}).json()["id"]
    created = client.post(
        f"/api/projects/{pid}/import/xlsx",
        files={"file": ("target.xlsx", _xlsx_bytes([["amount"], [1]]))},
    )
    assert created.status_code == 200, created.text
    sheet_id = created.json()["sheet_id"]
    source = _xlsx_bytes([["incoming"], [2]])

    preview = client.post(
        f"/api/projects/{pid}/import/xlsx/preview",
        files={"file": ("source.xlsx", source)},
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["columns"] == [
        {"name": "incoming", "type": "integer", "format": None}
    ]

    def append(attempt: str):
        return client.post(
            f"/api/projects/{pid}/import/xlsx/append",
            files={"file": ("source.xlsx", source)},
            data={
                "destination_sheet_id": str(sheet_id),
                "append_request_key": attempt,
                "column_mapping": json.dumps({"incoming": "amount"}),
            },
        )

    first = append("attempt-1")
    replay = append("attempt-1")
    deliberate_repeat = append("attempt-2")
    assert (
        first.status_code == replay.status_code == deliberate_repeat.status_code == 200
    )
    assert (
        first.json()["rows"]
        == replay.json()["rows"]
        == deliberate_repeat.json()["rows"]
        == 1
    )
    data = client.get(f"/api/projects/{pid}/sheets/{sheet_id}/data").json()
    assert [next(iter(row["cells"].values())) for row in data["rows"]] == [1, 2, 2]
    invalid = client.post(
        f"/api/projects/{pid}/import/xlsx/append",
        files={"file": ("invalid.xlsx", _xlsx_bytes([["incoming"], ["not a number"]]))},
        data={
            "destination_sheet_id": str(sheet_id),
            "append_request_key": "invalid-value",
            "column_mapping": json.dumps({"incoming": "amount"}),
        },
    )
    assert invalid.status_code == 400, invalid.text
    assert invalid.json()["errors"][0]["code"] == "invalid_xlsx_value"
    assert client.app.state.workspace.get(pid).row_count(sheet_id) == 3
    project = client.app.state.workspace.get(pid)
    project.undo()
    undone_replay = append("attempt-2")
    assert undone_replay.status_code == 409, undone_replay.text
    assert undone_replay.json()["errors"][0]["code"] == "stale_replay"
    assert project.row_count(sheet_id) == 2


def test_xlsx_retry_preserves_allocated_name_and_original_name_intent(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = client.post("/api/projects", json={"name": "XLSX retry"}).json()["id"]
    endpoint = f"/api/projects/{pid}/import/xlsx"
    occupied = client.post(
        endpoint,
        files={"file": ("book.xlsx", _xlsx_bytes([["name"], ["Existing"]]))},
    )
    assert occupied.status_code == 200, occupied.text
    raw = _xlsx_bytes([["name"], ["Ada"], [None], ["Grace"]])
    upload = {"file": ("book.xlsx", raw)}
    first = client.post(endpoint, files=upload)
    assert first.status_code == 200, first.text
    project = client.app.state.workspace.get(pid)
    assert [sheet["name"] for sheet in project.sheets()] == ["book", "book-2"]
    replay = client.post(endpoint, files=upload)
    assert replay.status_code == 200, replay.text
    assert replay.json() == first.json()
    assert replay.json()["rows"] == 2
    assert len(project.sheets()) == 2

    # Asking explicitly for the previously allocated suffix is a different intent.
    renamed = client.post(endpoint, files=upload, params={"sheet_name": "book-2"})
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["sheet_id"] != first.json()["sheet_id"]
    assert [sheet["name"] for sheet in project.sheets()] == [
        "book",
        "book-2",
        "book-2-2",
    ]
    assert project.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 3


def test_xlsx_import_records_the_admitted_upload_read(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = client.post("/api/projects", json={"name": "Held XLSX"}).json()["id"]
    raw = _xlsx_bytes([["name"], ["Original"]])
    response = client.post(
        f"/api/projects/{pid}/import/xlsx",
        files={"file": ("book.xlsx", raw)},
    )
    assert response.status_code == 200, response.text
    sheet_id = response.json()["sheet_id"]
    data = client.get(f"/api/projects/{pid}/sheets/{sheet_id}/data").json()
    assert list(data["rows"][0]["cells"].values()) == ["Original"]
    project = client.app.state.workspace.get(pid)
    receipt = json.loads(project.db.execute("SELECT body FROM receipts").fetchone()[0])
    sheet_ref = next(
        output["ref"]
        for output in receipt["outputs"]
        if output["ref"].get("kind") == "materialized_sheet"
    )
    assert (
        sheet_ref["reads"][0]["sha256"] == f"sha256:{hashlib.sha256(raw).hexdigest()}"
    )
    assert sheet_ref["reads"][0]["byte_count"] == len(raw)


def test_xlsx_inspection_samples_fifty_nonempty_rows_and_counts_every_row() -> None:
    raw = _xlsx_bytes(
        [
            ["name", "quantity"],
            *[[f"row-{index}", index] for index in range(50)],
            [None, None],
            ["last", 51],
        ]
    )
    scan = import_xlsx_service.scan_xlsx_stream(
        io.BytesIO(raw), logical_path="rows.xlsx"
    )
    assert scan.headers == ["name", "quantity"]
    assert len(scan.sample_records) == 50
    assert scan.sample_records[-1] == {"name": "row-49", "quantity": "49"}
    assert scan.row_count == 51
