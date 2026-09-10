"""Acquire actual URL arguments into host-owned row file outputs."""

from pydantic import BaseModel

from frisket.actions.core import ActionCategory, action, map_rows
from frisket.actions.file_types import FileFetcher, UrlColumn
from frisket.actions.types import ActionParams, Row, RowResult, StagedFile


class FetchParams(ActionParams):
    source: UrlColumn


class FetchOutput(BaseModel):
    media: StagedFile


async def fetch_file(
    params: FetchParams, row: Row, fetcher: FileFetcher
) -> RowResult[FetchOutput]:
    return RowResult(
        output=FetchOutput(media=await fetcher.fetch(params.source.read(row)))
    )


FETCH_URL = action(
    name="fetch_url",
    title="Download file",
    description="Download a URL into a blob-backed file; enabled media plugins retain their native downloader.",
    category=ActionCategory.SOURCES,
    examples=(FetchParams(source="url"),),
    run=map_rows(fetch_file),
)
