"""Capture page files or extract links using one admitted capture operation."""

from pydantic import Field

from frisket.actions.core import ActionCategory, action
from frisket.actions.file_types import UrlColumn
from frisket.actions.page_capture_types import (
    CaptureOptions,
    PageCapturer,
    PreparedPageCapture,
)


class CapturePageParams(CaptureOptions):
    source: UrlColumn = Field(description="Choose a column of HTTP(S) page URLs.")


def capture_page(
    params: CapturePageParams, capture: PageCapturer
) -> PreparedPageCapture:
    return capture.prepare(params.source, options=params)


CAPTURE_PAGE = action(
    name="capture_page",
    title="Capture web page",
    description="Save HTML page artifacts or extract links from selected URL cells.",
    category=ActionCategory.SOURCES,
    run=capture_page,
    examples=(CapturePageParams(source="source_url"),),
)
