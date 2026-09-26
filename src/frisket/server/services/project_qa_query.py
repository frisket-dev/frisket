"""Pure canonical query evaluation used by Ask tools and saved query sources."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from frisket.features.watchlists.specs import canonical_json, normalize_query_spec
from frisket.querysets import (
    SheetRowSetError,
    anchor_relative_date_filters,
    count_sheet_filter_values,
    resolve_sheet_filter_rows,
)
from frisket.server.services.project_qa_analytics import (
    AnalyticsCancelled,
    AnalyticsGroup,
    AnalyticsHaving,
    AnalyticsMetric,
    AnalyticsRequest,
    AnalyticsRequestError,
    AnalyticsSort,
    NumericOverflowError,
    evaluate_analytics,
)

__all__ = [
    "AnalyticsCancelled",
    "AnalyticsGroup",
    "AnalyticsHaving",
    "AnalyticsMetric",
    "AnalyticsRequest",
    "AnalyticsRequestError",
    "AnalyticsSort",
    "NumericOverflowError",
    "evaluate_analytics",
    "evaluate_query",
]


MAX_COUNT_BY_GROUPS = 100


def evaluate_query(
    project: Any,
    query: Mapping[str, Any],
    scope: Mapping[str, Any],
    *,
    limit: int = 50,
    offset: int = 0,
    count_by: int | None = None,
) -> dict[str, Any]:
    """Evaluate a saved ``frisket.query.v1`` against its explicit scope.

    This reader does no persistence and is deliberately usable by citation
    resolution.  It applies the selection before both result limiting and
    aggregation, so ``total`` and ``count_by`` are exact for the saved scope.
    """

    normalized = normalize_query_spec(dict(query), allow_row_cell=False)
    if normalized.get("kind") != "sheet.filter":
        raise ValueError("only canonical sheet filters are available")
    query_scope = normalized.get("scope")
    if not isinstance(query_scope, Mapping) or not isinstance(
        query_scope.get("sheet_id"), int
    ):
        raise ValueError("query scope must name a sheet")
    sheet_id = int(query_scope["sheet_id"])
    effective = _normalize_scope(scope, sheet_id)
    normalized["filter"] = anchor_relative_date_filters(
        normalized.get("filter", {}), reference_date=datetime.now(UTC).date()
    )
    _validate_file_query_columns(project, sheet_id, normalized, effective)
    filter_ = canonical_json(normalized.get("filter", {}))
    sort = canonical_json(normalized["sort"]) if "sort" in normalized else None
    try:
        rowset = resolve_sheet_filter_rows(
            project,
            sheet_id,
            filter_=filter_,
            sort=sort,
            limit=limit,
            offset=offset,
            row_ids=effective.get("row_ids"),
        )
    except SheetRowSetError as exc:
        raise ValueError(str(exc)) from exc
    result: dict[str, Any] = {
        "sheet_id": sheet_id,
        "query": normalized,
        "scope": effective,
        "row_ids": rowset.row_ids,
        "total": rowset.total,
    }
    if count_by is not None:
        if isinstance(count_by, bool) or not isinstance(count_by, int):
            raise ValueError("count_by must be a column id")
        allowed_columns = effective.get("column_ids")
        if allowed_columns is not None and count_by not in allowed_columns:
            raise ValueError("file scope may count only the selected file column")
        values, complete = count_sheet_filter_values(
            project,
            sheet_id,
            count_by,
            filter_=filter_,
            sort=sort,
            row_ids=effective.get("row_ids"),
            limit=MAX_COUNT_BY_GROUPS,
        )
        result["count_by"] = {
            "column_id": count_by,
            "values": [{"value": value, "count": count} for value, count in values],
            "complete": complete,
        }
    return result


def _normalize_scope(scope: Mapping[str, Any], sheet_id: int) -> dict[str, Any]:
    """Close a replayable query constraint without consulting today's composer."""

    if scope.get("kind") == "sheet" and scope.get("sheet_id") == sheet_id:
        return {"kind": "sheet", "sheet_id": sheet_id}
    if scope.get("kind") != "rows" or scope.get("sheet_id") != sheet_id:
        raise ValueError("saved query scope does not match its sheet")
    row_ids = scope.get("row_ids")
    if not isinstance(row_ids, list) or not all(
        isinstance(row_id, int) and not isinstance(row_id, bool) for row_id in row_ids
    ):
        raise ValueError("saved query scope has invalid rows")
    result: dict[str, Any] = {
        "kind": "rows",
        "sheet_id": sheet_id,
        "row_ids": sorted(set(row_ids)),
    }
    column_ids = scope.get("column_ids")
    if column_ids is not None:
        if not isinstance(column_ids, list) or not all(
            isinstance(column_id, int) and not isinstance(column_id, bool)
            for column_id in column_ids
        ):
            raise ValueError("saved query scope has invalid file columns")
        result["column_ids"] = sorted(set(column_ids))
    return result


def _validate_file_query_columns(
    project: Any,
    sheet_id: int,
    query: Mapping[str, Any],
    scope: Mapping[str, Any],
) -> None:
    """A file selection may inspect only the selected cell's column."""

    column_ids = scope.get("column_ids")
    if column_ids is None:
        return
    by_name = {
        str(column["name"]): int(column["id"]) for column in project.columns(sheet_id)
    }
    filter_ = query.get("filter", {})
    filter_columns = set(filter_) if isinstance(filter_, Mapping) else set()
    sort = query.get("sort")
    if isinstance(sort, list):
        filter_columns.update(
            str(item.get("column"))
            for item in sort
            if isinstance(item, Mapping) and isinstance(item.get("column"), str)
        )
    used = {by_name.get(name) for name in filter_columns}
    if None in used or not used <= set(column_ids):
        raise ValueError("file scope may filter only the selected file column")
