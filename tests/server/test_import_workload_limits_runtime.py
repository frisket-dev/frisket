from __future__ import annotations

from email.message import EmailMessage
import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

from frisket.contracts.action import ActionResult
from frisket.engine.executor import CellEditQueryLimits, ImportWorkloadLimits
from frisket.engine.executor.pdf_page_read import AdmittedPdfPageRenderer
from frisket.server.app import create_app
from frisket.server.services import import_bulk_execute


def _project(client: TestClient, name: str) -> str:
    response = client.post("/api/projects", json={"name": name})
    assert response.status_code == 200, response.text
    return str(response.json()["id"])


def _visible_sheets(client: TestClient, project_id: str) -> list[dict]:
    response = client.get(f"/api/projects/{project_id}/sheets")
    assert response.status_code == 200, response.text
    return response.json()


def _assert_no_import_residue(client: TestClient, project_id: str) -> None:
    project = client.app.state.workspace.get(project_id)
    assert project.sheets(include_hidden=True) == []
    assert {
        table: project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("blobs", "sheets", "ops", "receipts")
    } == {"blobs": 0, "sheets": 0, "ops": 0, "receipts": 0}
    assert _visible_sheets(client, project_id) == []


def _assert_limit_action_result(
    response,
    *,
    project_id: str,
    action_kind: str,
    row_count: int,
    max_rows: int,
) -> None:
    assert response.status_code == 400, response.text
    result = ActionResult.model_validate(response.json())
    assert result.status == "failed"
    assert result.project_id == project_id
    assert result.action.kind == action_kind
    assert result.outputs == []
    assert len(result.errors) == 1
    error = result.errors[0]
    assert error.code == "import_workload_limit_exceeded"
    assert error.message == (
        f"{action_kind} row count exceeds the deployment limit of {max_rows}"
    )
    assert error.details == {"row_count": row_count, "max_rows": max_rows}


def test_cell_edit_query_uses_create_app_deployment_limit(tmp_path: Path) -> None:
    client = TestClient(
        create_app(
            tmp_path / "workspace",
            cell_edit_query_limits=CellEditQueryLimits(max_rows=2),
        )
    )
    project_id = _project(client, "Hosted query edit limit")
    project = client.app.state.workspace.get(project_id)
    sheet_id = project.add_sheet("Tasks")
    columns = {
        "sequence": project.add_column(sheet_id, "sequence", "integer"),
        "status": project.add_column(sheet_id, "status", "text"),
    }
    project.add_rows(
        sheet_id,
        [{"sequence": index, "status": "open"} for index in range(3)],
        columns,
    )
    before = {
        table: project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("ops", "edits", "receipts")
    }

    response = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json={
            "action_id": "cell.edit_query",
            "scope": {"kind": "project"},
            "params": {
                "query": {
                    "schema_version": "frisket.query.v1",
                    "kind": "sheet.filter",
                    "scope": {"kind": "sheet", "sheet_id": sheet_id},
                    "filter": {"status": {"eq": "open"}},
                    "sort": [{"column": "sequence", "dir": "asc"}],
                },
                "column_id": columns["status"],
                "value": "closed",
            },
            "idempotency_key": "cell-edit-query-limit@sha256:three",
        },
    )

    assert response.status_code == 400, response.text
    result = ActionResult.model_validate(response.json())
    assert result.status == "failed"
    assert result.errors[0].code == "query_rowset_too_large"
    assert result.errors[0].details == {"row_count": 3, "max_rows": 2}
    assert {
        table: project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("ops", "edits", "receipts")
    } == before


def _xlsx_bytes(rows: list[list[object]]) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    workbook.close()
    return buffer.getvalue()


