from __future__ import annotations

import io
import hashlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

from frisket.engine.executor import ImportWorkloadLimits
from frisket.server.app import create_app
from frisket.server.services.import_xlsx import ImportXlsxUploadService
from frisket.server.services.import_uploads import AdmittedUpload


def _xlsx_bytes(rows: list[list[object]]) -> bytes:
    workbook = Workbook()
    worksheet = workbook.active
    for row in rows:
        worksheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    workbook.close()
    return buffer.getvalue()


def _admitted(filename: str, raw: bytes) -> AdmittedUpload:
    return AdmittedUpload(
        filename,
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        io.BytesIO(raw),
        hashlib.sha256(raw).hexdigest(),
        len(raw),
    )


def _formula_xlsx_bytes(*, formula_column: str) -> bytes:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.append(["code", "amount", "calculation"])
    worksheet.append(["A1", 11, "ignored"])
    worksheet[f"{formula_column}2"] = "=1+1"
    buffer = io.BytesIO()
    workbook.save(buffer)
    workbook.close()
    return buffer.getvalue()


def _seed(tmp_path: Path) -> tuple[TestClient, str, int, ImportXlsxUploadService]:
    client = TestClient(create_app(tmp_path / "workspace"))
    project_id = client.post("/api/projects", json={"name": "XLSX update"}).json()["id"]
    imported = client.post(
        f"/api/projects/{project_id}/import/xlsx",
        files={
            "file": (
                "stock.xlsx",
                _xlsx_bytes([["sku", "qty"], ["A1", 3], ["B2", 8]]),
            )
        },
    )
    assert imported.status_code == 200, imported.text
    service = ImportXlsxUploadService(client.app.state.workspace)
    return client, project_id, imported.json()["sheet_id"], service


def test_xlsx_update_preview_is_bounded_and_does_not_mutate(tmp_path: Path) -> None:
    client, project_id, sheet_id, service = _seed(tmp_path)
    raw = _xlsx_bytes([["code", "amount"], ["A1", 11], ["missing", 4]])
    mapping = json.dumps({"code": "sku", "amount": "qty"})

    preview = service.preview_xlsx_update(
        project_id,
        upload=_admitted("incoming.xlsx", raw),
        destination_sheet_id=sheet_id,
        column_mapping=mapping,
        key_columns=json.dumps(["sku"]),
        keep_existing_on_blank=False,
    )

    assert preview["matched"] == 1
    assert preview["unmatched"] == 1
    assert preview["changed_cells"] == 1
    assert len(preview["samples"]) <= 20
    assert preview["confirmation"]
    data = client.get(f"/api/projects/{project_id}/sheets/{sheet_id}/data").json()
    qty = next(column for column in data["columns"] if column["name"] == "qty")
    assert data["rows"][0]["cells"][str(qty["id"])] == 3


def test_xlsx_update_apply_uses_exact_mapping_and_preview_confirmation(
    tmp_path: Path,
) -> None:
    client, project_id, sheet_id, service = _seed(tmp_path)
    raw = _xlsx_bytes([["code", "amount"], ["A1", 11]])
    mapping = json.dumps({"code": "sku", "amount": "qty"})
    keys = json.dumps(["sku"])
    preview = service.preview_xlsx_update(
        project_id,
        upload=_admitted("incoming.xlsx", raw),
        destination_sheet_id=sheet_id,
        column_mapping=mapping,
        key_columns=keys,
        keep_existing_on_blank=False,
    )

    applied = service.upload_xlsx(
        project_id,
        upload=_admitted("incoming.xlsx", raw),
        sheet_name=None,
        destination_sheet_id=sheet_id,
        append_request_key="update-1",
        column_mapping=mapping,
        update_key_columns=keys,
        keep_existing_on_blank=False,
        confirmation=preview["confirmation"],
    )

    assert applied.status_code == 200
    assert applied.payload == {
        "sheet_id": sheet_id,
        "rows": 1,
        "columns": ["code", "amount"],
    }
    data = client.get(f"/api/projects/{project_id}/sheets/{sheet_id}/data").json()
    qty = next(column for column in data["columns"] if column["name"] == "qty")
    assert data["rows"][0]["cells"][str(qty["id"])] == 11
    assert data["rows"][1]["cells"][str(qty["id"])] == 8


def test_xlsx_update_requires_the_exact_current_preview(tmp_path: Path) -> None:
    _client, project_id, sheet_id, service = _seed(tmp_path)
    raw = _xlsx_bytes([["sku", "qty"], ["A1", 11]])
    mapping = json.dumps({"sku": "sku", "qty": "qty"})

    refused = service.upload_xlsx(
        project_id,
        upload=_admitted("incoming.xlsx", raw),
        sheet_name=None,
        destination_sheet_id=sheet_id,
        append_request_key="update-stale",
        column_mapping=mapping,
        update_key_columns=json.dumps(["sku"]),
        confirmation="not-the-preview-confirmation",
    )

    assert refused.status_code == 400
    assert refused.force_json_response is True
    assert refused.payload["errors"][0]["code"] == "stale_import_preview"


