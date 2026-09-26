"""Bounded, validity-aware SQL analytics used by Project Ask.

This deliberately is not a general query planner.  The request model closes
the small analytics vocabulary, while SQL owns the filtered population,
aggregation, ordering, and page boundary.
"""

from __future__ import annotations

import json
import math
import sqlite3
import sys
import threading
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from frisket.features.watchlists.specs import canonical_json
from frisket.querysets import (
    BUILTIN_FILTER_OPERATORS,
    SheetRowSetError,
    sheet_row_scope_query,
)


_NUMERIC_TYPES = frozenset({"integer", "number"})
_ORDERED_TYPES = frozenset(
    {"integer", "number", "text", "category", "link", "date", "boolean"}
)
_GROUPABLE_TYPES = _ORDERED_TYPES
_NUMERIC_METRICS = frozenset({"sum", "mean", "median"})
_HAVING_METRICS = frozenset(
    {"count", "value_count", "missing_count", "distinct_count", *_NUMERIC_METRICS}
)


class AnalyticsRequestError(ValueError):
    """A repairable closed-request validation failure."""


class NumericOverflowError(AnalyticsRequestError):
    """One non-finite/overflowed metric invalidates the whole calculation."""


class AnalyticsCancelled(AnalyticsRequestError):
    """The caller stopped a query while SQLite was evaluating it."""


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class AnalyticsMetric(_ClosedModel):
    id: str = Field(min_length=1, max_length=64)
    kind: Literal[
        "count",
        "value_count",
        "missing_count",
        "distinct_count",
        "sum",
        "mean",
        "median",
        "min",
        "max",
    ]
    column_id: int | None = Field(default=None, ge=1)
    percent_of_total: bool = False

    @model_validator(mode="after")
    def _validate_column(self) -> "AnalyticsMetric":
        if self.kind == "count":
            if self.column_id is not None:
                raise ValueError("count does not accept column_id")
        elif self.column_id is None:
            raise ValueError(f"{self.kind} requires column_id")
        if self.percent_of_total and self.kind not in {"count", "sum"}:
            raise ValueError("percent_of_total is available only for count and sum")
        return self


class AnalyticsGroup(_ClosedModel):
    column_id: int = Field(ge=1)
    bucket: Literal["day", "month", "year"] | None = None


class AnalyticsHaving(_ClosedModel):
    metric_id: str = Field(min_length=1, max_length=64)
    operator: Literal["eq", "neq", "gt", "gte", "lt", "lte"]
    value: int | float


class AnalyticsSort(_ClosedModel):
    kind: Literal["metric", "group"]
    metric_id: str | None = Field(default=None, min_length=1, max_length=64)
    group_index: int | None = Field(default=None, ge=0)
    direction: Literal["asc", "desc"] = "asc"

    @model_validator(mode="after")
    def _validate_target(self) -> "AnalyticsSort":
        if self.kind == "metric" and self.metric_id is None:
            raise ValueError("metric sort requires metric_id")
        if self.kind == "group" and self.group_index is None:
            raise ValueError("group sort requires group_index")
        if self.kind == "metric" and self.group_index is not None:
            raise ValueError("metric sort cannot include group_index")
        if self.kind == "group" and self.metric_id is not None:
            raise ValueError("group sort cannot include metric_id")
        return self