def _two_page_pdf_bytes() -> bytes:
    contents = [
        b"BT /F1 24 Tf 72 700 Td (First page) Tj ET",
        b"BT /F1 24 Tf 72 700 Td (Second page) Tj ET",
    ]
    objects = [
        b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n",
        b"2 0 obj<</Type/Pages/Kids[3 0 R 4 0 R]/Count 2>>endobj\n",
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]"
        b"/Contents 5 0 R/Resources<</Font<</F1 7 0 R>>>>>>endobj\n",
        b"4 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]"
        b"/Contents 6 0 R/Resources<</Font<</F1 7 0 R>>>>>>endobj\n",
        b"5 0 obj<</Length "
        + str(len(contents[0])).encode()
        + b">>stream\n"
        + contents[0]
        + b"\nendstream\nendobj\n",
        b"6 0 obj<</Length "
        + str(len(contents[1])).encode()
        + b">>stream\n"
        + contents[1]
        + b"\nendstream\nendobj\n",
        b"7 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\n",
    ]
    document = bytearray(b"%PDF-1.4\n")
    offsets = []
    for obj in objects:
        offsets.append(len(document))
        document += obj
    xref = len(document)
    document += b"xref\n0 8\n0000000000 65535 f \n"
    for offset in offsets:
        document += f"{offset:010d} 00000 n \n".encode()
    document += b"trailer<</Size 8/Root 1 0 R>>\nstartxref\n"
    document += str(xref).encode() + b"\n%%EOF"
    return bytes(document)


def _bulk_plan(
    client: TestClient,
    project_id: str,
    uploads: list[tuple[str, bytes, str, str]],
) -> dict:
    parts: list[tuple[str, tuple[None, str] | tuple[str, bytes, str]]] = []
    for filename, content, content_type, logical_path in uploads:
        parts.extend(
            [
                ("files", (filename, content, content_type)),
                ("logical_paths", (None, logical_path)),
            ]
        )
    parts.append(("expand_archive", (None, "false")))
    response = client.post(f"/api/projects/{project_id}/import/bulk/plan", files=parts)
    assert response.status_code == 200, response.text
    return response.json()


