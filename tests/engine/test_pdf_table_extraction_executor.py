from __future__ import annotations

from frisket.engine.store.media_blobs import owned_media_metadata_document

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from frisket.contracts.action import (
    ActionResult,
    Receipt,
)
from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.core import MapRows
from frisket.actions.pdf_tables import PdfTablesParams
from pydantic import ValidationError
from frisket.engine.executor.actions import run_action_spec
from frisket.ops.integrations import natural_pdf
from frisket.engine.jobs import RUN_PROJECT_KIND
from frisket.engine.store.media_blobs import media_cell
from frisket.ops.pdf_tables import _pdf_table_extract_options
from frisket.server.app import create_app
from frisket.engine.store import Project
from frisket.engine.store.runs import RunResultStore
from http_test_helpers import drain_queue
from tests.helpers import write_claimed_test_results


PDF_BYTES = b"%PDF-1.4\n% table extraction fixture\n"


FIXTURE_PDF = (
    Path(__file__).parent.parent / "fixtures" / "pdf_tables" / "simple_table.pdf"
)


def test_natural_pdf_adapter_extracts_one_flow_table_from_real_pdf() -> None:
    """End-to-end against the real natural-pdf package (skipped when absent).

    Proves the adapter wires to ``pdf.pages.to_flow().extract_table()`` and
    coerces the single ``TableResult`` into one ``PdfTable``. The fixture is a
    synthetic rendered table — a structural wiring smoke test, not a
    classification-quality benchmark (synthetic fixtures are fine here since
    quality isn't being measured). Unit coverage of the
    coercion branches lives in ``test_pdf_table_extraction_adapter.py``.
    """
    pytest.importorskip("natural_pdf")
    tables = natural_pdf.extract_pdf_tables(
        natural_pdf.PdfTableExtractRequest(
            path=FIXTURE_PDF,
            filename="simple_table.pdf",
            source_row_id=1,
            source_blob_hash="sha256:fixture",
            mode="extract_table",
            options={},
        )
    )

    assert len(tables) == 1
    assert tables[0].table_index == 0
    assert tables[0].header == ["vendor", "amount"]
    assert tables[0].rows == [["Acme", "10"], ["Beta", "12"], ["Gamma", "14"]]


def _seed_pdf_rows(project: Project) -> tuple[int, list[int], int, list[str]]:
    sheet_id = project.add_sheet("Documents")
    columns = {
        "title": project.add_column(sheet_id, "title", "text"),
        "pdf": project.add_column(sheet_id, "pdf", "file"),
    }
    hashes: list[str] = []
    records: list[dict[str, Any]] = []
    for filename in ("alpha.pdf", "bravo.pdf"):
        digest = project.add_blob(
            PDF_BYTES + filename.encode(),
            filename=filename,
            mime="application/pdf",
            metadata=owned_media_metadata_document(probe={"kind": "pdf", "pages": 5}),
        )
        hashes.append(digest)
        records.append(
            {
                "title": filename,
                "pdf": media_cell(
                    digest,
                    mime="application/pdf",
                    filename=filename,
                ),
            }
        )
    row_ids = project.add_rows(sheet_id, records, columns)
    return sheet_id, row_ids, columns["pdf"], hashes


def _seed_many_pdf_rows(project: Project, *, count: int) -> tuple[int, list[int], int]:
    """Seed many source rows cheaply while retaining real blob/PDF validation."""
    sheet_id = project.add_sheet("Many documents")
    columns = {
        "title": project.add_column(sheet_id, "title", "text"),
        "pdf": project.add_column(sheet_id, "pdf", "file"),
    }
    digest = project.add_blob(
        PDF_BYTES + b"shared.pdf",
        filename="shared.pdf",
        mime="application/pdf",
        metadata=owned_media_metadata_document(probe={"kind": "pdf", "pages": 1}),
    )
    row_ids = project.add_rows(
        sheet_id,
        [
            {
                "title": f"document-{index}",
                "pdf": media_cell(
                    digest,
                    mime="application/pdf",
                    filename=f"document-{index}.pdf",
                ),
            }
            for index in range(count)
        ],
        columns,
    )
    return sheet_id, row_ids, columns["pdf"]


