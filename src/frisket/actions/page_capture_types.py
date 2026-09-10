"""Page capture options and its invocation-owned, read-only preparation."""

from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import Field, model_validator

from frisket.actions.file_types import UrlColumn
from frisket.actions.types import ActionParams


class CaptureOptions(ActionParams):
    output_mode: Literal["page", "links"] = "page"
    render_mode: Literal["static", "playwright"] = "static"
    include_warc: bool = Field(default=False, strict=True)
    max_bytes: int = Field(default=5_000_000, ge=1, le=50_000_000, strict=True)
    timeout_ms: int = Field(default=30_000, ge=1, le=300_000, strict=True)

    @model_validator(mode="after")
    def _warc_mode(self):
        if self.include_warc and (
            self.output_mode != "page" or self.render_mode != "playwright"
        ):
            raise ValueError("WARC requires page output with browser rendering")
        return self


class CapturedPages(ActionParams):
    """Capture summary; artifact and project identities remain host-owned."""

    row_count: int
    status_counts: dict[str, int]


@dataclass(frozen=True, eq=False)
class PreparedPageCapture:
    """A preparation can only be executed by the host that issued it."""

    selected_row_count: int


class PageCapturer(Protocol):
    def prepare(
        self, source: UrlColumn, *, options: CaptureOptions
    ) -> PreparedPageCapture: ...
