"""Small helpers for public offset-style page envelopes."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Query

PageOffset = Annotated[int, Query(ge=0)]
PageLimit100 = Annotated[int, Query(ge=1, le=100)]
PageLimit500 = Annotated[int, Query(ge=1, le=500)]


def offset_page_meta(
    *,
    offset: int,
    limit: int,
    total: int,
    item_count: int,
    schema_version: str | None = None,
    order: str | None = None,
) -> dict[str, Any]:
    next_offset = offset + item_count
    has_more = next_offset < total
    payload: dict[str, Any] = {}
    if schema_version is not None:
        payload["schema_version"] = schema_version
    if order is not None:
        payload["order"] = order
    payload.update(
        {
            "offset": offset,
            "limit": limit,
            "total": total,
            "has_more": has_more,
            "next_offset": next_offset if has_more else None,
        }
    )
    return payload


def offset_page_payload(
    schema_version: str,
    *,
    items: list[Any],
    item_key: str,
    offset: int,
    limit: int,
    total: int,
    order: str | None = None,
) -> dict[str, Any]:
    return {
        **offset_page_meta(
            schema_version=schema_version,
            order=order,
            offset=offset,
            limit=limit,
            total=total,
            item_count=len(items),
        ),
        item_key: items,
    }
