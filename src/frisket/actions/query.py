"""Typed, receipt-writing query preview action."""

from __future__ import annotations

from typing import Any

from pydantic import Field

from frisket.actions.core import ActionCategory, action
from frisket.actions.types import (
    ActionParams,
    QueryPreviewResult,
    QueryPreviewer,
)


class QueryPreviewParams(ActionParams):
    query: dict[str, Any]
    limit: int = Field(default=50, ge=0, le=500, strict=True)
    offset: int = Field(default=0, ge=0, strict=True)


def preview_query(
    params: QueryPreviewParams, previews: QueryPreviewer
) -> QueryPreviewResult:
    return previews.preview(
        query=params.query,
        limit=params.limit,
        offset=params.offset,
    )


PREVIEW = action(
    examples=(
        QueryPreviewParams(
            query={
                "schema_version": "frisket.query.v1",
                "kind": "sheet.filter",
                "scope": {"kind": "sheet", "sheet_id": 1},
                "filter": {"status": {"eq": "open"}},
            },
            limit=10,
        ),
    ),
    name="preview",
    title="Preview query rows",
    description=(
        "Resolve sheet filters, stored-vector similarity, local manual-text "
        "similarity, or local hybrid search and preserve the rowset in a receipt."
    ),
    category=ActionCategory.SOURCES,
    run=preview_query,
    form="query_preview",
)
