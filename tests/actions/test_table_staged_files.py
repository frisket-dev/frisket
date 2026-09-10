from __future__ import annotations

import hashlib
import io
from contextlib import closing

import pytest
from pydantic import BaseModel, Field

from frisket.actions.core import (
    ActionCategory,
    ActionNamespace,
    ActionRegistry,
    action,
    create_sheet,
)
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.types import (
    ActionParams,
    ActionRequest,
    DynamicOutput,
    ImportBlobStager,
    PdfDocument,
    PdfPage,
    StagedFile,
    TableColumn,
    TableResult,
    TableRow,
)
from frisket.engine.executor import table_action
from frisket.engine.executor.import_blob_stage import AdmittedImportBlobStager
from frisket.engine.executor.table_action import run_typed_create_sheet_action
from frisket.engine.store import Project
from frisket.engine.store.receipts import ReceiptStore


class StaticParams(ActionParams):
    pass


class AttachmentRow(BaseModel):
    attachment: StagedFile


class DeclaredParams(ActionParams):
    columns: list[TableColumn] = Field(min_length=1)


def _bound(producer, *, columns_from=None, params=None, output_names=None):
    definition = action(
        name="attachments",
        title="Attachments",
        description="Publish staged files in a typed table.",
        category=ActionCategory.CONVERT,
        run=create_sheet(producer, columns_from=columns_from),
    )
    registered = ActionRegistry(
        (ActionNamespace("example", actions=(definition,)),)
    ).get("example.attachments")
    return BoundTypedActionRequest.bind(
        registered,
        ActionRequest(
            action_id="example.attachments",
            scope={"kind": "project"},
            params=params or {},
            output_names=output_names or {},
            sheet_name="Attachments",
            idempotency_key="staged-table",
        ),
    )


@pytest.mark.parametrize("streamed", [False, True])
def test_static_staged_file_output_publishes_inferred_schema_and_replays(
    tmp_path, streamed
):
    calls = []

    def produce(
        params: StaticParams, blobs: ImportBlobStager
    ) -> TableResult[AttachmentRow]:
        calls.append(True)
        handle = blobs.stage(
            io.BytesIO(b"attachment bytes"),
            filename="source.bin",
            mime="application/octet-stream",
        )
        rows = [TableRow(output=AttachmentRow(attachment=handle)) for _ in range(2)]
        return TableResult(rows=iter(rows) if streamed else rows)

    bound = _bound(produce, output_names={"attachment": "Evidence"})
    assert [(field.key, field.column_type) for field in bound.output_fields] == [
        ("attachment", "file")
    ]
    with closing(Project.create(tmp_path / "project")) as project:
        seed = project.add_sheet("Existing")
        column = project.add_column(seed, "Value", type="text")
        project.add_rows(seed, [{"Value": "existing"}], {"Value": column})
        result = run_typed_create_sheet_action(project, "p", bound)
        assert result.status == "completed", result.errors
        sheet = result.outputs[0].ref
        column_id = sheet["columns"]["Evidence"]
        values = project.get_values(sheet["sheet_id"], column_id)
        assert len(values) == 2
        digest = hashlib.sha256(b"attachment bytes").hexdigest()
        assert (
            list(values.values())
            == [
                {
                    "blob": digest,
                    "filename": "source.bin",
                    "mime": "application/octet-stream",
                }
            ]
            * 2
        )
        receipt = ReceiptStore(project).parsed_by_id(result.receipt_id)
        refs = [
            item.ref for item in receipt.evidence if item.ref["kind"] == "imported_blob"
        ]
        assert {(ref["row_id"], ref["column_id"]) for ref in refs} == {
            (row_id, column_id) for row_id in values
        }
        assert sorted(ref["row_index"] for ref in refs) == [1, 2]
        assert project.db.execute("SELECT COUNT(*) FROM blobs").fetchone()[0] == 1
        replay = run_typed_create_sheet_action(project, "p", bound)
        assert replay.status == "completed", replay.errors
        assert replay.receipt_id == result.receipt_id
        assert calls == [True]


@pytest.mark.parametrize("streamed", [False, True])
def test_params_defined_columns_preserve_every_repeated_handle_occurrence(
    tmp_path, streamed
):
    def produce(
        params: DeclaredParams, blobs: ImportBlobStager
    ) -> TableResult[DynamicOutput]:
        handle = blobs.stage(
            io.BytesIO(b"shared"),
            filename="shared.bin",
            mime="application/octet-stream",
        )
        rows = [
            TableRow(
                output=DynamicOutput({column.key: handle for column in params.columns})
            )
            for _ in range(2)
        ]
        return TableResult(rows=iter(rows) if streamed else rows)

    bound = _bound(
        produce,
        columns_from=lambda params: params.columns,
        params={
            "columns": [
                {"key": "left", "type": "file"},
                {"key": "right", "type": "file"},
            ]
        },
        output_names={"left": "First attachment", "right": "Second attachment"},
    )
    with closing(Project.create(tmp_path / "project")) as project:
        result = run_typed_create_sheet_action(project, "p", bound)
        assert result.status == "completed", result.errors
        sheet = result.outputs[0].ref
        rows = project.visible_row_ids(sheet["sheet_id"])
        receipt = ReceiptStore(project).parsed_by_id(result.receipt_id)
        refs = [
            item.ref for item in receipt.evidence if item.ref["kind"] == "imported_blob"
        ]
        assert len(refs) == 4
        assert {(ref["row_id"], ref["column_id"]) for ref in refs} == {
            (row_id, column_id)
            for row_id in rows
            for column_id in sheet["columns"].values()
        }
        assert set(sheet["columns"]) == {"First attachment", "Second attachment"}
        assert len({ref["hash"] for ref in refs}) == 1
        assert project.db.execute("SELECT COUNT(*) FROM blobs").fetchone()[0] == 1


