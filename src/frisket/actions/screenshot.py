"""Capture one browser-rendered PNG from each admitted URL row."""

from typing import ClassVar

from pydantic import BaseModel, Field

from frisket.actions.core import ActionCategory, action, map_rows
from frisket.actions.screenshot_types import Screenshotter
from frisket.actions.types import ActionParams, ColumnRef, Row, RowResult, StagedImage


class UrlColumn(ColumnRef[str]):
    accepted_column_types: ClassVar[tuple[str, ...]] = ("text", "link")


class ScreenshotParams(ActionParams):
    source: UrlColumn
    full_page: bool = Field(default=True, strict=True)
    viewport_width: int = Field(default=1280, ge=1, le=4096, strict=True)
    viewport_height: int = Field(default=720, ge=1, le=4096, strict=True)
    max_bytes: int = Field(default=5_000_000, ge=1, le=50_000_000, strict=True)
    timeout_ms: int = Field(default=30_000, ge=1, le=300_000, strict=True)


class ScreenshotOutput(BaseModel):
    screenshot: StagedImage


async def capture_screenshot(
    params: ScreenshotParams, row: Row, screenshotter: Screenshotter
) -> RowResult[ScreenshotOutput]:
    image = await screenshotter.capture(
        params.source.read(row),
        full_page=params.full_page,
        viewport=(params.viewport_width, params.viewport_height),
        max_bytes=params.max_bytes,
        timeout_ms=params.timeout_ms,
    )
    return RowResult(output=ScreenshotOutput(screenshot=image))


CAPTURE_SCREENSHOT = action(
    name="capture_screenshot",
    title="Capture web screenshot",
    description="Render link or text URL cells in Playwright and write one PNG image cell per row.",
    category=ActionCategory.SOURCES,
    run=map_rows(capture_screenshot),
    examples=(ScreenshotParams(source="source_url"),),
)
