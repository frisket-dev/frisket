"""Typed source metadata actions."""

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


def poll_source(params: SourcePollParams, sources: SourcePoller) -> PolledSource:
    return sources.poll(params.source)


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


def create_source(params: SourceCreateParams, sources: SourceCreator) -> SourceRecord:
    return sources.create(
        name=params.name,
        kind=params.kind,
        url=params.url,
        config=params.config,
        sheet_id=params.sheet_id,
        schedule=params.schedule,
        enabled=params.enabled,
    )


def update_source(params: SourceUpdateParams, sources: SourceUpdater) -> UpdatedSource:
    return sources.update(params.source_id, patch=params.patch)


def delete_source(params: SourceDeleteParams, sources: SourceDeleter) -> DeletedSource:
    return sources.delete(params.source_id)


def check_source(params: SourceCheckParams, sources: SourceChecker) -> CheckedSource:
    return sources.check(
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
    title="Create source",
    description=(
        "Create source metadata for later polling without fetching external content."
    ),
    category=ActionCategory.SOURCES,
    run=create_source,
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
    title="Poll source",
    description=(
        "Poll a registered source, deduplicate new and revised items, and record "
        "source-run, cursor and provider evidence."
    ),
    category=ActionCategory.SOURCES,
    run=poll_source,
    form="source_poll",
)

UPDATE = action(
    examples=(SourceUpdateParams(source_id=1, patch=SourcePatch(enabled=False)),),
    name="update",
    title="Update source",
    description="Update selected fields on an existing source.",
    category=ActionCategory.SOURCES,
    run=update_source,
    form="source_update",
)

DELETE = action(
    examples=(SourceDeleteParams(source_id=1),),
    name="delete",
    title="Delete source",
    description="Delete a source and its source-run history.",
    category=ActionCategory.SOURCES,
    run=delete_source,
    form="source_delete",
)

CHECK = action(
    examples=(SourceCheckParams(source_id=1, new_rows=3, cursor="page:2"),),
    name="check",
    title="Record source check",
    description=(
        "Record an externally observed non-RSS source check, update source "
        "monitoring state, and write source-run receipt evidence. RSS feed "
        "polling is owned by source.poll."
    ),
    category=ActionCategory.SOURCES,
    run=check_source,
    form="source_check",
)
