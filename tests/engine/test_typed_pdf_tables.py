from contextlib import closing
from copy import deepcopy
from typing import Any
import asyncio
from threading import Event
from contextlib import contextmanager

import pytest
from pydantic import BaseModel

from frisket.actions.core import ActionCategory, RegisteredAction, action, map_rows
from frisket.actions.pdf_table_types import PdfTableRows, PdfTablesReader
from frisket.actions.types import ActionParams, ColumnRef, Row, RowResult
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.types import ActionRequest
from frisket.engine.executor.actions import _default_map_runner_factory
from frisket.engine.executor.map_rows_action import run_typed_map_rows_action
from frisket.engine.jobs.ports import JobHandlerContext
from fastapi.testclient import TestClient
from frisket.server.app import create_app
from http_test_helpers import drain_queue
from tests.engine.test_pdf_table_extraction_executor import (
    _seed_pdf_rows,
    _extract_pdf_tables_action,
    _install_matching_adapter,
    _derive_pdf_rows_action,
)

from frisket.actions.registry import ACTION_REGISTRY
from frisket.engine.executor import run_action_spec
from frisket.engine.store import Project
from frisket.engine.store.media_blobs import media_cell, owned_media_metadata_document
from frisket.engine.store.receipts import ReceiptStore
from frisket.ops.integrations import natural_pdf


def test_typed_pdf_read_publishes_host_schema_and_replays(tmp_path, monkeypatch):
    calls = []

    def extract(request):
        calls.append(request)
        return [
            natural_pdf.PdfTable(
                page_start=1,
                page_end=2,
                table_index=0,
                header=["vendor", "amount"],
                rows=[["Acme", None]],
                raw_cells=[["Acme", None]],
            )
        ]

    monkeypatch.setattr(natural_pdf, "extract_pdf_tables", extract)
    entry = ACTION_REGISTRY.get("media.extract_pdf_tables").catalog_entry()
    source_schema = entry["input_schema"]["properties"]["source"]
    if "$ref" in source_schema:
        source_schema = entry["input_schema"]["$defs"][
            source_schema["$ref"].split("/")[-1]
        ]
    assert source_schema["type"] == "string"
    with closing(Project.create(tmp_path / "pdf.frisket")) as project:
        sheet = project.add_sheet("Documents")
        column = project.add_column(sheet, "pdf", "file")
        digest = project.add_blob(
            b"%PDF-1.4\nfixture\n",
            filename="document.pdf",
            mime="application/pdf",
            metadata=owned_media_metadata_document(probe={"kind": "pdf", "pages": 2}),
        )
        rows = project.add_rows(
            sheet,
            [
                {
                    "pdf": media_cell(
                        digest, mime="application/pdf", filename="document.pdf"
                    )
                }
            ],
            {"pdf": column},
        )
        request = {
            "action_id": "media.extract_pdf_tables",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet},
            "params": {"source": "pdf"},
            "idempotency_key": "pdf",
        }
        result = run_action_spec(project, request, project_id="p")
        assert result.status == "completed", result.model_dump_json()
        receipt = ReceiptStore(project).parsed_by_id(result.receipt_id)
        named = next(
            item.ref
            for item in receipt.outputs
            if item.ref.get("kind") == "named_result"
        )
        assert named["row_ids"] == rows
        assert {"vendor", "amount"} <= named["item_schema"]["properties"].keys()
        assert len(calls) == 1
        read = next(
            item.ref
            for item in receipt.evidence
            if item.ref.get("kind") == "pdf_table_read"
        )
        assert read["columns"] == ["vendor", "amount"]
        replay = run_action_spec(project, request, project_id="p")
        assert replay.receipt_id == result.receipt_id
        assert len(calls) == 1


@pytest.fixture
def pdf_source(tmp_path, monkeypatch):
    calls = []
    _install_matching_adapter(monkeypatch, calls)
    with closing(Project.create(tmp_path / "source.frisket")) as project:
        sheet, rows, column, hashes = _seed_pdf_rows(project)
        yield project, sheet, rows, column, hashes, calls


class AlternateParams(ActionParams):
    document: ColumnRef[Any]
    attachment: ColumnRef[Any]
    mode: str = "valid"


class AlternateOutput(BaseModel):
    extracted: PdfTableRows
    ordinary: list[dict]


escaped_readers = []


