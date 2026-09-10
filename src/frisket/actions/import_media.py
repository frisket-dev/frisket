"""File attachments and PDF pages produced through host-owned blob staging."""

from __future__ import annotations

import mimetypes
from itertools import chain
from pathlib import Path

from pydantic import Field

from frisket.actions.core import ActionCategory, action, create_sheet
from frisket.actions.imports import FileSource
from frisket.actions.types import (
    ActionParams,
    DynamicOutput,
    DynamicTableResult,
    ImportBlobStager,
    LocalFileReader,
    PdfDocument,
    PdfPageRenderer,
    TableColumn,
    TableError,
    TableResult,
    TableRow,
)


class FileImportSource(ActionParams):
    path: str = Field(min_length=1)
    filename: str | None = None
    mime: str | None = None


class ImportFilesParams(ActionParams):
    files: list[FileImportSource] = Field(min_length=1)


class ImportPdfParams(ActionParams):
    source: FileSource
    dpi: int = Field(default=150, ge=50, le=600)
    render_pages: bool = True


def _file_name_and_mime(source: FileImportSource) -> tuple[str, str]:
    filename = source.filename or Path(source.path).name
    mime = (
        source.mime or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    )
    return filename, mime


def file_columns(params: ImportFilesParams) -> tuple[TableColumn, ...]:
    kinds = set()
    for source in params.files:
        _, mime = _file_name_and_mime(source)
        kinds.add(
            next(
                (
                    kind
                    for kind in ("audio", "video", "image")
                    if mime.startswith(f"{kind}/")
                ),
                "file",
            )
        )
    return (
        TableColumn(key="filename", type="text"),
        TableColumn(key="media", type=next(iter(kinds)) if len(kinds) == 1 else "file"),
        TableColumn(key="size", type="integer", format="filesize"),
    )


def import_files(
    params: ImportFilesParams, files: LocalFileReader, blobs: ImportBlobStager
) -> TableResult[DynamicOutput]:
    def rows():
        for source in params.files:
            filename, mime = _file_name_and_mime(source)
            with files.open_binary(source.path) as stream:
                staged = blobs.stage(stream, filename=filename, mime=mime)
            yield TableRow(
                output=DynamicOutput(
                    {"filename": filename, "media": staged, "size": staged.size}
                )
            )

    return TableResult(
        rows=rows(),
        source={"kind": "file", "label": "files", "importer": "files"},
    )


def import_pdf(
    params: ImportPdfParams,
    files: LocalFileReader,
    blobs: ImportBlobStager,
    pdfs: PdfPageRenderer,
) -> DynamicTableResult:
    filename = params.source.label or Path(params.source.path).name
    columns = [
        TableColumn(key="page", type="integer"),
        TableColumn(key="text", type="text"),
        TableColumn(key="source", type="text"),
    ]
    if params.render_pages:
        columns.append(TableColumn(key="page_image", type="image"))
    warnings = []

    def rows():
        try:
            with files.open_binary(params.source.path) as stream:
                document = blobs.stage(
                    stream,
                    filename=filename,
                    mime="application/pdf",
                    role=PdfDocument(),
                )
        except TableError as exc:
            if exc.code == "invalid_file_source":
                raise TableError(
                    "invalid_pdf_source", str(exc), details=exc.details
                ) from exc
            raise

        with blobs.open_binary(document) as stream:
            try:
                from pypdf import PdfReader

                pages = iter(PdfReader(stream).pages)
                first = next(pages, None)
            except Exception as exc:  # noqa: BLE001 - PDF parsing is an input boundary
                raise TableError(
                    "pdf_parse_failed", "PDF document could not be parsed"
                ) from exc
            if first is None:
                raise TableError("pdf_parse_failed", "PDF document has no pages")
            images = (
                pdfs.render(document, dpi=params.dpi) if params.render_pages else {}
            )
            for index, page in enumerate(chain((first,), pages), start=1):
                try:
                    text = page.extract_text() or ""
                except Exception:  # noqa: BLE001 - text extraction is best effort per page
                    text = ""
                values = {"page": index, "text": text, "source": filename}
                if params.render_pages:
                    values["page_image"] = images.get(index)
                    if index not in images and not warnings:
                        warnings.append(
                            "Some PDF page images were unavailable; text is provided without images for those pages."
                        )
                yield TableRow(output=DynamicOutput(values))

    return DynamicTableResult(
        schema=tuple(columns),
        rows=rows(),
        source={"kind": "file", "label": filename, "importer": "pdf"},
        warnings=warnings,
    )


FILES = action(
    examples=(
        ImportFilesParams(
            files=[FileImportSource(path="examples/report.pdf", mime="application/pdf")]
        ),
    ),
    name="files",
    title="Import files",
    description="Import files as typed attachments with their names and sizes.",
    category=ActionCategory.CONVERT,
    run=create_sheet(import_files, columns_from=file_columns),
)


PDF = action(
    examples=(
        ImportPdfParams(source=FileSource(kind="file", path="examples/report.pdf")),
    ),
    name="pdf",
    title="Import PDF",
    description="Import PDF pages with extracted text and optional page images.",
    category=ActionCategory.CONVERT,
    run=create_sheet(import_pdf),
)
