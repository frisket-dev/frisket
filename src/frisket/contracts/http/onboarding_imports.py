"""HTTP contracts for onboarding seed and JSON import routes.

The paste-draft producer owns a deliberately extensible draft shape, while
the seed and successful URL-import responses are fixed route products. URL
imports also retain their existing v1 ``ActionResult`` failure transport.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from frisket.contracts.action import ActionResult
from frisket.contracts.http.models import HttpError, WireModel


class ImportPasteDraftBody(BaseModel):
    """Existing paste request boundary, exported with its HTTP contract."""

    raw: str = Field(min_length=1, max_length=2_000_000)


class ImportUpdatePreviewColumn(BaseModel):
    source_name: str = Field(min_length=1)
    name: str | None = Field(default=None, min_length=1)
    type: str = Field(min_length=1)
    format: str | None = None


class ImportUpdatePreviewBody(BaseModel):
    raw: str = Field(min_length=1, max_length=2_000_000)
    draft_id: str = Field(min_length=1)
    destination_sheet_id: int = Field(ge=1, strict=True)
    columns: list[ImportUpdatePreviewColumn] = Field(min_length=1)
    key_columns: list[str] = Field(min_length=1)
    keep_existing_on_blank: bool = False


class ImportPasteConfirmBody(BaseModel):
    raw: str = Field(min_length=1, max_length=2_000_000)
    draft_id: str = Field(min_length=1)
    columns: list[ImportUpdatePreviewColumn] = Field(min_length=1)
    sheet_name: str | None = Field(default=None, min_length=1)
    destination_sheet_id: int | None = Field(default=None, ge=1, strict=True)
    key_columns: list[str] | None = None
    keep_existing_on_blank: bool = False
    confirmation: str | None = Field(default=None, min_length=1)


class ImportPasteConfirmResponse(WireModel):
    sheet_id: int
    rows: int
    columns: list[str]


class ImportUpdatePreviewSample(WireModel):
    source_row: int
    key: dict[str, JsonValue]
    status: Literal["matched", "unmatched", "blank_key", "ambiguous"]
    target_row_id: int | None
    before: dict[str, JsonValue]
    after: dict[str, JsonValue]


class ImportUpdatePreviewResponse(WireModel):
    matched: int
    unmatched: int
    blank_keys: int
    ambiguous: int
    changed_cells: int
    cleared_cells: int
    samples: list[ImportUpdatePreviewSample]
    confirmation: str


class ImportUrlsBody(BaseModel):
    """Existing JSON URL-import request boundary and service defaults."""

    urls: list[str]
    sheet_name: str | None = None
    column: str = "media"


class SampleProjectSeedResponse(WireModel):
    ok: bool
    project_id: str
    sheet_id: int
    sheet_name: str
    blank_column: str
    rows: int


class ImportPasteDraftColumn(WireModel):
    """Stable column metadata with room for producer-owned additions."""

    model_config = ConfigDict(extra="allow")

    key: str
    name: str
    type: str
    include: bool
    sample_values: list[JsonValue]
    # The paste producer deliberately emits this only for markdown columns.
    # response_model_exclude_unset keeps its absence distinct from null.
    format: str | None = None


class ImportPasteDraftSource(WireModel):
    """Stable provenance metadata with producer-owned extension leaves."""

    model_config = ConfigDict(extra="allow")

    kind: str
    label: str
    fingerprint: str
    line_count: int


class ImportPasteDraftResponse(WireModel):
    """Typed paste-draft core while preserving producer-owned extensions."""

    model_config = ConfigDict(extra="allow")

    schema_version: Literal["frisket.import_draft.v2"]
    draft_id: str
    source_kind: Literal["paste"]
    sheet_name: str
    row_count: int
    columns: list[ImportPasteDraftColumn]
    preview_rows: list[dict[str, JsonValue]]
    warnings: list[str]
    source: ImportPasteDraftSource


class ImportUrlsResponse(WireModel):
    sheet_id: int
    rows: int
    downloaded: int
    failed: int


class ImportCsvResponse(WireModel):
    sheet_id: int
    rows: int
    columns: list[str]
    encoding: str


class ImportCsvPreviewColumn(WireModel):
    name: str
    type: str
    format: str | None = None


class ImportCsvPreviewResponse(WireModel):
    encoding: str
    delimiter: str
    decimal_separator: str
    row_count: int
    columns: list[ImportCsvPreviewColumn]
    preview_rows: list[dict[str, JsonValue]]
    truncated: bool


class ImportXlsxResponse(WireModel):
    sheet_id: int
    rows: int
    columns: list[str]


class ImportPdfResponse(WireModel):
    sheet_id: int
    rows: int
    pages: int
    columns: list[str]


class ImportFilesResponse(WireModel):
    sheet_id: int
    rows: int


# The legacy bridge can return a normal ``{"detail": ...}`` route refusal at
# 400/500 or a bare v1 ActionResult at its non-success action statuses.
IMPORT_URLS_ERROR_RESPONSES = {
    400: {"model": HttpError | ActionResult},
    402: {"model": ActionResult},
    409: {"model": ActionResult},
    500: {"model": HttpError | ActionResult},
}


# The current multipart CSV transport has the same ActionResult failures as
# URL import, plus normal route refusals before the action boundary.
IMPORT_CSV_ERROR_RESPONSES = {
    400: {"model": HttpError | ActionResult},
    402: {"model": ActionResult},
    409: {"model": ActionResult},
    500: {"model": HttpError | ActionResult},
}


# XLSX keeps the CSV transport's route-refusal and action-error union.
IMPORT_XLSX_ERROR_RESPONSES = IMPORT_CSV_ERROR_RESPONSES


# PDF and generic-files failures cross the action boundary at 400; unlike the
# CSV/XLSX transports they must not overclaim an HttpError shape for that status.
IMPORT_PDF_ERROR_RESPONSES = {
    400: {"model": ActionResult},
    402: {"model": ActionResult},
    409: {"model": ActionResult},
    500: {"model": HttpError | ActionResult},
}
IMPORT_FILES_ERROR_RESPONSES = IMPORT_PDF_ERROR_RESPONSES


__all__ = [
    "IMPORT_CSV_ERROR_RESPONSES",
    "IMPORT_FILES_ERROR_RESPONSES",
    "IMPORT_PDF_ERROR_RESPONSES",
    "IMPORT_URLS_ERROR_RESPONSES",
    "IMPORT_XLSX_ERROR_RESPONSES",
    "ImportCsvResponse",
    "ImportCsvPreviewColumn",
    "ImportCsvPreviewResponse",
    "ImportFilesResponse",
    "ImportPasteDraftBody",
    "ImportPasteConfirmBody",
    "ImportPasteConfirmResponse",
    "ImportUpdatePreviewBody",
    "ImportUpdatePreviewResponse",
    "ImportPasteDraftColumn",
    "ImportPasteDraftResponse",
    "ImportPasteDraftSource",
    "ImportUrlsBody",
    "ImportUrlsResponse",
    "ImportPdfResponse",
    "ImportXlsxResponse",
    "SampleProjectSeedResponse",
]