def _extract_pdf_tables_action(
    sheet_id: int,
    *,
    row_ids: list[int] | None = None,
    key: str = "media_extract_pdf_tables@sha256:v1",
    mode: str = "extract_table",
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "source": "pdf",
        "mode": mode,
        "extract_table": {"pages": "all", "table_index": "all", "options": {}},
    }
    return {
        "action_id": "media.extract_pdf_tables",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id, "row_ids": row_ids},
        "output_names": {"pdf_tables": "pdf_tables"},
        "params": params,
        "idempotency_key": key,
    }


def _derive_pdf_rows_action(source_ref: dict[str, Any]) -> dict[str, Any]:
    return {
        "action_id": "derive.table_from_list",
        "scope": {"kind": "project"},
        "sheet_name": "Extracted tables",
        "params": {
            "source": {
                "kind": "named_result",
                "sheet_id": source_ref["sheet_id"],
                "column_id": source_ref["column_id"],
                "run_id": source_ref["run_id"],
                "route": source_ref["route"],
                "schema": source_ref["schema"],
            },
            "item_schema": source_ref["item_schema"],
            "columns": [
                {"name": "source_row_id", "path": "$.source_row_id", "type": "integer"},
                {
                    "name": "source_filename",
                    "path": "$.source_filename",
                    "type": "text",
                },
                {
                    "name": "source_blob_hash",
                    "path": "$.source_blob_hash",
                    "type": "text",
                },
                {"name": "page_start", "path": "$.page_start", "type": "integer"},
                {"name": "page_end", "path": "$.page_end", "type": "integer"},
                {"name": "table_index", "path": "$.table_index", "type": "integer"},
                {
                    "name": "table_row_index",
                    "path": "$.table_row_index",
                    "type": "integer",
                },
                {"name": "raw_cells_json", "path": "$.raw_cells_json", "type": "json"},
                {"name": "vendor", "path": "$.vendor", "type": "text"},
                {"name": "amount", "path": "$.amount", "type": "text"},
            ],
        },
        "idempotency_key": "derive_pdf_rows@sha256:v1",
    }


def _install_matching_adapter(
    monkeypatch: pytest.MonkeyPatch, calls: list[dict[str, Any]]
) -> None:
    def fake_extract(
        request: natural_pdf.PdfTableExtractRequest,
    ) -> list[natural_pdf.PdfTable]:
        calls.append(
            {
                "path": str(request.path),
                "filename": request.filename,
                "row_id": request.source_row_id,
                "blob_hash": request.source_blob_hash,
                "mode": request.mode,
            }
        )
        if request.filename == "alpha.pdf":
            return [
                natural_pdf.PdfTable(
                    page_start=1,
                    page_end=2,
                    table_index=0,
                    header=["vendor", "amount"],
                    rows=[["Acme", "10"], ["Beta", "12"]],
                    raw_cells=[["Acme", "10"], ["Beta", "12"]],
                )
            ]
        return [
            natural_pdf.PdfTable(
                page_start=3,
                page_end=3,
                table_index=0,
                header=["vendor", "amount"],
                rows=[["Civic", "7"]],
                raw_cells=[["Civic", "7"]],
            )
        ]

    monkeypatch.setattr(natural_pdf, "extract_pdf_tables", fake_extract)


def _create_pdf_project(client: TestClient) -> tuple[str, Project, int, list[int], int]:
    project_id = client.post("/api/projects", json={"name": "PDF Tables"}).json()["id"]
    project = client.app.state.workspace.get(project_id)
    sheet_id, row_ids, pdf_col, _hashes = _seed_pdf_rows(project)
    return project_id, project, sheet_id, row_ids, pdf_col