async def alternate(
    params: AlternateParams, row: Row, reader: PdfTablesReader
) -> RowResult[AlternateOutput]:
    if params.mode == "retain":
        escaped_readers.append((reader, row, params.document))
    value = await reader.read(
        row, params.document, table_mode="stream", extract_table={"pages": [1]}
    )
    if params.mode == "copy":
        value = value.model_copy(deep=True)
    elif params.mode == "mutate":
        value.root[0]["source_blob_hash"] = "forged"
    elif params.mode == "wrong_source":
        value = await reader.read(row, ColumnRef("unselected"))
    elif params.mode == "bad_options":
        value = await reader.read(row, params.document, extract_table=False)
    return RowResult(
        output=AlternateOutput(extracted=value, ordinary=deepcopy(value.root))
    )


@pytest.mark.parametrize(
    "mode", ["valid", "retain", "copy", "mutate", "wrong_source", "bad_options"]
)
def test_alternate_params_actual_source_options_and_exact_output_association(
    pdf_source, monkeypatch, mode
):
    project, sheet, rows, _, _, calls = pdf_source
    attachment = project.add_column(sheet, "jpeg", "file")
    digest = project.add_blob(
        b"jpeg fixture", filename="unrelated.jpg", mime="image/jpeg"
    )
    # The action is allowed to select a non-PDF input without reading it as PDF.
    project.add_rows(
        sheet,
        [
            {
                "pdf": project.get_values(sheet, project.columns(sheet)[1]["id"])[
                    rows[0]
                ],
                "jpeg": media_cell(digest, mime="image/jpeg"),
            }
        ],
        {"pdf": project.columns(sheet)[1]["id"], "jpeg": attachment},
    )
    selected = project.visible_row_ids(sheet)[-1:]
    registered = RegisteredAction(
        "example.pdf",
        action(
            name="pdf",
            title="PDF",
            description="Actual derived PDF inputs",
            category=ActionCategory.EXTRACT,
            run=map_rows(alternate),
        ),
    )
    monkeypatch.setattr(
        ACTION_REGISTRY,
        "_actions",
        {**ACTION_REGISTRY._actions, registered.action_id: registered},
    )
    bound = BoundTypedActionRequest.bind(
        registered,
        ActionRequest(
            action_id=registered.action_id,
            scope={"kind": "sheet_rows", "sheet_id": sheet, "row_ids": selected},
            params={"document": "pdf", "attachment": "jpeg", "mode": mode},
            idempotency_key="custom",
        ),
    )
    result = run_typed_map_rows_action(
        project, "p", bound, None, _default_map_runner_factory
    )
    receipt = ReceiptStore(project).parsed_by_id(result.receipt_id)
    named = [
        item.ref for item in receipt.outputs if item.ref.get("kind") == "named_result"
    ]
    if mode not in {"valid", "retain"}:
        assert result.status == "failed", result.errors
        assert named == []
        assert not any(
            item.ref.get("kind") == "pdf_table_read" for item in receipt.evidence
        )
        return
    assert result.status == "completed", result.errors
    assert len(named) == 1 and named[0]["route"] == "extracted"
    read = next(
        item.ref
        for item in receipt.evidence
        if item.ref.get("kind") == "pdf_table_read"
    )
    assert read["options"] == {
        "mode": "extract_table",
        "table_mode": "stream",
        "extract_table": {"pages": [1]},
    }
    assert read["column_id"] == named[0]["column_id"]
    assert len(calls) == 1
    derived = run_action_spec(
        project, _derive_pdf_rows_action(named[0]), project_id="p"
    )
    assert derived.status == "completed", derived.errors
    if mode == "retain":
        reader, row, source = escaped_readers.pop()
        with pytest.raises(RuntimeError, match="closed"):
            asyncio.run(reader.read(row, source))
        assert len(calls) == 1