@pytest.mark.parametrize(("keep", "changed", "cleared"), [(False, 1, 1), (True, 0, 0)])
def test_xlsx_empty_and_trailing_cells_follow_blank_policy(
    tmp_path: Path, keep: bool, changed: int, cleared: int
) -> None:
    _client, project_id, sheet_id, service = _seed(tmp_path)
    preview = service.preview_xlsx_update(
        project_id,
        upload=_admitted("blank.xlsx", _xlsx_bytes([["code", "amount"], ["A1"]])),
        destination_sheet_id=sheet_id,
        column_mapping=json.dumps({"code": "sku", "amount": "qty"}),
        key_columns=json.dumps(["sku"]),
        keep_existing_on_blank=keep,
    )

    assert (preview["matched"], preview["changed_cells"], preview["cleared_cells"]) == (
        1,
        changed,
        cleared,
    )


@pytest.mark.parametrize("formula_column", ["A", "B"])
def test_xlsx_update_preview_refuses_selected_formula_without_cached_result(
    tmp_path: Path, formula_column: str
) -> None:
    client, project_id, sheet_id, _service = _seed(tmp_path)
    response = client.post(
        f"/api/projects/{project_id}/import/xlsx/update/preview",
        data={
            "destination_sheet_id": sheet_id,
            "column_mapping": json.dumps(
                {"code": "sku", "amount": "qty", "calculation": None}
            ),
            "key_columns": json.dumps(["sku"]),
        },
        files={
            "file": ("formula.xlsx", _formula_xlsx_bytes(formula_column=formula_column))
        },
    )

    assert response.status_code == 400
    assert "formula has no cached result" in response.json()["detail"]


def test_xlsx_update_ignores_formula_in_unmapped_column(tmp_path: Path) -> None:
    _client, project_id, sheet_id, service = _seed(tmp_path)
    preview = service.preview_xlsx_update(
        project_id,
        upload=_admitted("formula.xlsx", _formula_xlsx_bytes(formula_column="C")),
        destination_sheet_id=sheet_id,
        column_mapping=json.dumps(
            {"code": "sku", "amount": "qty", "calculation": None}
        ),
        key_columns=json.dumps(["sku"]),
        keep_existing_on_blank=False,
    )

    assert preview["matched"] == 1
    assert preview["changed_cells"] == 1


def test_xlsx_update_apply_returns_typed_formula_refusal(tmp_path: Path) -> None:
    client, project_id, sheet_id, _service = _seed(tmp_path)
    response = client.post(
        f"/api/projects/{project_id}/import/xlsx/update",
        data={
            "destination_sheet_id": sheet_id,
            "update_request_key": "formula-apply",
            "confirmation": "sha256:not-reached",
            "column_mapping": json.dumps(
                {"code": "sku", "amount": "qty", "calculation": None}
            ),
            "key_columns": json.dumps(["sku"]),
        },
        files={"file": ("formula.xlsx", _formula_xlsx_bytes(formula_column="B"))},
    )

    assert response.status_code == 400
    assert response.json()["errors"][0]["code"] == "unresolved_xlsx_formula"


def test_xlsx_update_preview_uses_request_row_limit(tmp_path: Path) -> None:
    client = TestClient(
        create_app(
            tmp_path / "workspace-limit",
            import_workload_limits=ImportWorkloadLimits(max_rows=1),
        )
    )
    project_id = client.post("/api/projects", json={"name": "XLSX limit"}).json()["id"]
    created = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json={
            "action_id": "import.rows",
            "scope": {"kind": "project"},
            "sheet_name": "People",
            "params": {
                "columns": [
                    {"name": "sku", "type": "text"},
                    {"name": "qty", "type": "integer"},
                ],
                "rows": [{"sku": "A1", "qty": 3}],
            },
            "idempotency_key": "seed-xlsx-limit",
        },
    ).json()
    response = client.post(
        f"/api/projects/{project_id}/import/xlsx/update/preview",
        data={
            "destination_sheet_id": created["outputs"][0]["sheet_id"],
            "column_mapping": json.dumps({"code": "sku", "amount": "qty"}),
            "key_columns": json.dumps(["sku"]),
        },
        files={
            "file": (
                "over-limit.xlsx",
                _xlsx_bytes([["code", "amount"], ["A1", 4], ["B2", 5]]),
            )
        },
    )

    assert response.status_code == 400
    assert "import.update_xlsx row count exceeds" in response.json()["detail"]
