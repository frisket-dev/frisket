"""Extract local PDF tables into one ordinary row-local JSON list."""

from typing import Any, ClassVar

from pydantic import BaseModel

from frisket.actions.core import ActionCategory, action, map_rows
from frisket.actions.pdf_table_types import (
    PdfTableOptions,
    PdfTableRows,
    PdfTablesReader,
)
from frisket.actions.types import ColumnRef, Row, RowResult


class PdfColumn(ColumnRef[Any]):
    accepted_column_types: ClassVar[tuple[str, ...]] = ("file",)


class PdfTablesParams(PdfTableOptions):
    source: PdfColumn


class PdfTablesOutput(BaseModel):
    pdf_tables: PdfTableRows


async def extract_pdf_tables(
    params: PdfTablesParams, row: Row, reader: PdfTablesReader
) -> RowResult[PdfTablesOutput]:
    return RowResult(
        output=PdfTablesOutput(
            pdf_tables=await reader.read(
                row,
                params.source,
                mode=params.mode,
                table_mode=params.table_mode,
                extract_table=params.extract_table,
            )
        )
    )


EXTRACT_PDF_TABLES = action(
    name="extract_pdf_tables",
    title="Extract PDF tables",
    description="Extract normalized table rows and source/page metadata from each blob-backed PDF.",
    category=ActionCategory.EXTRACT,
    examples=(PdfTablesParams(source="pdf"),),
    run=map_rows(extract_pdf_tables),
)