@pytest.mark.parametrize(
    "change", ["filename", "mime", "size", "source_url", "missing_blob", "roster"]
)
def test_queue_pins_source_roster_and_blob_descriptors(tmp_path, monkeypatch, change):
    calls = []
    _install_matching_adapter(monkeypatch, calls)
    with TestClient(
        create_app(tmp_path / "ws", run_status_grace_seconds=3600)
    ) as client:
        project_id = client.post("/api/projects", json={"name": "PDF"}).json()["id"]
        project = client.app.state.workspace.get(project_id)
        sheet, rows, column, hashes = _seed_pdf_rows(project)
        body = _extract_pdf_tables_action(sheet)
        response = client.post(f"/api/projects/{project_id}/actions/v1/run", json=body)
        assert response.status_code == 200, response.text
        receipt_id = response.json()["receipt_id"]
        if change == "missing_blob":
            project.db.execute("DELETE FROM blobs WHERE hash=?", (hashes[0],))
        elif change == "roster":
            project.add_rows(
                sheet,
                [{"pdf": project.get_values(sheet, column)[rows[0]]}],
                {"pdf": column},
            )
        else:
            # Whitelisted test parameter, never caller SQL.
            project.db.execute(
                f"UPDATE blobs SET {change}=? WHERE hash=?",
                (123 if change == "size" else "changed", hashes[0]),
            )
        project.db.commit()
        drain_queue(client)
        receipt = ReceiptStore(project).parsed_by_id(receipt_id)
        assert receipt.status == "failed"
        assert receipt.errors[0].code == "stale_media_extract_pdf_tables_input", (
            receipt.errors
        )
        assert calls == []


@pytest.mark.parametrize("crash", [False, True])
def test_pdf_worker_recovery_uses_committed_read_evidence(tmp_path, monkeypatch, crash):
    from frisket.engine.jobs import runs

    calls = []
    _install_matching_adapter(monkeypatch, calls)
    with TestClient(
        create_app(tmp_path / "ws", run_status_grace_seconds=3600)
    ) as client:
        project_id = client.post("/api/projects", json={"name": "PDF"}).json()["id"]
        project = client.app.state.workspace.get(project_id)
        sheet, rows, _, _ = _seed_pdf_rows(project)
        response = client.post(
            f"/api/projects/{project_id}/actions/v1/run",
            json=_extract_pdf_tables_action(sheet, row_ids=rows),
        )
        assert response.status_code == 200, response.text
        queued = response.json()
        workspace = client.app.state.workspace
        job = workspace.queue.get(queued["job_id"])
        payload = {**deepcopy(job.payload), "job_id": job.id}
        handler = workspace.registry.get("project.run")
        context = JobHandlerContext.from_claimed_job(trusted_org_id=None)
        original = runs.queued_v1_finalize_action_result
        if crash:

            class ProcessDeath(BaseException):
                pass

            def die(*args, **kwargs):
                raise ProcessDeath()

            monkeypatch.setattr(runs, "queued_v1_finalize_action_result", die)
            with pytest.raises(ProcessDeath):
                handler(deepcopy(payload), context)
            monkeypatch.setattr(runs, "queued_v1_finalize_action_result", original)
        else:
            drain_queue(client)
        before = ReceiptStore(project).parsed_by_id(queued["receipt_id"])
        read_facts = [
            item.ref
            for item in before.evidence
            if item.ref.get("kind") == "pdf_table_read"
        ]
        assert len(read_facts) == 2

        def forbidden(*args, **kwargs):
            raise AssertionError("Recovery must not re-extract")

        monkeypatch.setattr(natural_pdf, "extract_pdf_tables", forbidden)
        assert handler(deepcopy(payload), context)["skipped"] is True
        after = ReceiptStore(project).parsed_by_id(queued["receipt_id"])
        assert after.status == "completed"
        assert sorted(
            [
                item.ref
                for item in after.evidence
                if item.ref.get("kind") == "pdf_table_read"
            ],
            key=lambda item: item["row_id"],
        ) == sorted(read_facts, key=lambda item: item["row_id"])
        named = next(
            item.ref for item in after.outputs if item.ref.get("kind") == "named_result"
        )
        assert named["row_ids"] == rows


def test_pdf_backfill_propagates_cross_source_schema_failure(pdf_source, monkeypatch):
    project, sheet, rows, column, _, _ = pdf_source
    first = run_action_spec(project, _extract_pdf_tables_action(sheet), project_id="p")
    assert first.status == "completed", first.errors
    project.add_rows(
        sheet,
        [{"pdf": value} for value in project.get_values(sheet, column).values()],
        {"pdf": column},
    )

    def mismatched(request):
        header = ["vendor"] if request.filename == "alpha.pdf" else ["different"]
        return [
            natural_pdf.PdfTable(
                page_start=1,
                page_end=1,
                table_index=0,
                header=header,
                rows=[["x"]],
                raw_cells=[["x"]],
            )
        ]

    monkeypatch.setattr(natural_pdf, "extract_pdf_tables", mismatched)
    body = {
        "action_id": "run.backfill",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet},
        "params": {"column": "pdf_tables"},
        "idempotency_key": "backfill",
    }
    result = run_action_spec(project, body, project_id="p")
    assert result.status == "failed", result.errors
    assert result.errors[0].code == "pdf_table_shape_mismatch", result.errors
    assert all(item.kind != "named_result" for item in result.outputs)