def _execute_bulk(client: TestClient, project_id: str, plan: dict) -> dict:
    response = client.post(
        f"/api/projects/{project_id}/import/bulk/{plan['plan_id']}/execute",
        json={"decisions": {}},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _email(subject: str, *, attachment: bytes | None = None) -> bytes:
    message = EmailMessage()
    message["From"] = "reporter@example.test"
    message["To"] = "editor@example.test"
    message["Subject"] = subject
    message.set_content(f"body for {subject}")
    if attachment is not None:
        message.add_attachment(
            attachment,
            maintype="application",
            subtype="octet-stream",
            filename=f"{subject}.bin",
        )
    return message.as_bytes()


def _mbox(messages: list[bytes]) -> bytes:
    return b"".join(
        b"From reporter@example.test Tue Sep  2 12:34:56 2026\n" + message + b"\n"
        for message in messages
    )


def test_direct_streamed_csv_obeys_injected_row_limit_before_publication(
    tmp_path: Path,
) -> None:
    client = TestClient(
        create_app(
            tmp_path / "workspace",
            import_workload_limits=ImportWorkloadLimits(max_rows=2),
        )
    )
    project_id = _project(client, "Direct CSV hosted limit")

    response = client.post(
        f"/api/projects/{project_id}/import/csv",
        files={
            "file": (
                "over-limit.csv",
                b"name\nAda\nGrace\nLin\n",
                "text/csv",
            )
        },
    )

    assert response.status_code == 400, response.text
    result = ActionResult.model_validate(response.json())
    assert result.status == "failed"
    assert result.project_id == project_id
    assert result.action.kind == "import.csv"
    assert result.outputs == []
    error = result.errors[0]
    assert error.code == "import_workload_limit_exceeded"
    assert error.message == "import.csv row count exceeds the deployment limit of 2"
    assert error.details == {"row_count": 3, "max_rows": 2}
    assert _visible_sheets(client, project_id) == []


def test_bulk_combined_csv_obeys_injected_row_limit_before_publication(
    tmp_path: Path,
) -> None:
    client = TestClient(
        create_app(
            tmp_path / "workspace",
            import_workload_limits=ImportWorkloadLimits(max_rows=2),
        )
    )
    project_id = _project(client, "Bulk CSV hosted limit")
    plan = _bulk_plan(
        client,
        project_id,
        [
            ("first.csv", b"name\nAda\n", "text/csv", "mail/first.csv"),
            (
                "second.csv",
                b"name\nGrace\nLin\n",
                "text/csv",
                "mail/second.csv",
            ),
        ],
    )
    question = plan["questions"][0]
    assert question["kind"] == "csv_combine"
    response = client.post(
        f"/api/projects/{project_id}/import/bulk/{plan['plan_id']}/execute",
        json={"decisions": {question["id"]: "combine"}},
    )

    assert response.status_code == 200, response.text
    result = response.json()
    assert result["created"] == []
    assert result["failed"] == [
        {
            "id": plan["proposed_outputs"][0]["id"],
            "kind": "csv_group",
            "logical_paths": ["mail/first.csv", "mail/second.csv"],
            "error": "import.csv row count exceeds the deployment limit of 2",
        }
    ]
    assert _visible_sheets(client, project_id) == []


@pytest.mark.parametrize(
    ("uploads", "name"),
    [
        (
            [
                ("one.eml", _email("one"), "message/rfc822", "mail/one.eml"),
                ("two.eml", _email("two"), "message/rfc822", "mail/two.eml"),
                ("three.eml", _email("three"), "message/rfc822", "mail/three.eml"),
            ],
            "EML",
        ),
        (
            [
                (
                    "archive.mbox",
                    _mbox([_email("one"), _email("two"), _email("three")]),
                    "application/mbox",
                    "mail/archive.mbox",
                )
            ],
            "MBOX",
        ),
    ],
)
def test_bulk_email_obeys_injected_row_limit_before_publication(
    tmp_path: Path,
    uploads: list[tuple[str, bytes, str, str]],
    name: str,
) -> None:
    client = TestClient(
        create_app(
            tmp_path / "workspace",
            import_workload_limits=ImportWorkloadLimits(max_rows=2),
        )
    )
    project_id = _project(client, f"Bulk {name} hosted limit")
    plan = _bulk_plan(client, project_id, uploads)

    result = _execute_bulk(client, project_id, plan)

    assert result["created"] == []
    assert result["failed"] == [
        {
            "id": plan["proposed_outputs"][0]["id"],
            "kind": "email",
            "logical_paths": plan["proposed_outputs"][0]["logical_paths"],
            "error": "import.email row count exceeds the deployment limit of 2",
        }
    ]
    assert _visible_sheets(client, project_id) == []


def test_bulk_email_over_limit_closes_ingress_without_attachment_or_project_residue(
    tmp_path: Path,
    monkeypatch,
) -> None:
    client = TestClient(
        create_app(
            tmp_path / "workspace",
            import_workload_limits=ImportWorkloadLimits(max_rows=2),
        )
    )
    project_id = _project(client, "Bulk email attachment hosted limit")
    uploads = [
        (
            f"{subject}.eml",
            _email(subject, attachment=f"evidence-{subject}".encode()),
            "message/rfc822",
            f"mail/{subject}.eml",
        )
        for subject in ("one", "two", "three")
    ]
    plan = _bulk_plan(client, project_id, uploads)
    held_streams = []
    run_action = import_bulk_execute.run_action_spec

    def capture_ingress(project, action, **kwargs):
        assert action["action_id"] == "import.email"
        admitted = kwargs["deps"].email_sources
        assert set(admitted) == {
            source["source_ref"] for source in action["params"]["sources"]
        }
        held_streams.extend(source.stream for source in admitted.values())
        assert len(held_streams) == 3
        assert all(not stream.closed for stream in held_streams)
        result = run_action(project, action, **kwargs)
        assert all(not stream.closed for stream in held_streams)
        return result

    monkeypatch.setattr(import_bulk_execute, "run_action_spec", capture_ingress)

    result = _execute_bulk(client, project_id, plan)

    assert len(held_streams) == 3
    assert all(stream.closed for stream in held_streams)
    assert result["created"] == []
    assert result["failed"] == [
        {
            "id": plan["proposed_outputs"][0]["id"],
            "kind": "email",
            "logical_paths": plan["proposed_outputs"][0]["logical_paths"],
            "error": "import.email row count exceeds the deployment limit of 2",
        }
    ]
    project = client.app.state.workspace.get(project_id)
    assert {
        table: project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("blobs", "sheets", "ops", "receipts")
    } == {"blobs": 0, "sheets": 0, "ops": 0, "receipts": 0}
    assert _visible_sheets(client, project_id) == []


def test_direct_xlsx_obeys_injected_row_limit_before_publication(
    tmp_path: Path,
) -> None:
    client = TestClient(
        create_app(
            tmp_path / "workspace",
            import_workload_limits=ImportWorkloadLimits(max_rows=1),
        )
    )
    project_id = _project(client, "Direct XLSX hosted limit")

    response = client.post(
        f"/api/projects/{project_id}/import/xlsx",
        files={
            "file": (
                "over-limit.xlsx",
                _xlsx_bytes([["name"], ["Ada"], ["Grace"]]),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )

    _assert_limit_action_result(
        response,
        project_id=project_id,
        action_kind="import.xlsx",
        row_count=2,
        max_rows=1,
    )
    _assert_no_import_residue(client, project_id)


@pytest.mark.parametrize("max_rows", [2, None])
def test_direct_xlsx_allows_exact_bound_and_unlimited_workloads(
    tmp_path: Path,
    max_rows: int | None,
) -> None:
    client = TestClient(
        create_app(
            tmp_path / "workspace",
            import_workload_limits=ImportWorkloadLimits(max_rows=max_rows),
        )
    )
    project_id = _project(client, f"Direct XLSX {max_rows!r} limit")

    response = client.post(
        f"/api/projects/{project_id}/import/xlsx",
        files={
            "file": (
                "at-limit.xlsx",
                _xlsx_bytes([["name"], ["Ada"], ["Grace"]]),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["rows"] == 2
    assert len(_visible_sheets(client, project_id)) == 1


def test_direct_files_obeys_injected_row_limit_before_blob_materialization(
    tmp_path: Path,
) -> None:
    client = TestClient(
        create_app(
            tmp_path / "workspace",
            import_workload_limits=ImportWorkloadLimits(max_rows=1),
        )
    )
    project_id = _project(client, "Direct files hosted limit")

    response = client.post(
        f"/api/projects/{project_id}/import/files",
        files=[
            ("files", ("one.txt", b"one", "text/plain")),
            ("files", ("two.txt", b"two", "text/plain")),
        ],
    )

    _assert_limit_action_result(
        response,
        project_id=project_id,
        action_kind="import.files",
        row_count=2,
        max_rows=1,
    )
    _assert_no_import_residue(client, project_id)


@pytest.mark.parametrize("max_rows", [2, None])
@pytest.mark.parametrize("kind", ["files", "pdf"])
def test_direct_media_allows_exact_bound_and_unlimited_workloads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    max_rows: int | None,
    kind: str,
) -> None:
    monkeypatch.setattr(
        AdmittedPdfPageRenderer, "render", lambda self, document, *, dpi: {}
    )
    client = TestClient(
        create_app(
            tmp_path / "workspace",
            import_workload_limits=ImportWorkloadLimits(max_rows=max_rows),
        )
    )
    project_id = _project(client, f"Direct {kind} {max_rows!r} limit")
    uploads = (
        [
            ("files", ("one.txt", b"one", "text/plain")),
            ("files", ("two.txt", b"two", "text/plain")),
        ]
        if kind == "files"
        else [("file", ("two.pdf", _two_page_pdf_bytes(), "application/pdf"))]
    )
    response = client.post(f"/api/projects/{project_id}/import/{kind}", files=uploads)
    assert response.status_code == 200, response.text
    assert response.json()["rows"] == 2
    project = client.app.state.workspace.get(project_id)
    assert len(project.sheets(include_hidden=True)) == 1
    assert project.row_count(response.json()["sheet_id"]) == 2
    assert project.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 1


def test_direct_pdf_obeys_injected_row_limit_before_blob_materialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        AdmittedPdfPageRenderer,
        "render",
        lambda self, document, *, dpi: {},
    )
    client = TestClient(
        create_app(
            tmp_path / "workspace",
            import_workload_limits=ImportWorkloadLimits(max_rows=1),
        )
    )
    project_id = _project(client, "Direct PDF hosted limit")

    response = client.post(
        f"/api/projects/{project_id}/import/pdf",
        files={"file": ("over-limit.pdf", _two_page_pdf_bytes(), "application/pdf")},
    )

    _assert_limit_action_result(
        response,
        project_id=project_id,
        action_kind="import.pdf",
        row_count=2,
        max_rows=1,
    )
    _assert_no_import_residue(client, project_id)


def test_bulk_xlsx_obeys_injected_row_limit_before_publication(
    tmp_path: Path,
) -> None:
    client = TestClient(
        create_app(
            tmp_path / "workspace",
            import_workload_limits=ImportWorkloadLimits(max_rows=1),
        )
    )
    project_id = _project(client, "Bulk XLSX hosted limit")
    plan = _bulk_plan(
        client,
        project_id,
        [
            (
                "over-limit.xlsx",
                _xlsx_bytes([["name"], ["Ada"], ["Grace"]]),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "mail/over-limit.xlsx",
            )
        ],
    )

    result = _execute_bulk(client, project_id, plan)

    output = plan["proposed_outputs"][0]
    assert result["created"] == []
    assert result["failed"] == [
        {
            "id": output["id"],
            "kind": "xlsx",
            "logical_paths": output["logical_paths"],
            "error": "import.xlsx row count exceeds the deployment limit of 1",
        }
    ]
    _assert_no_import_residue(client, project_id)


def test_bulk_pdf_files_obey_injected_row_limit_before_blob_materialization(
    tmp_path: Path,
) -> None:
    client = TestClient(
        create_app(
            tmp_path / "workspace",
            import_workload_limits=ImportWorkloadLimits(max_rows=1),
        )
    )
    project_id = _project(client, "Bulk PDF files hosted limit")
    document = _two_page_pdf_bytes()
    plan = _bulk_plan(
        client,
        project_id,
        [
            ("one.pdf", document, "application/pdf", "evidence/one.pdf"),
            ("two.pdf", document, "application/pdf", "evidence/two.pdf"),
        ],
    )

    result = _execute_bulk(client, project_id, plan)

    output = plan["proposed_outputs"][0]
    assert output["kind"] == "files_group"
    assert result["created"] == []
    assert result["failed"] == [
        {
            "id": output["id"],
            "kind": "files_group",
            "logical_paths": output["logical_paths"],
            "error": "import.files row count exceeds the deployment limit of 1",
        }
    ]
    _assert_no_import_residue(client, project_id)


def test_bulk_limit_refusal_preserves_a_valid_sibling_output(
    tmp_path: Path,
) -> None:
    client = TestClient(
        create_app(
            tmp_path / "workspace",
            import_workload_limits=ImportWorkloadLimits(max_rows=1),
        )
    )
    project_id = _project(client, "Bulk hosted sibling survival")
    plan = _bulk_plan(
        client,
        project_id,
        [
            (
                "over-limit.xlsx",
                _xlsx_bytes([["name"], ["Ada"], ["Grace"]]),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "mail/over-limit.xlsx",
            ),
            ("valid.txt", b"one row", "text/plain", "mail/valid.txt"),
        ],
    )

    result = _execute_bulk(client, project_id, plan)

    xlsx = next(
        output for output in plan["proposed_outputs"] if output["kind"] == "xlsx"
    )
    files = next(
        output for output in plan["proposed_outputs"] if output["kind"] == "files_group"
    )
    assert result["failed"] == [
        {
            "id": xlsx["id"],
            "kind": "xlsx",
            "logical_paths": xlsx["logical_paths"],
            "error": "import.xlsx row count exceeds the deployment limit of 1",
        }
    ]
    assert result["created"] == [
        {
            "id": files["id"],
            "kind": "sheet",
            "sheet_id": 1,
            "sheet_name": "files",
            "logical_paths": files["logical_paths"],
            "rows": 1,
        }
    ]
    assert result["first_sheet_id"] == 1
    assert [sheet["name"] for sheet in _visible_sheets(client, project_id)] == ["files"]
