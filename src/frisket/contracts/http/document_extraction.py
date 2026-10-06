"""Visual extraction editor wire models; annotations remain ordinary action Params."""

from typing import Literal

from pydantic import Field

from frisket.actions.document_extraction_types import (
    DocumentExtraction,
    ExtractionField,
    ExtractionScope,
    ExtractionTemplate,
    IgnoreBand,
    PageRegion,
    PositionedDocument,
    RepeatedSection,
)
from frisket.contracts.http.models import WireModel


class ExtractionDocumentResponse(WireModel):
    row_id: int
    blob_id: str
    filename: str
    mime: str
    document: PositionedDocument


class ExtractionPreviewRequest(WireModel):
    sheet_id: int = Field(gt=0)
    source: str = Field(min_length=1)
    scope: ExtractionScope
    layout_id: int | None = Field(default=None, gt=0)
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


class ExtractionDraftPending(WireModel):
    tool: Literal["key", "repeat"]
    region: PageRegion


class ExtractionLayoutDraft(WireModel):
    """Editable state, deliberately less strict than an executable template."""

    reference_blob_id: str = ""
    reference_fingerprint: str = ""
    fields: list[ExtractionField] = Field(default_factory=list, max_length=200)
    sections: list[RepeatedSection] = Field(default_factory=list, max_length=20)
    ignore_bands: list[IgnoreBand] = Field(default_factory=list, max_length=20)
    expand_values: bool = False
    look_every_page: bool = True
    continue_across_pages: bool = False
    pending: ExtractionDraftPending | None = None


class ExtractionTemplateSave(WireModel):
    id: int | None = Field(default=None, gt=0)
    sheet_id: int = Field(gt=0)
    reference_row_id: int | None = Field(default=None, gt=0)
    source: str = Field(min_length=1)
    draft: ExtractionLayoutDraft
    repeat_group_id: str | None = None


class ExtractionSavedLayout(WireModel):
    id: int
    name: str
    sheet_id: int
    source: str
    source_column_id: int
    reference_row_id: int | None = None
    draft: ExtractionLayoutDraft
    repeat_group_id: str | None = None
    has_applied: bool


class ExtractionTemplatesResponse(WireModel):
    templates: list[ExtractionSavedLayout]
    selected_layout_id: int | None = None


class ExtractionLayoutSelection(WireModel):
    sheet_id: int = Field(gt=0)
    source: str = Field(min_length=1)
    layout_id: int = Field(gt=0)


class ExtractionScopeCountsRequest(WireModel):
    sheet_id: int = Field(gt=0)
    source: str = Field(min_length=1)
    layout_id: int | None = Field(default=None, gt=0)
    row_id: int | None = Field(default=None, gt=0)
    filter: dict | None = None
    parent_row_id: int | None = Field(default=None, gt=0)
    scope_row_ids: list[int] | None = None


class ExtractionScopeCountsResponse(WireModel):
    all: int
    filter: int
    layout: int
    this: int