@pytest.mark.parametrize("behavior", ["empty", "failed", "reordered_headers"])
def test_pdf_no_false_feedable_schema(pdf_source, monkeypatch, behavior):
    project, sheet, _, _, _, _ = pdf_source

    def extract(request):
        if behavior == "failed":
            raise natural_pdf.NaturalPdfError("fixture failure")
        if behavior == "empty":
            return []
        header = ["a", "b"] if request.filename == "alpha.pdf" else ["b", "a"]
        return [
            natural_pdf.PdfTable(
                page_start=1,
                page_end=1,
                table_index=0,
                header=header,
                rows=[["1", "2"]],
                raw_cells=[["1", "2"]],
            )
        ]

    monkeypatch.setattr(natural_pdf, "extract_pdf_tables", extract)
    result = run_action_spec(project, _extract_pdf_tables_action(sheet), project_id="p")
    assert result.status == "failed", result.errors
    expected = (
        "pdf_table_shape_mismatch"
        if behavior == "reordered_headers"
        else "pdf_table_extract_failed"
    )
    assert result.errors[0].code == expected
    assert all(item.kind != "named_result" for item in result.outputs)


@pytest.mark.asyncio
async def test_pdf_reader_cancellation_settles_borrowed_resource(
    pdf_source, monkeypatch
):
    from frisket.engine.executor.pdf_tables_read import AdmittedPdfTablesReader

    project, sheet, rows, column, _, _ = pdf_source
    entered, release = Event(), Event()
    released = []
    original = project.materialize_blob

    @contextmanager
    def materialize(digest):
        with original(digest) as path:
            try:
                yield path
            finally:
                released.append(True)

    def extract(request):
        entered.set()
        assert release.wait(3)
        assert not released
        assert request.path.exists()
        return []

    monkeypatch.setattr(project, "materialize_blob", materialize)
    monkeypatch.setattr(natural_pdf, "extract_pdf_tables", extract)
    value = project.get_values(sheet, column)[rows[0]]
    row = Row({"pdf": value})
    reader = AdmittedPdfTablesReader(project)
    bound = reader.bind_row(
        row,
        sheet_id=sheet,
        row_id=rows[0],
        sources={"pdf": {"column_id": column, "value": value}},
    )
    task = asyncio.create_task(bound.read(row, ColumnRef("pdf")))
    assert await asyncio.to_thread(entered.wait, 3)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not released and not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert released == [True]
    await reader.aclose()
    with pytest.raises(RuntimeError, match="closed"):
        await bound.read(row, ColumnRef("pdf"))


@pytest.mark.asyncio
@pytest.mark.parametrize("refusal", ["cancelled", "page_limit"])
async def test_pdf_reader_refuses_before_adapter(pdf_source, refusal):
    from frisket.engine.executor.pdf_tables_read import AdmittedPdfTablesReader
    from frisket.execution.provider import ExecutionLimits, ExecutionLimitExceeded

    project, sheet, rows, column, _, calls = pdf_source
    value = project.get_values(sheet, column)[rows[0]]
    row = Row({"pdf": value})
    cancelled = []
    reader = AdmittedPdfTablesReader(
        project,
        cancelled=lambda: bool(cancelled),
        execution_limits=ExecutionLimits(max_pdf_pages=1),
    )
    bound = reader.bind_row(
        row,
        sheet_id=sheet,
        row_id=rows[0],
        sources={"pdf": {"column_id": column, "value": value}},
    )
    if refusal == "cancelled":
        cancelled.append(True)
    with pytest.raises(
        asyncio.CancelledError if refusal == "cancelled" else ExecutionLimitExceeded
    ):
        await bound.read(row, ColumnRef("pdf"))
    assert calls == []
    await reader.aclose()
