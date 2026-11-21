"""Typed feed metadata and polling actions."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from frisket.actions.core import ActionCategory, action
from frisket.actions.types import (
    ActionParams,
    CheckedFeed,
    DeletedFeed,
    FeedCreator,
    FeedChecker,
    FeedDeleter,
    FeedPatch,
    FeedRecord,
    FeedUpdater,
    FeedCreateIntent,
    FeedPoller,
    FeedPollSelector,
    PolledFeed,
    UpdatedFeed,
)


FeedCreateParams = FeedCreateIntent


class FeedPollParams(ActionParams):
    feed: FeedPollSelector


def poll_feed(params: FeedPollParams, feeds: FeedPoller) -> PolledFeed:
    return feeds.poll(params.feed)


class FeedUpdateParams(ActionParams):
    feed_id: int = Field(gt=0, strict=True)
    patch: FeedPatch


class FeedDeleteParams(ActionParams):
    feed_id: int = Field(gt=0, strict=True)


class FeedCheckParams(ActionParams):
    feed_id: int = Field(gt=0, strict=True)
    new_rows: int = Field(default=0, ge=0, strict=True)
    status: Literal["ok", "error"] = "ok"
    error: str | None = None
    cursor: str | None = None


def create_feed(params: FeedCreateParams, feeds: FeedCreator) -> FeedRecord:
    return feeds.create(
        name=params.name,
        kind=params.kind,
        url=params.url,
        config=params.config,
        sheet_id=params.sheet_id,
        schedule=params.schedule,
        enabled=params.enabled,
    )


def update_feed(params: FeedUpdateParams, feeds: FeedUpdater) -> UpdatedFeed:
    return feeds.update(params.feed_id, patch=params.patch)


def delete_feed(params: FeedDeleteParams, feeds: FeedDeleter) -> DeletedFeed:
    return feeds.delete(params.feed_id)


def check_feed(params: FeedCheckParams, feeds: FeedChecker) -> CheckedFeed:
    return feeds.check(
        params.feed_id,
        new_rows=params.new_rows,
        status=params.status,
        error=params.error,
        cursor=params.cursor,
    )


CREATE = action(
    examples=(
        FeedCreateParams(
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
    form="feed_create",
)

POLL = action(
    examples=(
        FeedPollParams(feed=1),
        FeedPollParams(
            feed=FeedCreateIntent(
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
    form="feed_poll",
)

UPDATE = action(
    examples=(FeedUpdateParams(feed_id=1, patch=FeedPatch(enabled=False)),),
    name="update",
    title="Update feed",
    description="Update selected fields on an existing feed.",
    category=ActionCategory.SOURCES,
    run=update_feed,
    form="feed_update",
)

DELETE = action(
    examples=(FeedDeleteParams(feed_id=1),),
    name="delete",
    title="Delete feed",
    description="Delete a feed and its check history.",
    category=ActionCategory.SOURCES,
    run=delete_feed,
    form="feed_delete",
)

CHECK = action(
    examples=(FeedCheckParams(feed_id=1, new_rows=3, cursor="page:2"),),
    name="check",
    title="Record feed check",
    description=(
        "Record an externally observed non-RSS feed check, update the feed's "
        "status, and write a receipt. RSS feeds are checked by feed.poll."
    ),
    category=ActionCategory.SOURCES,
    run=check_feed,
    form="feed_check",
)
