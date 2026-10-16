"""Read complete native PDF text through the ordinary typed row host."""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from frisket.actions.core import ActionCategory, action, map_rows
from frisket.actions.media_types import OcrText
from frisket.actions.native_pdf_types import NativePdfTextReader
from frisket.actions.types import ActionParams, ColumnRef, Row, RowResult


class PdfColumn(ColumnRef[Any]):
    accepted_column_types: ClassVar[tuple[str, ...]] = ("file",)


class ReadPdfTextParams(ActionParams):
    source: PdfColumn = Field(description="Choose a PDF file column.")


class NativePdfTextOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: OcrText


async def read_pdf_text(
    params: ReadPdfTextParams, row: Row, reader: NativePdfTextReader
) -> RowResult[NativePdfTextOutput]:
    return RowResult(
        output=NativePdfTextOutput(text=await reader.read(row, params.source))
    )


READ_PDF_TEXT = action(
    name="read_pdf_text",
    title="Read PDF text",
    description="Read existing text from every selected PDF page without OCR.",
    category=ActionCategory.EXTRACT,
    examples=(ReadPdfTextParams(source="document"),),
    run=map_rows(read_pdf_text),
)


__all__ = ["READ_PDF_TEXT", "NativePdfTextOutput", "PdfColumn", "ReadPdfTextParams"]