def _mutate_pdf_table_output_for_replay(
    project: Project, receipt_id: str | None, *, legacy: bool
) -> None:
    assert receipt_id is not None
    receipt_row = project.db.execute(
        "SELECT body FROM receipts WHERE id=?", (receipt_id,)
    ).fetchone()
    assert receipt_row is not None
    body = json.loads(receipt_row["body"])
    ref = next(
        item["ref"]
        for item in body["outputs"]
        if item["ref"]["kind"] == "map_result_column"
    )
    assert ref["value_hash"].startswith("sha256:")
    if legacy:
        ref.pop("value_hash")
        project.db.execute(
            "UPDATE receipts SET body=? WHERE id=?",
            (json.dumps(body), receipt_id),
        )
    if not legacy:
        store = RunResultStore(project)
        op_id = project.append_op("map")
        run_id = store.start_run(op_id, ref["sheet_id"], "media.extract_pdf_tables")
        write_claimed_test_results(
            project,
            run_id,
            [
                {
                    "row_id": ref["row_ids"][0],
                    "column_id": ref["column_id"],
                    "value": [{"structurally_drifted": True}],
                }
            ],
        )
        store.finish_run(run_id)
        store.point_column_at_run(op_id, ref["column_id"], run_id)
    project.db.commit()


def test_pdf_table_replay_rejects_stored_value_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []
    _install_matching_adapter(monkeypatch, calls)
    client = TestClient(create_app(tmp_path / "ws", run_status_grace_seconds=3600.0))
    project_id, project, sheet_id, row_ids, _pdf_col = _create_pdf_project(client)
    action = _extract_pdf_tables_action(
        sheet_id, row_ids=row_ids, key="media_extract_pdf_tables@sha256:drift"
    )
    queued_response = client.post(
        f"/api/projects/{project_id}/actions/v1/run", json=action
    )
    assert queued_response.status_code == 200, queued_response.text
    queued = ActionResult.model_validate(queued_response.json())
    assert queued.status == "queued"
    drain_queue(client)
    _mutate_pdf_table_output_for_replay(project, queued.receipt_id, legacy=False)

    replay_response = client.post(
        f"/api/projects/{project_id}/actions/v1/run", json=action
    )
    assert replay_response.status_code == 409, replay_response.text
    replay = ActionResult.model_validate(replay_response.json())
    assert replay.status == "failed"
    assert replay.errors[0].code == "stale_replay"
    assert len(calls) == 2


def test_pdf_table_optional_value_hash_omission_retains_unchanged_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []
    _install_matching_adapter(monkeypatch, calls)
    client = TestClient(create_app(tmp_path / "ws", run_status_grace_seconds=3600.0))
    project_id, project, sheet_id, row_ids, _pdf_col = _create_pdf_project(client)
    action = _extract_pdf_tables_action(
        sheet_id, row_ids=row_ids, key="media_extract_pdf_tables@sha256:legacy"
    )
    queued_response = client.post(
        f"/api/projects/{project_id}/actions/v1/run", json=action
    )
    assert queued_response.status_code == 200, queued_response.text
    queued = ActionResult.model_validate(queued_response.json())
    assert queued.status == "queued"
    drain_queue(client)
    _mutate_pdf_table_output_for_replay(project, queued.receipt_id, legacy=True)

    replay_response = client.post(
        f"/api/projects/{project_id}/actions/v1/run", json=action
    )
    assert replay_response.status_code == 200, replay_response.text
    replay = ActionResult.model_validate(replay_response.json())
    assert replay.status == "completed", replay.errors
    assert replay.receipt_id == queued.receipt_id
    assert len(calls) == 2


