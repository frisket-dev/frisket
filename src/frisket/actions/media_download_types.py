"""Actual-argument media acquisition and its finite optional outputs."""

from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from frisket.actions.types import (
    Outcome,
    StagedAudio,
    StagedFile,
    StagedImage,
    StagedVideo,
)


class DownloadedFiles(BaseModel):
    model_config = ConfigDict(extra="forbid")

    audio: StagedAudio | None = None
    video: StagedVideo | None = None
    thumbnail: Outcome[StagedImage | None] = Field(
        default_factory=lambda: Outcome.ok(None)
    )
    subtitles: Outcome[StagedFile | None] = Field(
        default_factory=lambda: Outcome.ok(None)
    )
    subtitles_text: Outcome[str | None] = Field(
        default_factory=lambda: Outcome.ok(None)
    )
    info: Outcome[StagedFile | None] = Field(default_factory=lambda: Outcome.ok(None))


def download_outputs(media_type: str, extra_opts: dict | None):
    options = extra_opts or {}
    outputs = [media_type]
    if options.get("writethumbnail"):
        outputs.append("thumbnail")
    if any(
        options.get(key)
        for key in ("writesubtitles", "writeautomaticsub", "allsubtitles")
    ):
        outputs.extend(("subtitles", "subtitles_text"))
    if options.get("writeinfojson"):
        outputs.append("info")
    return tuple(outputs)


class MediaDownloader(Protocol):
    async def download(
        self,
        url: str,
        *,
        media_type: Literal["audio", "video"] = "video",
        format_selector: str | None = None,
        extra_opts: dict[str, Any] | None = None,
    ) -> DownloadedFiles: ...
