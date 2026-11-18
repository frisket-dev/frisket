"""Typed feed metadata actions (action ids stay ``source.*``)."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from frisket.actions.core import ActionCategory, action
from frisket.actions.types import (
    ActionParams,
    CheckedSource,
    DeletedSource,
    SourceCreator,
    SourceChecker,
    SourceDeleter,
    SourcePatch,
    SourceRecord,
    SourceUpdater,
    SourceCreateIntent,
    SourcePoller,
    SourcePollSelector,
    PolledSource,
    UpdatedSource,
)


SourceCreateParams = SourceCreateIntent


class SourcePollParams(ActionParams):
    source: SourcePollSelector


def poll_feed(params: SourcePollParams, feeds: SourcePoller) -> PolledSource:
    return feeds.poll(params.source)


class SourceUpdateParams(ActionParams):
    source_id: int = Field(gt=0, strict=True)
    patch: SourcePatch


class SourceDeleteParams(ActionParams):
    source_id: int = Field(gt=0, strict=True)


class SourceCheckParams(ActionParams):
    source_id: int = Field(gt=0, strict=True)
    new_rows: int = Field(default=0, ge=0, strict=True)
    status: Literal["ok", "error"] = "ok"
    error: str | None = None
    cursor: str | None = None


def create_feed(params: SourceCreateParams, feeds: SourceCreator) -> SourceRecord:
    return feeds.create(
        name=params.name,
        kind=params.kind,
        url=params.url,
        config=params.config,
        sheet_id=params.sheet_id,
        schedule=params.schedule,
        enabled=params.enabled,
    )


def update_feed(params: SourceUpdateParams, feeds: SourceUpdater) -> UpdatedSource:
    return feeds.update(params.source_id, patch=params.patch)


def delete_feed(params: SourceDeleteParams, feeds: SourceDeleter) -> DeletedSource:
    return feeds.delete(params.source_id)


def check_feed(params: SourceCheckParams, feeds: SourceChecker) -> CheckedSource:
    return feeds.check(
        params.source_id,
        new_rows=params.new_rows,
        status=params.status,
        error=params.error,
        cursor=params.cursor,
    )


CREATE = action(
    examples=(
        SourceCreateParams(
            name="Example feed", kind="rss", url="https://example.org/feed.xml"
        ),
    ),
    name="create",
    title="Create feed",
    description=(
        "Create feed metadata for later checks without fetching external content."
    ),
    category=ActionCategory.SOURCES,
    run=create_feed,
    form="source_create",
)

POLL = action(
    examples=(
        SourcePollParams(source=1),
        SourcePollParams(
            source=SourceCreateIntent(
                name="Example feed", kind="rss", url="https://example.org/feed.xml"
            )
        ),
    ),
    name="poll",
    title="Check feed",
    description=(
        "Check a registered feed, deduplicate new and revised items, and record "
        "the check, cursor and provider in a receipt."
    ),
    category=ActionCategory.SOURCES,
    run=poll_feed,
    form="source_poll",
)

UPDATE = action(
    examples=(SourceUpdateParams(source_id=1, patch=SourcePatch(enabled=False)),),
    name="update",
    title="Update feed",
    description="Update selected fields on an existing feed.",
    category=ActionCategory.SOURCES,
    run=update_feed,
    form="source_update",
)

DELETE = action(
    examples=(SourceDeleteParams(source_id=1),),
    name="delete",
    title="Delete feed",
    description="Delete a feed and its check history.",
    category=ActionCategory.SOURCES,
    run=delete_feed,
    form="source_delete",
)

CHECK = action(
    examples=(SourceCheckParams(source_id=1, new_rows=3, cursor="page:2"),),
    name="check",
    title="Record feed check",
    description=(
        "Record an externally observed non-RSS feed check, update the feed's "
        "status, and write a receipt. RSS feeds are checked by source.poll."
    ),
    category=ActionCategory.SOURCES,
    run=check_feed,
    form="source_check",
)
