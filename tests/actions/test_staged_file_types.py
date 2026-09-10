from __future__ import annotations

import json

import pytest
from pydantic import BaseModel, TypeAdapter, ValidationError

from frisket.actions.core import ActionCategory, RegisteredAction
from frisket.actions.file_types import FileFetcher
from frisket.actions.model_rows import AskParams
from frisket.actions.types import RowError
from frisket.sdk import (
    ActionParams,
    DynamicOutput,
    ImportBlobStager,
    ModelPrompt,
    PdfDocument,
    PdfPage,
    Row,
    RowResult,
    Rows,
    StagedFile,
    TableResult,
    action,
    create_sheet,
    map_batch,
    map_rows,
    model_rows,
)


class Params(ActionParams):
    pass


class FileOutput(BaseModel):
    media: StagedFile
    optional_media: StagedFile | None


def produce(params: Params, blobs: ImportBlobStager) -> TableResult[FileOutput]:
    return TableResult(rows=[])


def map_file(params: Params, row: Row) -> RowResult[FileOutput]:
    raise NotImplementedError


def fetch_file(params: Params, row: Row, fetcher: FileFetcher) -> RowResult[FileOutput]:
    raise NotImplementedError


def batch_file(params: Params, rows: Rows) -> dict[int, RowResult[FileOutput]]:
    raise NotImplementedError


def model_file(params: AskParams, row: Row) -> ModelPrompt[FileOutput]:
    raise NotImplementedError


def test_staged_file_identity_survives_static_and_dynamic_validation():
    first, second = StagedFile(10), StagedFile(10)
    assert first is not second and first != second
    assert len({first, second}) == 2
    output = FileOutput.model_validate({"media": first, "optional_media": second})
    assert output.media is first
    assert output.optional_media is second
    assert FileOutput.model_validate(output).media is first
    assert TypeAdapter(StagedFile).validate_python(first) is first
    assert FileOutput(media=first, optional_media=None).optional_media is None

    dynamic = DynamicOutput.model_validate({"first": first, "second": second})
    assert dynamic.root["first"] is first
    assert dynamic.root["second"] is second
    assert DynamicOutput.model_validate(dynamic).root["first"] is first


@pytest.mark.parametrize(
    "value",
    [
        {"size": 10},
        {"blob": "digest", "mime": "image/png", "filename": "a.png"},
        10,
    ],
)
def test_staged_file_validation_cannot_reconstruct_a_token(value):
    adapter = TypeAdapter(StagedFile)
    with pytest.raises(ValidationError):
        adapter.validate_python(value)
    with pytest.raises(ValidationError):
        adapter.validate_json(json.dumps(value))


def test_staged_file_and_pdf_roles_are_immutable_without_storage_coordinates():
    file = StagedFile(0)
    page = PdfPage(document=file, page=1)
    assert page.document is file
    assert PdfDocument() == PdfDocument()
    for value, field, replacement in ((file, "size", 1), (page, "page", 2)):
        with pytest.raises(AttributeError):
            setattr(value, field, replacement)
    assert not hasattr(file, "path")
    assert not hasattr(file, "digest")
    assert not hasattr(file, "value")


@pytest.mark.parametrize("size", [-1, True, 1.5, "1"])
def test_staged_file_size_is_an_observed_nonnegative_integer(size):
    with pytest.raises(ValueError, match="size"):
        StagedFile(size)


@pytest.mark.parametrize("page", [0, -1, True, 1.5, "1"])
def test_pdf_page_numbers_are_positive_integers(page):
    with pytest.raises(ValueError, match="page"):
        PdfPage(document=StagedFile(0), page=page)


def test_pdf_page_requires_a_nominal_document_handle():
    with pytest.raises(ValueError, match="document"):
        PdfPage(document={"size": 1}, page=1)


def test_static_table_infers_file_columns_and_projects_only_emitted_json_schema():
    terminal = create_sheet(produce)
    assert terminal.capabilities == (ImportBlobStager,)
    assert [(field.key, field.column_type) for field in terminal.output_fields] == [
        ("media", "file"),
        ("optional_media", "file"),
    ]
    registered = RegisteredAction(
        "test.files",
        action(
            name="files",
            title="Files",
            description="Stage files",
            category=ActionCategory.CONVERT,
            run=terminal,
        ),
    )
    catalog = registered.catalog_entry()
    envelope = {
        "type": "object",
        "properties": {
            "blob": {"type": "string"},
            "mime": {"type": "string"},
            "filename": {"type": "string"},
        },
        "required": ["blob", "mime", "filename"],
        "additionalProperties": False,
    }
    assert TypeAdapter(StagedFile).json_schema() == envelope
    assert catalog["output_schema"]["properties"]["media"] == {
        **envelope,
        "title": "Media",
    }
    assert catalog["output_schema"]["properties"]["optional_media"]["anyOf"] == [
        envelope,
        {"type": "null"},
    ]
    assert "write_blob_store" in catalog["side_effects"]
    assert "write_evidence_links" in catalog["side_effects"]
    json.dumps(catalog)


def test_map_rows_admits_declared_capability_file_outputs_not_forged_handles():
    from frisket.engine.executor.row_file_stage import RowFileStager

    terminal = map_rows(fetch_file)
    assert terminal.capabilities == (FileFetcher,)
    assert [(field.key, field.column_type) for field in terminal.output_fields] == [
        ("media", "file"),
        ("optional_media", "file"),
    ]
    owner = RowFileStager(None)
    try:
        with pytest.raises(RowError, match="not issued"):
            owner.bind_row(1).dump_field(StagedFile, StagedFile(10), "media")
    finally:
        owner.close()


@pytest.mark.parametrize("terminal", ["map_batch", "model_rows"])
def test_other_row_terminals_do_not_advertise_staged_publication(terminal):
    with pytest.raises(TypeError, match="unsupported output field type"):
        if terminal == "map_batch":
            map_batch(batch_file)
        else:
            model_rows(model_file)


def test_dynamic_row_outputs_cannot_declare_staged_file_columns():
    def dynamic(params: Params, row: Row) -> RowResult[DynamicOutput]:
        raise NotImplementedError

    terminal = map_rows(dynamic, dynamic_outputs=lambda params: {"media": StagedFile})
    with pytest.raises(TypeError, match="invalid dynamic output type"):
        terminal.resolve_output_fields(Params())
