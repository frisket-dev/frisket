"""Download media with yt-dlp using the ordinary typed row host."""

from typing import Any, Literal

from pydantic import Field, field_validator

from frisket.actions.core import ActionCategory, action, map_rows
from frisket.actions.file_types import UrlColumn
from frisket.actions.media_download_types import (
    DownloadedFiles,
    MediaDownloader,
    download_outputs,
)
from frisket.actions.types import ActionParams, Row, RowResult


class MediaDownloadParams(ActionParams):
    source: UrlColumn = Field(description="Choose a column of HTTP(S) media URLs.")
    media_type: Literal["audio", "video"] = "video"
    format_selector: str | None = Field(default=None, strict=True, min_length=1)
    extra_opts: dict[str, Any] | None = None

    @field_validator("extra_opts")
    @classmethod
    def _options(cls, value):
        if value is not None:
            from frisket.ops.ytdlp import validate_extra_opts

            validate_extra_opts(value)
        return value

    @field_validator("format_selector")
    @classmethod
    def _format(cls, value):
        if value is not None and not value.strip():
            raise ValueError("format_selector must not be blank")
        return value


async def download_media(
    params: MediaDownloadParams, row: Row, downloader: MediaDownloader
) -> RowResult[DownloadedFiles]:
    return RowResult(
        output=await downloader.download(
            params.source.read(row),
            media_type=params.media_type,
            format_selector=params.format_selector,
            extra_opts=params.extra_opts,
        )
    )


YTDLP_DOWNLOAD = action(
    name="ytdlp_download",
    title="Download media",
    description="Download audio or video and optional subtitles, thumbnails, and metadata with yt-dlp.",
    category=ActionCategory.SOURCES,
    examples=(MediaDownloadParams(source="url"),),
    run=map_rows(
        download_media,
        active_outputs=lambda params: download_outputs(
            params.media_type, params.extra_opts
        ),
    ),
)
