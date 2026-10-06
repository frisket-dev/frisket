"""Visual extraction editor wire models; annotations remain ordinary action Params."""

from typing import Literal

from pydantic import Field

from frisket.actions.document_extract import DocumentExtractParams

from frisket.actions.document_extraction_types import (
    DocumentExtraction,
    ExtractionTemplate,
    PositionedDocument,
)
from frisket.contracts.http.models import WireModel


class ExtractionDocumentResponse(WireModel):
    row_id: int
    blob_id: str
    reference_page: int | None = None
    filename: str
    mime: str
    document: PositionedDocument


class ExtractionPreviewRequest(WireModel):
    sheet_id: int = Field(gt=0)
    source: str = Field(min_length=1)
    row_ids: list[int] | None = None
    template: ExtractionTemplate
    repeat_group_id: str | None = None


class ExtractionPreviewDocument(WireModel):
    row_id: int
    blob_id: str
    filename: str
    result: DocumentExtraction


class ExtractionPreviewResponse(WireModel):
    documents: list[ExtractionPreviewDocument]
    truncated: bool = False


class ExtractionTemplateSave(WireModel):
    id: int | None = Field(default=None, gt=0)
    name: str = Field(min_length=1, max_length=200)
    sheet_id: int = Field(gt=0)
    reference_row_id: int = Field(gt=0)
    source: str = Field(min_length=1)
    template: ExtractionTemplate
    repeat_group_id: str | None = None


class ExtractionSavedSpec(WireModel):
    action_kind: Literal["media.extract_document"]
    project_id: str
    sheet_id: int
    reference_row_id: int
    params: DocumentExtractParams


class ExtractionSavedTemplate(WireModel):
    id: int
    name: str
    sheet_id: int
    reference_row_id: int
    spec: ExtractionSavedSpec


class ExtractionTemplatesResponse(WireModel):
    templates: list[ExtractionSavedTemplate]
