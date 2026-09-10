"""URL import intent; acquisition and atomic publication belong to the host."""

from __future__ import annotations

from pydantic import Field, StrictStr, field_validator

from frisket.actions.core import ActionCategory, action, create_sheet
from frisket.actions.types import ActionParams, DynamicTableResult
from frisket.actions.url_import_types import UrlImporter


class ImportUrlsParams(ActionParams):
    urls: list[StrictStr] = Field(min_length=1)

    @field_validator("urls")
    @classmethod
    def _trim_urls(cls, urls: list[str]) -> list[str]:
        return [url.strip() for url in urls]


def import_urls(params: ImportUrlsParams, importer: UrlImporter) -> DynamicTableResult:
    return importer.read(params.urls)


URLS = action(
    name="urls",
    title="Import URLs",
    description="Download URLs into a new sheet, retaining per-URL failures.",
    category=ActionCategory.CONVERT,
    run=create_sheet(import_urls),
    examples=(ImportUrlsParams(urls=["https://example.com/report.pdf"]),),
)