def test_pdf_table_replay_preserves_identity_rejections_and_named_result_tolerance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from frisket.actions.system import BoundTypedActionRequest
    from frisket.actions.types import ActionRequest
    from frisket.engine.executor.map_rows_action import _typed_replay_error

    expected_messages = {
        "invalid_ref": "media.extract_pdf_tables replay receipt has invalid output refs",
        "missing": "media.extract_pdf_tables replay output column is missing",
        "renamed": "media.extract_pdf_tables replay output column was renamed",
        "type_changed": "media.extract_pdf_tables replay output column type changed",
        "run_changed": "media.extract_pdf_tables replay output column changed runs",
        "no_output": "media.extract_pdf_tables replay receipt has no output column ref",
    }
    calls: list[dict[str, Any]] = []
    _install_matching_adapter(monkeypatch, calls)
    client = TestClient(create_app(tmp_path / "ws", run_status_grace_seconds=3600.0))
    project_id, project, sheet_id, row_ids, _pdf_col = _create_pdf_project(client)
    action = _extract_pdf_tables_action(
        sheet_id, row_ids=row_ids, key="media_extract_pdf_tables@sha256:identity"
    )
    typed_action = BoundTypedActionRequest.bind(
        ACTION_REGISTRY.get(action["action_id"]), ActionRequest.model_validate(action)
    )
    queued_response = client.post(
        f"/api/projects/{project_id}/actions/v1/run", json=action
    )
    assert queued_response.status_code == 200, queued_response.text
    queued = ActionResult.model_validate(queued_response.json())
    assert queued.status == "queued"
    drain_queue(client)
    receipt_row = project.db.execute(
        "SELECT body FROM receipts WHERE id=?", (queued.receipt_id,)
    ).fetchone()
    base = json.loads(receipt_row["body"])
    replay_error = _typed_replay_error(project, typed_action)
    assert replay_error(Receipt.model_validate(base)) is None

    for defect, expected_message in expected_messages.items():
        body = json.loads(json.dumps(base))
        if defect == "no_output":
            body["outputs"] = [
                item
                for item in body["outputs"]
                if item["ref"]["kind"] not in {"map_result_column", "named_result"}
            ]
        else:
            ref = next(
                item["ref"]
                for item in body["outputs"]
                if item["ref"]["kind"] == "map_result_column"
            )
            if defect == "invalid_ref":
                ref["column_id"] = "not-an-integer"
            elif defect == "missing":
                ref["column_id"] = 2_147_483_647
            elif defect == "renamed":
                ref["name"] = "renamed_pdf_tables"
            elif defect == "type_changed":
                ref["type"] = "text"
            else:
                assert defect == "run_changed"
                ref["run_id"] += 1

        error = replay_error(Receipt.model_validate(body))
        assert error is not None, defect
        assert error.code == "stale_replay", defect
        assert error.message == expected_message, defect


def test_pdf_table_action_catalog_validation_and_snippet_gate(tmp_path) -> None:
    entry = ACTION_REGISTRY.get("media.extract_pdf_tables").catalog_entry()
    assert isinstance(
        ACTION_REGISTRY.get("media.extract_pdf_tables").definition.run, MapRows
    )
    assert entry["cost_policy"]["kind"] == "none"
    assert {"read_pdf_blob_cells", "call_natural_pdf_adapter"} <= set(
        entry["side_effects"]
    )
    assert PdfTablesParams.model_validate({"source": "pdf"}).mode == "extract_table"
    with pytest.raises(ValidationError):
        PdfTablesParams.model_validate({"source": "pdf", "mode": "natural_pdf_snippet"})
    project = Project.create(tmp_path / "catalog.frisket")
    try:
        sheet_id, _, _, _ = _seed_pdf_rows(project)
        missing = _extract_pdf_tables_action(sheet_id)
        missing.pop("idempotency_key")
        result = run_action_spec(project, missing, project_id="p")
        assert result.status == "failed"
        assert result.errors[0].code == "invalid_action_request"
        assert "idempotency_key" in result.errors[0].message
        old = {**_extract_pdf_tables_action(sheet_id), "action_id": "derive.pdf_tables"}
        result = run_action_spec(project, old, project_id="p")
        assert result.status == "failed"
        assert result.errors[0].code == "invalid_action_request"
    finally:
        project.close()