@pytest.mark.parametrize("streamed", [False, True])
def test_equal_pdf_bytes_keep_separate_document_and_page_associations(
    tmp_path, streamed
):
    def produce(
        params: DeclaredParams, blobs: ImportBlobStager
    ) -> TableResult[DynamicOutput]:
        rows = []
        for page in (1, 2):
            document = blobs.stage(
                io.BytesIO(b"same PDF bytes"),
                filename=f"document-{page}.pdf",
                mime="application/pdf",
                role=PdfDocument(),
            )
            image = blobs.stage(
                io.BytesIO(b"same image bytes"),
                filename=f"page-{page}.png",
                mime="image/png",
                role=PdfPage(document=document, page=page),
            )
            rows.append(TableRow(output=DynamicOutput({"image": image})))
        return TableResult(rows=iter(rows) if streamed else rows)

    bound = _bound(
        produce,
        columns_from=lambda params: params.columns,
        params={"columns": [{"key": "image", "type": "image"}]},
        output_names={"image": "Preview"},
    )
    with closing(Project.create(tmp_path / "project")) as project:
        result = run_typed_create_sheet_action(project, "p", bound)
        assert result.status == "completed", result.errors
        sheet = result.outputs[0].ref
        receipt = ReceiptStore(project).parsed_by_id(result.receipt_id)
        documents = [
            item.ref
            for item in receipt.evidence
            if item.ref["kind"] == "imported_pdf_document"
        ]
        pages = [
            item.ref
            for item in receipt.evidence
            if item.ref["kind"] == "imported_pdf_page_image"
        ]
        assert len(documents) == len(pages) == 2
        assert len({ref["hash"] for ref in documents}) == 1
        assert len({ref["hash"] for ref in pages}) == 1
        assert len({ref["artifact_id"] for ref in documents}) == 2
        docs_by_name = {ref["filename"]: ref for ref in documents}
        for ref in pages:
            assert (
                ref["artifact_id"]
                == docs_by_name[f"document-{ref['page']}.pdf"]["artifact_id"]
            )
            assert ref["column_id"] == sheet["columns"]["Preview"]
            link = project.db.execute(
                "SELECT row_id, column_id FROM evidence_links WHERE id=?",
                (ref["evidence_link_id"],),
            ).fetchone()
            assert tuple(link) == (ref["row_id"], ref["column_id"])
        assert sorted(ref["page"] for ref in pages) == [1, 2]
        assert project.db.execute("SELECT COUNT(*) FROM blobs").fetchone()[0] == 2


@pytest.mark.parametrize("failure", ["forged", "foreign", "nested", "raw_cell"])
@pytest.mark.parametrize("streamed", [False, True])
def test_invalid_staged_file_never_publishes_sql(
    tmp_path, monkeypatch, failure, streamed
):
    monkeypatch.setattr(table_action, "_TABLE_BATCH_SIZE", 1)
    with AdmittedImportBlobStager() as foreign:
        other = foreign.stage(
            io.BytesIO(b"foreign"),
            filename="foreign.bin",
            mime="application/octet-stream",
        )

        def produce(
            params: DeclaredParams, blobs: ImportBlobStager
        ) -> TableResult[DynamicOutput]:
            local = blobs.stage(
                io.BytesIO(b"local"),
                filename="local.bin",
                mime="application/octet-stream",
            )
            invalid = StagedFile(size=local.size) if failure == "forged" else other
            valid = local
            if failure == "nested":
                valid, invalid = {"ok": True}, {"hidden": [local]}
            elif failure == "raw_cell":
                invalid = {
                    "blob": hashlib.sha256(b"local").hexdigest(),
                    "filename": "local.bin",
                    "mime": "application/octet-stream",
                }
            rows = [
                TableRow(output=DynamicOutput({"value": valid})),
                TableRow(output=DynamicOutput({"value": invalid})),
            ]
            return TableResult(rows=iter(rows) if streamed else rows)

        bound = _bound(
            produce,
            columns_from=lambda params: params.columns,
            params={
                "columns": [
                    {"key": "value", "type": "json" if failure == "nested" else "file"}
                ]
            },
        )
        with closing(Project.create(tmp_path / "project")) as project:
            result = run_typed_create_sheet_action(project, "p", bound)
            assert result.status == "failed"
            assert result.errors[0].code == "invalid_params"
            for table in (
                "sheets",
                "columns",
                "rows",
                "cells",
                "ops",
                "receipts",
                "blobs",
                "source_artifacts",
                "source_spans",
                "evidence_links",
            ):
                assert (
                    project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    == 0
                ), table
