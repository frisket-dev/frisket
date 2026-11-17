"""HTTP contracts for onboarding seed, upload, and URL-preparation routes.

The seed and successful upload responses are fixed route products. Upload
imports also retain their existing v1 ``ActionResult`` failure transport.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from frisket.contracts.action import ActionResult
from frisket.contracts.http.models import HttpError, WireModel
from frisket.contracts.http.pdf_imports import PdfImportStatus


class PrepareImportUrlsBody(BaseModel):
    urls: list[str]


class PrepareImportUrlsResponse(WireModel):
    kind: Literal["pdf_import"]
    draft: PdfImportStatus
    downloaded: int = Field(ge=0)
    failed: int = Field(ge=0)
    warnings: list[str]


class SampleProjectSeedResponse(WireModel):
    ok: bool
    project_id: str
    sheet_id: int
    sheet_name: str
    blank_column: str
    rows: int


class ImportCsvResponse(WireModel):
    sheet_id: int
    rows: int
    columns: list[str]
    encoding: str


class ImportXlsxResponse(WireModel):
    sheet_id: int
    rows: int
    columns: list[str]


class ImportFilesResponse(WireModel):
    sheet_id: int
    rows: int
    search_preparation_batch_id: str | None = None


# The multipart CSV transport returns a normal ``{"detail": ...}`` route
# refusal at 400/500 or a bare v1 ActionResult at its non-success action
# statuses.
IMPORT_CSV_ERROR_RESPONSES = {
    400: {"model": HttpError | ActionResult},
    402: {"model": ActionResult},
    409: {"model": ActionResult},
    500: {"model": HttpError | ActionResult},
}


# XLSX keeps the CSV transport's route-refusal and action-error union.
IMPORT_XLSX_ERROR_RESPONSES = IMPORT_CSV_ERROR_RESPONSES


# Generic-files failures cross the action boundary at 400; unlike the CSV/XLSX
# transports they must not overclaim an HttpError shape for that status.
IMPORT_FILES_ERROR_RESPONSES = {
    400: {"model": ActionResult},
    402: {"model": ActionResult},
    409: {"model": ActionResult},
    500: {"model": HttpError | ActionResult},
}


__all__ = [
    "IMPORT_CSV_ERROR_RESPONSES",
    "IMPORT_FILES_ERROR_RESPONSES",
    "IMPORT_XLSX_ERROR_RESPONSES",
    "ImportCsvResponse",
    "ImportFilesResponse",
    "PrepareImportUrlsBody",
    "PrepareImportUrlsResponse",
    "ImportXlsxResponse",
    "SampleProjectSeedResponse",
]
