"""Document columns, plain conversion results, and the admitted converter."""

from __future__ import annotations

from typing import Any, ClassVar, Protocol

from pydantic import BaseModel, ConfigDict, Field

from frisket.actions.types import ColumnRef, Row

DEFAULT_DOCUMENT_ENGINE = "markitdown"


class DocumentColumn(ColumnRef[Any]):
    """A file or text column carrying a convertible document."""

    accepted_column_types: ClassVar[tuple[str, ...]] = ("file", "text")


class ConvertedDocument(BaseModel):
    """Converted text and per-page OCR flags, when reported by the engine."""

    model_config = ConfigDict(extra="forbid")

    markdown: str = Field(json_schema_extra={"format": "markdown"})
    ocr_used: list[bool] = Field(default_factory=list)


class DocumentConverter(Protocol):
    """Read a row's document through the engine selected at admission."""

    async def convert(self, row: Row, source: ColumnRef[Any]) -> ConvertedDocument: ...
