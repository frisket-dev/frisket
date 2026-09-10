"""Expand one collection URL into a child sheet of external metadata."""

from pydantic import BaseModel, Field

from frisket.actions.core import ActionCategory, action, create_sheet
from frisket.actions.types import (
    ActionParams,
    CollectionReader,
    Link,
    TableResult,
    TableRow,
)


class CollectionExpandParams(ActionParams):
    source_sheet_id: int = Field(gt=0, strict=True)
    source_column_id: int = Field(gt=0, strict=True)
    source_row_id: int = Field(gt=0, strict=True)


class CollectionOutput(BaseModel):
    url: Link | None
    title: str | None
    video_id: str | None
    channel_title: str | None
    published_at: str | None
    position: int | None


def expand_collection(
    params: CollectionExpandParams, collections: CollectionReader
) -> TableResult[CollectionOutput]:
    items = collections.read(
        sheet_id=params.source_sheet_id,
        column_id=params.source_column_id,
        row_id=params.source_row_id,
    )
    return TableResult(
        rows=tuple(
            TableRow(
                output=CollectionOutput.model_validate(
                    {key: item.value.get(key) for key in CollectionOutput.model_fields}
                ),
                sources=(item.source,),
                parent=item.source,
            )
            for item in items
        )
    )


COLLECTION_EXPAND = action(
    name="collection_expand",
    title="Expand collection",
    description="Enumerate a channel or playlist URL into a child sheet of video metadata.",
    category=ActionCategory.CONVERT,
    run=create_sheet(expand_collection),
    examples=(
        CollectionExpandParams(source_sheet_id=1, source_column_id=1, source_row_id=1),
    ),
)