def test_pdf_table_queue_inventory_is_owned_by_typed_map_rows() -> None:
    from frisket.engine.executor.action_specs import declared_queued_project_run_kinds

    assert "media.extract_pdf_tables" in declared_queued_project_run_kinds()
    assert isinstance(
        ACTION_REGISTRY.get("media.extract_pdf_tables").definition.run, MapRows
    )


@pytest.mark.parametrize("value", ["require_matching", "anything_goes"])
def test_shape_policy_is_rejected(value) -> None:
    with pytest.raises(ValidationError):
        PdfTablesParams.model_validate({"source": "pdf", "shape_policy": value})


def test_table_mode_accepts_natural_pdf_vocabulary_and_rejects_unknown() -> None:
    for mode in ("auto", "stream", "lattice"):
        assert (
            PdfTablesParams.model_validate(
                {"source": "pdf", "table_mode": mode}
            ).table_mode
            == mode
        )
    with pytest.raises(ValidationError) as error:
        PdfTablesParams.model_validate({"source": "pdf", "table_mode": "bordered"})
    assert error.value.errors()[0]["loc"] == ("table_mode",)


def test_pdf_table_extraction_direct_typed_handler_has_no_empty_feed(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[natural_pdf.PdfTableExtractRequest] = []

    def fake_extract(
        request: natural_pdf.PdfTableExtractRequest,
    ) -> list[natural_pdf.PdfTable]:
        calls.append(request)
        return []

    monkeypatch.setattr(natural_pdf, "extract_pdf_tables", fake_extract)
    project = Project.create(tmp_path / "direct.frisket", name="Direct")
    try:
        sheet_id, row_ids, _pdf_col, _hashes = _seed_pdf_rows(project)
        result = run_action_spec(
            project,
            _extract_pdf_tables_action(
                sheet_id,
                row_ids=[row_ids[0]],
                key="media_extract_pdf_tables_direct@sha256:v1",
            ),
            project_id="direct-project",
        )

        assert result.status == "failed"
        assert result.errors[0].code == "pdf_table_extract_failed"
        assert len(calls) == 1
        assert all(output.kind != "named_result" for output in result.outputs)
    finally:
        project.close()


def test_pdf_table_extraction_processes_501_explicit_source_rows(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[natural_pdf.PdfTableExtractRequest] = []

    def fake_extract(
        request: natural_pdf.PdfTableExtractRequest,
    ) -> list[natural_pdf.PdfTable]:
        calls.append(request)
        return [
            natural_pdf.PdfTable(
                page_start=1,
                page_end=1,
                table_index=0,
                header=["document"],
                rows=[[request.filename]],
                raw_cells=[[request.filename]],
            )
        ]

    monkeypatch.setattr(natural_pdf, "extract_pdf_tables", fake_extract)
    client = TestClient(create_app(tmp_path / "ws", run_status_grace_seconds=3600.0))
    project_id = client.post("/api/projects", json={"name": "Many PDF tables"}).json()[
        "id"
    ]
    project = client.app.state.workspace.get(project_id)
    sheet_id, row_ids, _pdf_col = _seed_many_pdf_rows(project, count=501)

    response = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json=_extract_pdf_tables_action(
            sheet_id,
            row_ids=row_ids,
            key="media_extract_pdf_tables_501_sources@sha256:v1",
        ),
    )
    assert response.status_code == 200, response.text
    queued = ActionResult.model_validate(response.json())
    assert queued.status == "queued"
    assert queued.job_id is not None
    job = client.app.state.workspace.queue.get(queued.job_id)
    assert job is not None
    assert job.payload["v1_row_source_snapshot"]["row_ids"] == row_ids

    drain_queue(client)

    assert sorted(request.source_row_id for request in calls) == sorted(row_ids)
    receipt_row = project.db.execute(
        "SELECT status FROM receipts WHERE id=?", (queued.receipt_id,)
    ).fetchone()
    assert receipt_row is not None
    assert receipt_row["status"] == "completed"


def test_pdf_table_extraction_publishes_10001_records_with_compact_receipt(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    records = [[f"vendor-{index}"] for index in range(10_001)]

    def fake_extract(
        request: natural_pdf.PdfTableExtractRequest,
    ) -> list[natural_pdf.PdfTable]:
        del request
        return [
            natural_pdf.PdfTable(
                page_start=1,
                page_end=1,
                table_index=0,
                header=["vendor"],
                rows=records,
                raw_cells=records,
            )
        ]

    monkeypatch.setattr(natural_pdf, "extract_pdf_tables", fake_extract)
    client = TestClient(create_app(tmp_path / "ws", run_status_grace_seconds=3600.0))
    project_id, project, sheet_id, row_ids, _pdf_col = _create_pdf_project(client)
    source_row_id = row_ids[0]

    response = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json=_extract_pdf_tables_action(
            sheet_id,
            row_ids=[source_row_id],
            key="media_extract_pdf_tables_10001_records@sha256:v1",
        ),
    )
    assert response.status_code == 200, response.text
    queued = ActionResult.model_validate(response.json())
    assert queued.status == "queued"

    drain_queue(client)

    receipt_row = project.db.execute(
        "SELECT status, body FROM receipts WHERE id=?", (queued.receipt_id,)
    ).fetchone()
    assert receipt_row is not None
    assert receipt_row["status"] == "completed"
    body = str(receipt_row["body"])
    assert len(body.encode("utf-8")) < 100_000
    assert "vendor-10000" not in body
    receipt = Receipt.model_validate(json.loads(body))
    column_ref = next(
        item.ref for item in receipt.outputs if item.ref["kind"] == "map_result_column"
    )
    value = project.get_values(
        sheet_id, int(column_ref["column_id"]), row_ids=[source_row_id]
    )[source_row_id]
    assert len(value) == 10_001
    assert value[0]["vendor"] == "vendor-0"
    assert value[-1]["vendor"] == "vendor-10000"
    assert all(
        len(item.ref.get("row_ids", [])) <= 1
        for item in [*receipt.inputs, *receipt.outputs, *receipt.evidence]
    )


def test_pdf_table_extraction_queues_then_feeds_table_from_list(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []
    _install_matching_adapter(monkeypatch, calls)
    client = TestClient(create_app(tmp_path / "ws", run_status_grace_seconds=3600.0))
    project_id, project, sheet_id, row_ids, _pdf_col = _create_pdf_project(client)

    response = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json=_extract_pdf_tables_action(sheet_id, row_ids=row_ids),
    )
    assert response.status_code == 200, response.text
    queued = ActionResult.model_validate(response.json())
    assert queued.status == "queued"
    assert queued.action.kind == "media.extract_pdf_tables"
    assert queued.run_id is not None
    assert queued.job_id is not None
    assert queued.receipt_id is not None
    assert calls == []

    job = client.app.state.workspace.queue.get(queued.job_id)
    assert job is not None
    assert job.kind == RUN_PROJECT_KIND
    assert job.payload["action_kind"] == "media.extract_pdf_tables"
    assert job.payload["spec"]["action_kind"] == "media.extract_pdf_tables"
    assert "recipe" not in job.payload["spec"]
    assert job.payload["spec"]["output_names"] == {"pdf_tables": "pdf_tables"}
    assert job.payload["v1_row_source_snapshot"]["row_ids"] == row_ids

    drain_queue(client)
    assert sorted(call["filename"] for call in calls) == ["alpha.pdf", "bravo.pdf"]

    receipt_row = project.db.execute(
        "SELECT run_id, action_kind, status, body FROM receipts WHERE id=?",
        (queued.receipt_id,),
    ).fetchone()
    assert receipt_row is not None
    assert receipt_row["run_id"] == queued.run_id
    assert receipt_row["action_kind"] == "media.extract_pdf_tables"
    assert receipt_row["status"] == "completed"
    receipt = Receipt.model_validate(json.loads(receipt_row["body"]))
    refs = [item.ref for item in receipt.outputs]
    column_ref = next(ref for ref in refs if ref["kind"] == "map_result_column")
    assert column_ref["value_hash"].startswith("sha256:")
    named_ref = next(ref for ref in refs if ref["kind"] == "named_result")
    assert named_ref["may_feed"] == ["derive.table_from_list"]
    assert named_ref["source_action_kind"] == "media.extract_pdf_tables"
    assert named_ref["route"] == "pdf_tables"
    assert named_ref["schema"] == "pdf_table_rows"

    values = project.get_values(sheet_id, int(column_ref["column_id"]), row_ids=row_ids)
    assert values[row_ids[0]][0]["vendor"] == "Acme"
    assert values[row_ids[0]][1]["amount"] == "12"
    assert values[row_ids[1]][0]["source_filename"] == "bravo.pdf"

    derived = run_action_spec(
        project,
        _derive_pdf_rows_action(named_ref),
        project_id=project_id,
    )
    assert derived.status == "completed"
    child_sheet_id = next(
        output.sheet_id for output in derived.outputs if output.kind == "sheet"
    )
    assert child_sheet_id is not None
    child_columns = {
        row["name"]: row
        for row in project.db.execute(
            "SELECT * FROM columns WHERE sheet_id=? ORDER BY position",
            (child_sheet_id,),
        ).fetchall()
    }
    assert list(child_columns) == [
        "source_row_id",
        "source_filename",
        "source_blob_hash",
        "page_start",
        "page_end",
        "table_index",
        "table_row_index",
        "raw_cells_json",
        "vendor",
        "amount",
    ]
    child_rows = project.db.execute(
        "SELECT id, parent_row_id FROM rows WHERE sheet_id=? ORDER BY position",
        (child_sheet_id,),
    ).fetchall()
    assert [int(row["parent_row_id"]) for row in child_rows] == [
        row_ids[0],
        row_ids[0],
        row_ids[1],
    ]
    assert list(
        project.get_values(child_sheet_id, int(child_columns["vendor"]["id"])).values()
    ) == ["Acme", "Beta", "Civic"]


def test_pdf_table_blank_cells_preserve_shape_for_named_result(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def sparse_cells(
        request: natural_pdf.PdfTableExtractRequest,
    ) -> list[natural_pdf.PdfTable]:
        return [
            natural_pdf.PdfTable(
                page_start=1,
                page_end=1,
                table_index=0,
                header=["vendor", "amount"],
                rows=[["Acme", None]]
                if request.filename == "alpha.pdf"
                else [["Beta", "12"]],
                raw_cells=[["Acme", None]]
                if request.filename == "alpha.pdf"
                else [["Beta", "12"]],
            )
        ]

    monkeypatch.setattr(natural_pdf, "extract_pdf_tables", sparse_cells)
    client = TestClient(create_app(tmp_path / "ws", run_status_grace_seconds=3600.0))
    project_id, project, sheet_id, row_ids, _pdf_col = _create_pdf_project(client)

    response = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json=_extract_pdf_tables_action(
            sheet_id,
            row_ids=row_ids,
            key="media_extract_pdf_tables_null_cells@sha256:v1",
        ),
    )
    assert response.status_code == 200, response.text
    queued = ActionResult.model_validate(response.json())
    assert queued.status == "queued"

    drain_queue(client)

    receipt_row = project.db.execute(
        "SELECT status, body FROM receipts WHERE id=?",
        (queued.receipt_id,),
    ).fetchone()
    assert receipt_row is not None
    assert receipt_row["status"] == "completed"
    receipt = Receipt.model_validate(json.loads(receipt_row["body"]))
    named_ref = next(
        item.ref for item in receipt.outputs if item.ref["kind"] == "named_result"
    )
    assert named_ref["item_schema"]["properties"]["amount"]["type"] == [
        "string",
        "null",
    ]
    column_ref = next(
        item.ref for item in receipt.outputs if item.ref["kind"] == "map_result_column"
    )
    values = project.get_values(sheet_id, int(column_ref["column_id"]), row_ids=row_ids)
    assert "amount" in values[row_ids[0]][0]
    assert values[row_ids[0]][0]["amount"] is None
    assert values[row_ids[1]][0]["amount"] == "12"


def test_pdf_table_shape_mismatch_fails_extractor_without_named_result(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def mismatched(
        request: natural_pdf.PdfTableExtractRequest,
    ) -> list[natural_pdf.PdfTable]:
        header = (
            ["vendor", "amount"]
            if request.filename == "alpha.pdf"
            else ["vendor", "total", "note"]
        )
        return [
            natural_pdf.PdfTable(
                page_start=1,
                page_end=1,
                table_index=0,
                header=header,
                rows=[["Acme", "10"] if len(header) == 2 else ["Civic", "7", "late"]],
                raw_cells=[
                    ["Acme", "10"] if len(header) == 2 else ["Civic", "7", "late"]
                ],
            )
        ]

    monkeypatch.setattr(natural_pdf, "extract_pdf_tables", mismatched)
    client = TestClient(create_app(tmp_path / "ws", run_status_grace_seconds=3600.0))
    project_id, project, sheet_id, row_ids, _pdf_col = _create_pdf_project(client)

    response = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json=_extract_pdf_tables_action(
            sheet_id,
            row_ids=row_ids,
            key="media_extract_pdf_tables_mismatch@sha256:v1",
        ),
    )
    assert response.status_code == 200, response.text
    queued = ActionResult.model_validate(response.json())
    assert queued.status == "queued"

    drain_queue(client)

    receipt_row = project.db.execute(
        "SELECT status, body FROM receipts WHERE id=?",
        (queued.receipt_id,),
    ).fetchone()
    assert receipt_row is not None
    assert receipt_row["status"] == "failed"
    receipt = Receipt.model_validate(json.loads(receipt_row["body"]))
    assert receipt.errors[0].code == "pdf_table_shape_mismatch"
    assert all(item.ref["kind"] != "named_result" for item in receipt.outputs)
    assert (
        project.db.execute(
            "SELECT COUNT(*) FROM sheets WHERE parent_sheet_id=?", (sheet_id,)
        ).fetchone()[0]
        == 0
    )


# ---------------------------------------------------------------------------
# A10: table_mode -> natural_pdf extract_table(method=...) kwargs (pure unit,
# no project/blob machinery — mirrors natural_pdf.py's own kwargs contract
# tested against a fake adapter in test_pdf_table_extraction_adapter.py).


def test_table_mode_auto_omits_method_kwarg() -> None:
    assert _pdf_table_extract_options({}) == {}
    assert _pdf_table_extract_options({"table_mode": "auto"}) == {}


def test_table_mode_stream_and_lattice_pass_through_as_method() -> None:
    assert _pdf_table_extract_options({"table_mode": "stream"}) == {
        "options": {"method": "stream"}
    }
    assert _pdf_table_extract_options({"table_mode": "lattice"}) == {
        "options": {"method": "lattice"}
    }


def test_table_mode_does_not_override_an_explicit_method_in_extract_table_options() -> (
    None
):
    # The free-form extract_table.options escape hatch wins over the
    # friendly table_mode control when the caller already set method there.
    spec = {
        "table_mode": "lattice",
        "extract_table": {"options": {"method": "text"}},
    }
    assert _pdf_table_extract_options(spec) == {"options": {"method": "text"}}


def test_table_mode_merges_alongside_other_extract_table_options() -> None:
    spec = {
        "table_mode": "stream",
        "extract_table": {"pages": "all", "options": {"cell_extract": "words"}},
    }
    assert _pdf_table_extract_options(spec) == {
        "pages": "all",
        "options": {"cell_extract": "words", "method": "stream"},
    }