class AnalyticsRequest(_ClosedModel):
    sheet_id: int = Field(ge=1)
    filter: dict[str, Any] = Field(default_factory=dict)
    groups: list[AnalyticsGroup] = Field(default_factory=list, max_length=4)
    metrics: list[AnalyticsMetric] = Field(min_length=1, max_length=12)
    having: list[AnalyticsHaving] = Field(default_factory=list, max_length=8)
    sort: list[AnalyticsSort] = Field(default_factory=list, max_length=5)
    limit: int = Field(default=50, ge=1, le=100)
    offset: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _validate_references(self) -> "AnalyticsRequest":
        ids = [metric.id for metric in self.metrics]
        if len(ids) != len(set(ids)):
            raise ValueError("metric ids must be unique")
        group_columns = [group.column_id for group in self.groups]
        if len(group_columns) != len(set(group_columns)):
            raise ValueError("grouping columns must be unique")
        known = {metric.id: metric for metric in self.metrics}
        if any(item.metric_id not in known for item in self.having):
            raise ValueError("having references an unknown metric")
        if any(
            known[item.metric_id].kind not in _HAVING_METRICS for item in self.having
        ):
            raise ValueError("having requires a numeric metric")
        for item in self.sort:
            if item.kind == "metric" and item.metric_id not in known:
                raise ValueError("sort references an unknown metric")
            if (
                item.kind == "group"
                and item.group_index is not None
                and item.group_index >= len(self.groups)
            ):
                raise ValueError("sort references an unknown group")
        return self


