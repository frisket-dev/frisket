from __future__ import annotations

import hashlib
import io
from contextlib import closing
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict, Field

from frisket.actions.core import ActionCategory, ActionNamespace, ActionRegistry
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.types import (
    ActionParams,
    ActionRequest,
    ImportBlobStager,
    StagedFile,
    TableResult,
    TableRow,
)
from frisket.engine.executor import table_action
from frisket.engine.executor.import_blob_stage import AdmittedImportBlobStager
from frisket.engine.executor.table_action import run_typed_create_sheet_action
from frisket.engine.store import Project
from frisket.engine.store.receipts import ReceiptStore
from frisket.sdk import action, create_sheet


class EmailFeatureParams(ActionParams):
    pass


class EmailFilesRow(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    from_: str = Field(alias="from")
    files_: list[StagedFile] = Field(alias="attachments", min_length=0)


class UntypedFilesRow(BaseModel):
    payload: dict[str, Any]


def _bound(producer, *, output_names=None):
    definition = action(
        name="email_features",
        title="Email features",
        description="Exercise static table attachments and final diagnostics.",
        category=ActionCategory.CONVERT,
        run=create_sheet(producer),
    )
    registered = ActionRegistry(
        (ActionNamespace("example", actions=(definition,)),)
    ).get("example.email_features")
    return BoundTypedActionRequest.bind(
        registered,
        ActionRequest(
            action_id=registered.action_id,
            scope={"kind": "project"},
            params={},
            output_names=output_names or {},
            sheet_name="Messages",
            idempotency_key="email-features",
        ),
    )


def _stage(blobs, filename):
    return blobs.stage(
        io.BytesIO(b"identical attachment bytes"),
        filename=filename,
        mime="application/octet-stream",
    )


def _assert_no_publication(project):
    assert not project.db.in_transaction
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
        assert project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


@pytest.mark.parametrize("streamed", [False, True])
def test_static_file_lists_preserve_order_occurrences_and_renamed_aliases(
    tmp_path, monkeypatch, streamed
):
    monkeypatch.setattr(table_action, "_TABLE_BATCH_SIZE", 1)
    calls = []

    def produce(
        params: EmailFeatureParams, blobs: ImportBlobStager
    ) -> TableResult[EmailFilesRow]:
        calls.append(True)
        first = _stage(blobs, "first.bin")
        second = _stage(blobs, "second.bin")
        rows = [
            TableRow(output=EmailFilesRow(from_="Sender", files_=files))
            for files in ([first, second, first], [second, first], [])
        ]
        return TableResult(rows=iter(rows) if streamed else rows)

    bound = _bound(produce, output_names={"from": "Sender", "attachments": "Evidence"})
    assert [(field.key, field.column_type) for field in bound.output_fields] == [
        ("from", "text"),
        ("attachments", "json"),
    ]
    assert calls == []
    with closing(Project.create(tmp_path / "project")) as project:
        seed = project.add_sheet("Existing")
        column = project.add_column(seed, "Value", type="text")
        project.add_rows(seed, [{"Value": "one"}, {"Value": "two"}], {"Value": column})
        result = run_typed_create_sheet_action(project, "p", bound)
        assert result.status == "completed", result.errors
        sheet = result.outputs[0].ref
        assert set(sheet["columns"]) == {"Sender", "Evidence"}
        column_id = sheet["columns"]["Evidence"]
        values = project.get_values(sheet["sheet_id"], column_id)
        row_ids = project.visible_row_ids(sheet["sheet_id"])
        assert len(row_ids) == 3 and min(row_ids) > 2
        filenames = [
            ["first.bin", "second.bin", "first.bin"],
            ["second.bin", "first.bin"],
            [],
        ]
        digest = hashlib.sha256(b"identical attachment bytes").hexdigest()
        assert [values[row_id] for row_id in row_ids] == [
            [
                {
                    "blob": digest,
                    "mime": "application/octet-stream",
                    "filename": name,
                }
                for name in names
            ]
            for names in filenames
        ]
        receipt = ReceiptStore(project).parsed_by_id(result.receipt_id)
        refs = [
            item.ref for item in receipt.evidence if item.ref["kind"] == "imported_blob"
        ]
        assert [
            (
                ref["row_id"],
                ref["column_id"],
                ref["row_index"],
                ref["filename"],
                ref["hash"],
            )
            for ref in refs
        ] == [
            (row_id, column_id, position, filename, digest)
            for position, (row_id, names) in enumerate(
                zip(row_ids, filenames, strict=True), 1
            )
            for filename in names
        ]
        assert project.db.execute("SELECT COUNT(*) FROM blobs").fetchone()[0] == 1
        replay = run_typed_create_sheet_action(project, "p", bound)
        assert replay.status == "completed", replay.errors
        assert replay.receipt_id == result.receipt_id
        assert calls == [True]


@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("invalid", ["foreign", "untyped_nested"])
def test_file_lists_do_not_admit_foreign_or_untyped_nested_tokens(
    tmp_path, monkeypatch, streamed, invalid
):
    monkeypatch.setattr(table_action, "_TABLE_BATCH_SIZE", 1)
    with AdmittedImportBlobStager() as foreign:
        other = _stage(foreign, "foreign.bin")
        if invalid == "foreign":

            def produce(
                params: EmailFeatureParams, blobs: ImportBlobStager
            ) -> TableResult[EmailFilesRow]:
                local = _stage(blobs, "local.bin")
                rows = [
                    TableRow(output=EmailFilesRow(from_="Sender", files_=files))
                    for files in ([local], [local, other])
                ]
                return TableResult(rows=iter(rows) if streamed else rows)

        else:

            def produce(
                params: EmailFeatureParams, blobs: ImportBlobStager
            ) -> TableResult[UntypedFilesRow]:
                local = _stage(blobs, "local.bin")
                rows = [
                    TableRow(output=UntypedFilesRow(payload={"safe": []})),
                    TableRow(output=UntypedFilesRow(payload={"hidden": [local]})),
                ]
                return TableResult(rows=iter(rows) if streamed else rows)

        with closing(Project.create(tmp_path / "project")) as project:
            result = run_typed_create_sheet_action(project, "p", _bound(produce))
            assert result.status == "failed"
            assert result.errors[0].code == "invalid_params"
            _assert_no_publication(project)


def _closable_rows(rows, streamed, closed):
    class Buffered(list):
        def close(self):
            closed()

    class Streamed:
        def __init__(self):
            self.iterator = iter(rows)

        def __iter__(self):
            return self

        def __next__(self):
            return next(self.iterator)

        def close(self):
            closed()

    return Streamed() if streamed else Buffered(rows)


@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("fail", [False, True])
def test_final_warnings_are_consumed_once_after_row_close_before_publication(
    tmp_path, monkeypatch, streamed, fail
):
    monkeypatch.setattr(table_action, "_TABLE_BATCH_SIZE", 1)
    events = []
    late = []
    with closing(Project.create(tmp_path / "project")) as project:

        class Warnings:
            def __iter__(self):
                assert events == ["rows_closed"]
                events.append("warnings_read")
                assert not project.db.in_transaction
                for table in ("blobs", "ops", "receipts"):
                    assert (
                        project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[
                            0
                        ]
                        == 0
                    )
                yield from late
                if fail:
                    raise RuntimeError("late warning iteration failed")

        def closed():
            events.append("rows_closed")
            late.append("Late malformed sibling skipped")

        def produce(
            params: EmailFeatureParams, blobs: ImportBlobStager
        ) -> TableResult[EmailFilesRow]:
            attachment = _stage(blobs, "message.bin")
            rows = [TableRow(output=EmailFilesRow(from_="Sender", files_=[attachment]))]
            result = TableResult(
                rows=_closable_rows(rows, streamed, closed), warnings=Warnings()
            )
            assert events == []
            return result

        bound = _bound(produce)
        result = run_typed_create_sheet_action(project, "p", bound)
        assert events == ["rows_closed", "warnings_read"]
        if fail:
            assert result.status == "failed"
            _assert_no_publication(project)
        else:
            assert result.status == "completed", result.errors
            assert result.warnings == late
            receipt = ReceiptStore(project).parsed_by_id(result.receipt_id)
            assert receipt.warnings == late
            replay = run_typed_create_sheet_action(project, "p", bound)
            assert replay.status == "completed", replay.errors
            assert replay.receipt_id == result.receipt_id
            assert replay.warnings == late
            assert events == ["rows_closed", "warnings_read"]


@pytest.mark.parametrize("streamed", [False, True])
def test_final_warnings_bound_examples_lengths_and_iterator_consumption(
    tmp_path, streamed
):
    visited = []

    def warnings():
        for index in range(103):
            assert index <= 100, "host must not drain warnings past the omission probe"
            visited.append(index)
            yield f"{index}:" + "x" * 2100

    def produce(
        params: EmailFeatureParams, blobs: ImportBlobStager
    ) -> TableResult[EmailFilesRow]:
        rows = [TableRow(output=EmailFilesRow(from_="Sender", files_=[]))]
        return TableResult(rows=iter(rows) if streamed else rows, warnings=warnings())

    with closing(Project.create(tmp_path / "project")) as project:
        result = run_typed_create_sheet_action(project, "p", _bound(produce))
        assert result.status == "completed", result.errors
        assert visited == list(range(101))
        assert len(result.warnings) == 101
        for index, warning in enumerate(result.warnings[:100]):
            assert warning.startswith(f"{index}:")
            assert len(warning) <= 2000
        assert "omitted" in result.warnings[-1].lower()
        assert len(result.warnings[-1]) <= 2000
        receipt = ReceiptStore(project).parsed_by_id(result.receipt_id)
        assert receipt.warnings == result.warnings
