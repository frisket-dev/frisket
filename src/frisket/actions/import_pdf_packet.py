"""Atomic table producer for server-prepared PDF packet children."""

from __future__ import annotations

from pydantic import Field

from frisket.actions.core import ActionCategory, action, create_sheet
from frisket.actions.pdf_packet_import_types import PacketSplitReader
from frisket.actions.types import (
    ActionParams,
    DynamicOutput,
    ImportBlobStager,
    PdfDocument,
    TableColumn,
    TableResult,
    TableRow,
)


class ImportPdfPacketParams(ActionParams):
    source_ref: str = Field(min_length=1)
    keep_ocr_text: bool = False


def packet_columns(params: ImportPdfPacketParams) -> tuple[TableColumn, ...]:
    columns = [
        TableColumn(key="filename", type="text"),
        TableColumn(key="media", type="file"),
        TableColumn(key="size", type="integer", format="filesize"),
    ]
    if params.keep_ocr_text:
        columns.append(TableColumn(key="OCR text", type="text", default_hidden=True))
    return tuple(columns)


def import_pdf_packet(
    params: ImportPdfPacketParams,
    packets: PacketSplitReader,
    blobs: ImportBlobStager,
) -> TableResult[DynamicOutput]:
    manifest = packets.manifest(params.source_ref)

    with packets.open_source(params.source_ref) as source:
        document = blobs.stage(
            source,
            filename=manifest.filename,
            mime="application/pdf",
            role=PdfDocument(),
        )
    rows = []
    for child in manifest.children:
        if params.keep_ocr_text and not child.ocr_pages:
            raise ValueError("retained packet OCR must cover every child")
        if not params.keep_ocr_text and child.ocr_pages:
            raise ValueError("packet OCR was supplied without retention")
        with packets.open_child(params.source_ref, child.index) as source:
            staged = blobs.stage(
                source,
                filename=child.filename,
                mime="application/pdf",
            )
        blobs.associate_packet_split(
            document,
            staged,
            source_page_count=manifest.page_count,
            sibling_count=len(manifest.children),
            page_start=child.page_start,
            page_end=child.page_end,
            ocr_pages=child.ocr_pages,
            prepared_column_name="OCR text",
        )
        values = {
            "filename": child.filename,
            "media": staged,
            "size": staged.size,
        }
        if params.keep_ocr_text:
            values["OCR text"] = "\n\n".join(page.text for page in child.ocr_pages)
        rows.append(TableRow(output=DynamicOutput(values)))

    return TableResult(
        rows=rows,
        source={"kind": "file", "label": manifest.filename, "importer": "pdf_packet"},
    )


PDF_PACKET = action(
    examples=(ImportPdfPacketParams(source_ref="packet-session-ref"),),
    name="packet_split",
    title="Import split PDF packet",
    description="Import confirmed document ranges from a prepared PDF packet.",
    category=ActionCategory.CONVERT,
    run=create_sheet(import_pdf_packet, columns_from=packet_columns),
)


__all__ = ["ImportPdfPacketParams", "PDF_PACKET", "import_pdf_packet"]