def evaluate_analytics(
    project: Any,
    request: AnalyticsRequest | Mapping[str, Any],
    scope: Mapping[str, Any],
    *,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    """Evaluate closed SQL analytics inside one query-owned read snapshot."""
    parsed = (
        request
        if isinstance(request, AnalyticsRequest)
        else AnalyticsRequest.model_validate(request)
    )
    effective = _normalize_scope(scope, parsed.sheet_id)
    if cancel_event is not None and cancel_event.is_set():
        raise AnalyticsCancelled("analytics query was stopped")

    with project.read_snapshot() as snapshot:
        db = snapshot.db
        db.set_progress_handler(
            _cancel_progress(cancel_event) if cancel_event is not None else None, 1_000
        )
        try:
            return _evaluate(snapshot, parsed, effective)
        except sqlite3.OperationalError as exc:
            if cancel_event is not None and cancel_event.is_set():
                raise AnalyticsCancelled("analytics query was stopped") from exc
            if "overflow" in str(exc).lower():
                raise NumericOverflowError("numeric_overflow") from exc
            raise
        finally:
            db.set_progress_handler(None, 0)


def _cancel_progress(event: threading.Event):
    def cancelled() -> int:
        return int(event.is_set())

    return cancelled


def _evaluate(
    snapshot: Any, request: AnalyticsRequest, scope: dict[str, Any]
) -> dict[str, Any]:
    columns = {
        int(column["id"]): column for column in snapshot.columns(request.sheet_id)
    }
    _validate_columns(request, columns, scope)
    _validate_scope_filter_columns(request.filter, columns, scope)
    _validate_builtin_filter(request.filter)
    filter_json = canonical_json(request.filter)
    try:
        _cols, where_sql, where_params, _order, _order_params = sheet_row_scope_query(
            snapshot,
            request.sheet_id,
            filter_=filter_json,
            row_ids=scope.get("row_ids"),
        )
    except SheetRowSetError as exc:
        raise AnalyticsRequestError(str(exc)) from exc

    involved = _involved_columns(request)
    ctes, source_params = _base_ctes(
        where_sql, where_params, involved, columns, request
    )
    metric_sql, quality_sql, quality_columns = _aggregate_fields(request, columns)
    group_fields = _group_field_names(request)
    aggregate_fields = [
        *group_fields,
        "COUNT(*) AS row_count",
        *metric_sql,
        *quality_sql,
    ]
    group_by = f" GROUP BY {', '.join(group_fields)}" if group_fields else ""
    ctes.append(
        "aggregated AS (SELECT "
        + ", ".join(aggregate_fields)
        + " FROM prepared"
        + group_by
        + ")"
    )
    median_joins = _median_ctes(ctes, request, columns, group_fields)
    denominator_fields = _denominator_fields(request, columns)
    ctes.append(
        "denominators AS (SELECT " + ", ".join(denominator_fields) + " FROM prepared)"
    )

    select_fields = ["a.*", *median_joins[0], "d.*"]
    from_sql = "aggregated a " + median_joins[1] + " CROSS JOIN denominators d"
    having_sql, having_params = _having_sql(request)
    primary_metric = (
        request.sort[0].metric_id
        if request.sort and request.sort[0].kind == "metric"
        else None
    )
    excluded_sql = ""
    if primary_metric is not None:
        excluded_sql = f"({_metric_ref(request, primary_metric)} IS NULL)"
        having_sql = _and_sql(having_sql, f"NOT {excluded_sql}")
    order_sql = _order_sql(request, group_fields)
    query = (
        "WITH "
        + ", ".join(ctes)
        + " SELECT "
        + ", ".join(select_fields)
        + " FROM "
        + from_sql
        + (" WHERE " + having_sql if having_sql else "")
        + " ORDER BY "
        + order_sql
        + " LIMIT ? OFFSET ?"
    )
    params = [*source_params, *having_params, request.limit + 1, request.offset]
    rows = snapshot.db.execute(query, params).fetchall()
    denominator_row = snapshot.db.execute(
        "WITH " + ", ".join(ctes) + " SELECT d.* FROM denominators d",
        source_params,
    ).fetchone()
    excluded_null_groups = _excluded_null_groups(
        snapshot.db, ctes, source_params, from_sql, excluded_sql, request, having_params
    )
    groups = [
        _result_group(row, request, columns, quality_columns)
        for row in rows[: request.limit]
    ]
    _assert_query_finite(snapshot.db, ctes, source_params, from_sql, request)
    denominators = _denominators(denominator_row, request)
    _attach_percentages(groups, denominators, request)
    result = {
        "sheet_id": request.sheet_id,
        "scope": scope,
        "filter": request.filter,
        "groups": groups,
        "row_count": int(denominator_row["full_row_count"]),
        "has_more": len(rows) > request.limit,
        "excluded_null_groups": excluded_null_groups,
        "denominators": denominators,
        "source_op_cursor": snapshot.op_cursor,
    }
    if not request.groups and groups:
        result["quality"] = groups[0].pop("quality")
    return result


def _normalize_scope(scope: Mapping[str, Any], sheet_id: int) -> dict[str, Any]:
    if scope.get("kind") == "sheet" and scope.get("sheet_id") == sheet_id:
        return {"kind": "sheet", "sheet_id": sheet_id}
    if scope.get("kind") != "rows" or scope.get("sheet_id") != sheet_id:
        raise AnalyticsRequestError("analytics scope does not match its sheet")
    row_ids = scope.get("row_ids")
    if not isinstance(row_ids, list) or not all(
        type(row_id) is int for row_id in row_ids
    ):
        raise AnalyticsRequestError("analytics scope has invalid rows")
    result: dict[str, Any] = {
        "kind": "rows",
        "sheet_id": sheet_id,
        "row_ids": sorted(set(row_ids)),
    }
    column_ids = scope.get("column_ids")
    if column_ids is not None:
        if not isinstance(column_ids, list) or not all(
            type(column_id) is int for column_id in column_ids
        ):
            raise AnalyticsRequestError("analytics scope has invalid file columns")
        result["column_ids"] = sorted(set(column_ids))
    return result


def _validate_columns(
    request: AnalyticsRequest, columns: Mapping[int, Any], scope: Mapping[str, Any]
) -> None:
    allowed = scope.get("column_ids")
    used = _involved_columns(request)
    if any(column_id not in columns for column_id in used):
        raise AnalyticsRequestError("analytics references a column outside the sheet")
    if allowed is not None and not used <= set(allowed):
        raise AnalyticsRequestError(
            "file scope may inspect only the selected file column"
        )
    for metric in request.metrics:
        if metric.column_id is None:
            continue
        column_type = str(columns[metric.column_id]["type"])
        if metric.kind in _NUMERIC_METRICS and column_type not in _NUMERIC_TYPES:
            raise AnalyticsRequestError(f"{metric.kind} requires a numeric column")
        if metric.kind in {"min", "max"} and column_type not in _ORDERED_TYPES:
            raise AnalyticsRequestError(f"{metric.kind} requires an ordered column")
    for group in request.groups:
        if str(columns[group.column_id]["type"]) not in _GROUPABLE_TYPES:
            raise AnalyticsRequestError("grouping requires a scalar column")
        if group.bucket is not None and str(columns[group.column_id]["type"]) != "date":
            raise AnalyticsRequestError("date buckets require a date column")


def _validate_scope_filter_columns(
    filter_spec: Mapping[str, Any], columns: Mapping[int, Any], scope: Mapping[str, Any]
) -> None:
    allowed = scope.get("column_ids")
    if allowed is None:
        return
    by_name = {str(column["name"]): column_id for column_id, column in columns.items()}
    used = {by_name.get(name) for name in filter_spec}
    if None in used or not used <= set(allowed):
        raise AnalyticsRequestError(
            "file scope may filter only the selected file column"
        )


def _validate_builtin_filter(filter_spec: Mapping[str, Any]) -> None:
    for condition in filter_spec.values():
        if not isinstance(condition, Mapping):
            continue
        unsupported = set(condition) - (set(BUILTIN_FILTER_OPERATORS) - {"group_eq"})
        if unsupported:
            raise AnalyticsRequestError(
                "unsupported filter operator: " + ", ".join(sorted(unsupported))
            )


def _involved_columns(request: AnalyticsRequest) -> set[int]:
    return {group.column_id for group in request.groups} | {
        metric.column_id for metric in request.metrics if metric.column_id is not None
    }


def _base_ctes(
    where_sql: str,
    where_params: Sequence[Any],
    involved: set[int],
    columns: Mapping[int, Any],
    request: AnalyticsRequest,
) -> tuple[list[str], list[Any]]:
    ordered = sorted(involved)
    source = ["scoped.row_id"]
    joins: list[str] = []
    for index, column_id in enumerate(ordered):
        source.extend((f"c{index}.value AS v{index}", f"c{index}.validity AS q{index}"))
        joins.append(
            f"LEFT JOIN current_cells c{index} ON c{index}.row_id=scoped.row_id AND c{index}.column_id=?"
        )
    positions = {column_id: index for index, column_id in enumerate(ordered)}
    prepared = ["source.*"]
    for index, group in enumerate(request.groups):
        pos = positions[group.column_id]
        scalar = f"json_extract(v{pos}, '$')"
        date_value = f"frisket_utc_calendar_date({scalar})"
        is_date = str(columns[group.column_id]["type"]) == "date"
        invalid = f"q{pos}='invalid'" + (
            f" OR (q{pos}='valid' AND {date_value} IS NULL)" if is_date else ""
        )
        prepared.append(
            f"CASE WHEN {invalid} THEN 'invalid' WHEN q{pos}='valid' AND v{pos} IS NOT NULL THEN 'valid' ELSE 'missing' END AS g{index}_kind"
        )
        valid = f"q{pos}='valid'" + (
            f" AND {date_value} IS NOT NULL" if is_date else ""
        )
        if group.bucket is None:
            prepared.append(f"CASE WHEN {valid} THEN {scalar} END AS g{index}_value")
            prepared.append(f"CASE WHEN {valid} THEN v{pos} END AS g{index}_json")
        else:
            width = {"year": 4, "month": 7, "day": 10}[group.bucket]
            prepared.append(
                f"CASE WHEN {valid} THEN substr({date_value}, 1, {width}) END AS g{index}_value"
            )
            prepared.append("NULL AS g%d_json" % index)
    return [
        f"scoped AS (SELECT r.id AS row_id FROM rows r WHERE {where_sql})",
        "source AS (SELECT "
        + ", ".join(source)
        + " FROM scoped "
        + " ".join(joins)
        + ")",
        "prepared AS (SELECT " + ", ".join(prepared) + " FROM source)",
    ], [*where_params, *ordered]


def _group_field_names(request: AnalyticsRequest) -> list[str]:
    fields: list[str] = []
    for index, _group in enumerate(request.groups):
        fields.extend((f"g{index}_kind", f"g{index}_value", f"g{index}_json"))
    return fields


def _column_pos(request: AnalyticsRequest, column_id: int) -> int:
    return sorted(_involved_columns(request)).index(column_id)


def _valid_present(request: AnalyticsRequest, column_id: int) -> str:
    pos = _column_pos(request, column_id)
    return f"q{pos}='valid' AND v{pos} IS NOT NULL"


def _numeric_value(request: AnalyticsRequest, column_id: int) -> str:
    pos = _column_pos(request, column_id)
    return f"CASE WHEN {_valid_present(request, column_id)} THEN json_extract(v{pos}, '$') END"


def _value_expr(request: AnalyticsRequest, column_id: int) -> str:
    pos = _column_pos(request, column_id)
    return f"CASE WHEN {_valid_present(request, column_id)} THEN json_extract(v{pos}, '$') END"


def _metric_ref(request: AnalyticsRequest, metric_id: str) -> str:
    index = next(
        index for index, metric in enumerate(request.metrics) if metric.id == metric_id
    )
    return (
        f"m{index}.value" if request.metrics[index].kind == "median" else f"a.m{index}"
    )


def _aggregate_fields(
    request: AnalyticsRequest, columns: Mapping[int, Any]
) -> tuple[list[str], list[str], list[int]]:
    metric_fields: list[str] = []
    for index, metric in enumerate(request.metrics):
        alias = f"m{index}"
        if metric.kind == "count":
            metric_fields.append(f"COUNT(*) AS {alias}")
        elif metric.kind == "value_count":
            metric_fields.append(
                f"COALESCE(SUM({_valid_present(request, metric.column_id)}), 0) AS {alias}"
            )
        elif metric.kind == "missing_count":
            pos = _column_pos(request, metric.column_id)
            metric_fields.append(
                f"COALESCE(SUM(q{pos}='missing' OR q{pos} IS NULL), 0) AS {alias}"
            )
        elif metric.kind == "distinct_count":
            pos = _column_pos(request, metric.column_id)
            metric_fields.append(
                f"COUNT(DISTINCT CASE WHEN {_valid_present(request, metric.column_id)} THEN v{pos} END) AS {alias}"
            )
        elif metric.kind == "sum":
            metric_fields.append(
                f"SUM({_numeric_value(request, metric.column_id)}) AS {alias}"
            )
        elif metric.kind == "mean":
            metric_fields.append(
                f"AVG({_numeric_value(request, metric.column_id)}) AS {alias}"
            )
        elif metric.kind == "median":
            # Filled from its window CTE below.
            continue
        elif metric.kind in {"min", "max"}:
            metric_fields.append(
                f"{metric.kind.upper()}({_value_expr(request, metric.column_id)}) AS {alias}"
            )
        else:  # pragma: no cover - Literal/model validation closes this.
            raise AssertionError(metric.kind)
    quality_columns = sorted(
        {metric.column_id for metric in request.metrics if metric.column_id is not None}
    )
    quality_fields: list[str] = []
    for column_id in quality_columns:
        pos = _column_pos(request, column_id)
        quality_fields.extend(
            (
                f"COALESCE(SUM({_valid_present(request, column_id)}), 0) AS q{pos}_present",
                f"COALESCE(SUM(q{pos}='missing' OR q{pos} IS NULL), 0) AS q{pos}_missing",
                f"COALESCE(SUM(q{pos}='invalid'), 0) AS q{pos}_invalid",
            )
        )
    return metric_fields, quality_fields, quality_columns


def _median_ctes(
    ctes: list[str],
    request: AnalyticsRequest,
    columns: Mapping[int, Any],
    group_fields: list[str],
) -> tuple[list[str], str]:
    del columns
    selected: list[str] = []
    joins: list[str] = []
    partitions = ", ".join(group_fields)
    for index, metric in enumerate(request.metrics):
        if metric.kind != "median":
            continue
        numeric = _numeric_value(request, metric.column_id)
        partition = f"PARTITION BY {partitions} " if partitions else ""
        group_select = ", ".join(group_fields)
        comma = ", " if group_select else ""
        ctes.append(
            f"median_source_{index} AS (SELECT {group_select}{comma}{numeric} AS value, "
            f"ROW_NUMBER() OVER ({partition}ORDER BY {numeric}) AS rn, "
            f"COUNT(*) OVER ({partition}) AS n FROM prepared WHERE {numeric} IS NOT NULL)"
        )
        ctes.append(
            f"median_{index} AS (SELECT {group_select}{comma}CASE WHEN MAX(n) % 2 = 1 "
            "THEN MAX(CASE WHEN rn=(n + 1) / 2 THEN value END) "
            "ELSE MAX(CASE WHEN rn=n / 2 THEN value / 2.0 END) + "
            "MAX(CASE WHEN rn=n / 2 + 1 THEN value / 2.0 END) END AS value "
            f"FROM median_source_{index}"
            + (f" GROUP BY {group_select}" if group_select else "")
            + ")"
        )
        selected.append(f"m{index}.value AS m{index}")
        join_condition = (
            " AND ".join(f"a.{field} IS m{index}.{field}" for field in group_fields)
            or "1=1"
        )
        joins.append(f" LEFT JOIN median_{index} m{index} ON {join_condition}")
    return selected, "".join(joins)


def _denominator_fields(
    request: AnalyticsRequest, columns: Mapping[int, Any]
) -> list[str]:
    del columns
    fields = ["COUNT(*) AS full_row_count"]
    for index, metric in enumerate(request.metrics):
        if not metric.percent_of_total:
            continue
        if metric.kind == "count":
            fields.append(f"COUNT(*) AS d{index}")
        else:
            numeric = _numeric_value(request, metric.column_id)
            fields.extend(
                (
                    f"SUM({numeric}) AS d{index}",
                    f"MIN({numeric}) < 0 AND MAX({numeric}) > 0 AS d{index}_mixed",
                )
            )
    return fields


def _having_sql(request: AnalyticsRequest) -> tuple[str, list[Any]]:
    operators = {"eq": "=", "neq": "!=", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}
    clauses = [
        f"{_metric_ref(request, item.metric_id)} {operators[item.operator]} ?"
        for item in request.having
    ]
    return " AND ".join(clauses), [item.value for item in request.having]


def _and_sql(left: str, right: str) -> str:
    return right if not left else f"({left}) AND ({right})"


def _order_sql(request: AnalyticsRequest, group_fields: list[str]) -> str:
    parts: list[str] = []
    for item in request.sort:
        if item.kind == "metric":
            value = _metric_ref(request, item.metric_id)
            parts.extend((f"{value} IS NULL ASC", f"{value} {item.direction.upper()}"))
        else:
            index = item.group_index
            parts.extend(
                (
                    f"CASE a.g{index}_kind WHEN 'valid' THEN 0 WHEN 'missing' THEN 1 ELSE 2 END ASC",
                    f"a.g{index}_value {item.direction.upper()}",
                    f"a.g{index}_json {item.direction.upper()}",
                )
            )
    for index, _group in enumerate(request.groups):
        parts.extend(
            (
                f"CASE a.g{index}_kind WHEN 'valid' THEN 0 WHEN 'missing' THEN 1 ELSE 2 END ASC",
                f"a.g{index}_value ASC",
                f"a.g{index}_json ASC",
            )
        )
    return ", ".join(parts) or "row_count DESC"


def _excluded_null_groups(
    db: sqlite3.Connection,
    ctes: list[str],
    source_params: Sequence[Any],
    from_sql: str,
    excluded_sql: str,
    request: AnalyticsRequest,
    having_params: Sequence[Any],
) -> int:
    if not excluded_sql:
        return 0
    # HAVING predicates still apply; only the primary-ranking null exclusion is counted.
    having, _ = _having_sql(request)
    where = having or "1=1"
    return int(
        db.execute(
            "WITH "
            + ", ".join(ctes)
            + " SELECT COUNT(*) FROM "
            + from_sql
            + f" WHERE ({where}) AND ({excluded_sql})",
            [*source_params, *having_params],
        ).fetchone()[0]
    )


def _result_group(
    row: sqlite3.Row,
    request: AnalyticsRequest,
    columns: Mapping[int, Any],
    quality_columns: Sequence[int],
) -> dict[str, Any]:
    group: list[dict[str, Any]] = []
    filter_: dict[str, Any] = {}
    for index, spec in enumerate(request.groups):
        kind = str(row[f"g{index}_kind"])
        item: dict[str, Any] = {"column_id": spec.column_id, "kind": kind}
        predicate: dict[str, Any] = {"kind": kind}
        if kind == "valid":
            if spec.bucket is None:
                value_json = str(row[f"g{index}_json"])
                item["value"] = json.loads(value_json)
                predicate = {
                    "kind": "value",
                    "value_json": value_json,
                }
            else:
                item["kind"] = "valid_date_bucket"
                item["bucket"] = spec.bucket
                item["value"] = row[f"g{index}_value"]
                predicate = {
                    "kind": "date_bucket",
                    "bucket": spec.bucket,
                    "value": row[f"g{index}_value"],
                }
        group.append(item)
        filter_[str(columns[spec.column_id]["name"])] = {"group_eq": predicate}
    metrics = {
        metric.id: row[f"m{index}"] for index, metric in enumerate(request.metrics)
    }
    quality: dict[str, Any] = {}
    for column_id in quality_columns:
        pos = _column_pos(request, column_id)
        quality[str(column_id)] = {
            "column_id": column_id,
            "present": int(row[f"q{pos}_present"]),
            "missing": int(row[f"q{pos}_missing"]),
            "invalid": int(row[f"q{pos}_invalid"]),
        }
    return {
        "group": group,
        "locator": {
            "version": "frisket.ask.group.v1",
            "sheet_id": request.sheet_id,
            "filter": filter_,
        },
        "metrics": metrics,
        "quality": quality,
    }


def _denominators(row: sqlite3.Row | None, request: AnalyticsRequest) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for index, metric in enumerate(request.metrics):
        if not metric.percent_of_total:
            continue
        value = row[f"d{index}"] if row is not None else 0
        if isinstance(value, float) and not math.isfinite(value):
            raise NumericOverflowError("numeric_overflow: " + metric.id)
        out[metric.id] = {
            "value": value,
            "reason": "zero_denominator" if value == 0 else None,
            "mixed_sign": bool(row[f"d{index}_mixed"])
            if row is not None and metric.kind == "sum"
            else False,
        }
    return out


def _attach_percentages(
    groups: list[dict[str, Any]],
    denominators: Mapping[str, Any],
    request: AnalyticsRequest,
) -> None:
    for group in groups:
        percentages: dict[str, float | None] = {}
        for metric in request.metrics:
            if not metric.percent_of_total:
                continue
            denominator = denominators[metric.id]["value"]
            value = group["metrics"][metric.id]
            percentages[metric.id] = (
                None
                if denominator in (None, 0) or value is None
                else float(value) * 100.0 / float(denominator)
            )
            if percentages[metric.id] is not None and not math.isfinite(
                percentages[metric.id]
            ):
                raise NumericOverflowError("numeric_overflow: " + metric.id)
        if percentages:
            group["percentages"] = percentages


def _assert_query_finite(
    db: sqlite3.Connection,
    ctes: Sequence[str],
    source_params: Sequence[Any],
    from_sql: str,
    request: AnalyticsRequest,
) -> None:
    names = [metric.id for metric in request.metrics if metric.kind in _NUMERIC_METRICS]
    checks = [
        f"({_metric_ref(request, metric_id)} IS NOT NULL AND "
        f"NOT ({_metric_ref(request, metric_id)} BETWEEN ? AND ?))"
        for metric_id in names
    ]
    if not checks:
        return
    bounds = [-sys.float_info.max, sys.float_info.max] * len(checks)
    invalid = db.execute(
        "WITH "
        + ", ".join(ctes)
        + " SELECT COUNT(*) FROM "
        + from_sql
        + " WHERE "
        + " OR ".join(checks),
        [*source_params, *bounds],
    ).fetchone()[0]
    if invalid:
        raise NumericOverflowError("numeric_overflow: " + ", ".join(names))
