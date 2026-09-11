"""Shared rowset evaluation helpers for grid, watches, and query consumers."""

from __future__ import annotations

from collections.abc import Sequence
from calendar import monthrange
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
import json
import math
import re
from typing import Any

from frisket.authoring.column_types import range_facet_value_kind
from frisket.calendar_dates import normalize_utc_calendar_date
from frisket.engine.store import Project
from frisket.engine.store.result_generations import ResultGenerationStore
from frisket.engine.store.runs import (
    FAILURE_OUTCOMES,
    TERMINAL_FAILURE_OUTCOMES,
    outcome_sql_list,
)


SHEET_FILTER_ROWSET_EVALUATOR = {
    "kind": "frisket.querysets.sheet_filter",
    "version": "v1",
}

_SQL_UTC_DATE = "frisket_utc_calendar_date"
_SQL_SIGNED_INT64 = "frisket_signed_int64"

# --------------------------------------------------------------------------
# The one guarded ``json_each`` construction for entity-mention JSON.
#
# BOTH the `entity_eq` WHERE clause below and the Mentions preview aggregation
# (frisket.preview.entity_mentions) walk entity arrays through these helpers,
# so the two can never disagree about which cells contribute entities — which
# is what makes the preview's row_count equal the row count of the selector it
# emits.
#
# Handing a live cell value straight to json_each() is not survivable:
# json_each() ABORTS the whole statement with "malformed JSON" on invalid
# text, and yields one scalar row (with a non-'object' type) for a JSON string
# or number. The guard rewrites anything that is not a JSON ARRAY into '[]',
# so a malformed / scalar / string / NULL / object-valued cell contributes
# zero entities instead of failing the request.
#
# The nesting is load-bearing: json_type() ITSELF raises on malformed JSON, so
# it must sit inside a CASE branch json_valid() has already cleared rather
# than beside it in one AND (whose short-circuiting SQLite does not promise).
# json_valid(NULL) is NULL, so a NULL cell falls through to the ELSE.
# --------------------------------------------------------------------------

ENTITY_MENTION_ALIAS = "je"

# Explicit marker (columns.semantic_type) a column must carry before any
# entity-mention reader will touch it. Never duck-typed from cell contents.
ENTITY_MENTIONS_SEMANTIC_TYPE = "entity_mentions"

# The two comparable selector payloads an `entity_eq` filter accepts, as the
# JSON path each one reads off an entity item.
_ENTITY_EQ_SELECTOR_SQL = {
    "text": f"json_extract({ENTITY_MENTION_ALIAS}.value, '$.text')",
    "fingerprint": f"json_extract({ENTITY_MENTION_ALIAS}.value, '$.fingerprint')",
}

# What counts as an entity, shared verbatim by the filter and the preview.
#
# Only a JSON ARRAY ITEM THAT IS AN OBJECT can be an entity, and it must carry
# a non-empty STRING `type` and `text`. The text requirement is not fussiness:
# every mention group has to be expressible as an `entity_eq` selector (which
# rejects empty type/text), so an item the preview could never emit a selector
# for must not be selectable by the filter either — otherwise a type-only
# filter would return rows the panel shows no group for, and parity would be a
# near-miss instead of an identity.
ENTITY_MENTION_ITEM_PREDICATE = " AND ".join(
    (
        f"{ENTITY_MENTION_ALIAS}.type = 'object'",
        f"json_type({ENTITY_MENTION_ALIAS}.value, '$.type') = 'text'",
        f"json_extract({ENTITY_MENTION_ALIAS}.value, '$.type') <> ''",
        f"json_type({ENTITY_MENTION_ALIAS}.value, '$.text') = 'text'",
        f"json_extract({ENTITY_MENTION_ALIAS}.value, '$.text') <> ''",
    )
)


def guarded_entity_array_sql(value_sql: str) -> str:
    """``value_sql`` narrowed to a JSON array expression, or ``'[]'``."""
    return (
        f"CASE WHEN json_valid({value_sql}) "
        f"THEN (CASE WHEN json_type({value_sql}) = 'array' "
        f"THEN {value_sql} ELSE '[]' END) "
        "ELSE '[]' END"
    )


def guarded_entity_array_params(value_params: Sequence[Any]) -> list[Any]:
    """``value_sql`` is emitted three times by the guard, so its bound
    parameters must be repeated three times in that order."""
    return [*value_params, *value_params, *value_params]


def entity_mention_source_sql(value_sql: str) -> str:
    """The FROM-clause fragment that walks one cell's entity array."""
    return f"json_each({guarded_entity_array_sql(value_sql)}) {ENTITY_MENTION_ALIAS}"


def column_semantic_type(column: Any) -> str | None:
    """A column row's ``semantic_type``, tolerating pre-migration rows."""
    try:
        keys = column.keys()
    except AttributeError:
        return column.get("semantic_type")
    return column["semantic_type"] if "semantic_type" in keys else None


def is_entity_mentions_column(column: Any) -> bool:
    """The single eligibility rule: a `json` column
    explicitly marked ``semantic_type='entity_mentions'``. No cell sampling,
    no key inspection, no name heuristics."""
    return (
        column["type"] == "json"
        and column_semantic_type(column) == ENTITY_MENTIONS_SEMANTIC_TYPE
    )


class SheetRowSetError(ValueError):
    """Raised when a sheet rowset filter or sort cannot be evaluated."""


@dataclass(frozen=True)
class SheetFilterRowSet:
    sheet_id: int
    row_ids: list[int]
    total: int
    limit: int
    offset: int
    filter: str | None = None
    sort: str | None = None


@dataclass(frozen=True)
class RuntimeSheetFilter:
    column: Any
    operator: str
    value: Any
    binding: Any


SheetFilter = tuple[Any, str, Any] | RuntimeSheetFilter


def sheet_row_scope_query(
    project: Project,
    sheet_id: int,
    *,
    parent_row_id: int | None = None,
    filter_: str | None = None,
    sort: str | None = None,
    reference_date: date | None = None,
) -> tuple[list[Any], str, list[Any], list[str], list[Any]]:
    """Return columns, WHERE SQL/params, and ORDER BY SQL/params for a sheet."""

    cols = project.columns(sheet_id)
    columns_by_name = {c["name"]: c for c in cols}
    filters = _parse_sheet_filter(filter_, columns_by_name, project)
    sorts = _parse_sheet_sort(sort, columns_by_name)
    generation_store = ResultGenerationStore(project)
    managed_columns: dict[int, bool] = {}
    today = reference_date or datetime.now(UTC).date()
    project.db.create_function(
        _SQL_UTC_DATE,
        1,
        normalize_utc_calendar_date,
        deterministic=True,
    )
    project.db.create_function(
        _SQL_SIGNED_INT64,
        1,
        _signed_int64_json_value,
        deterministic=True,
    )

    def is_generation_managed(column: Any) -> bool:
        column_id = int(column["id"])
        if column_id not in managed_columns:
            managed_columns[column_id] = generation_store.is_generation_managed(
                column_id
            )
        return managed_columns[column_id]

    where = ["r.sheet_id=?", "r.hidden=0"]
    where_params: list[Any] = [sheet_id]
    if parent_row_id is not None:
        where.append("r.parent_row_id=?")
        where_params.append(parent_row_id)
    for filter_item in filters:
        if isinstance(filter_item, RuntimeSheetFilter):
            runtime_sql, runtime_params = _runtime_operator_where(
                project,
                sheet_id,
                filter_item,
                base_where=where,
                base_params=where_params,
            )
            where.append(runtime_sql)
            where_params.extend(runtime_params)
            continue
        column, operator, value = filter_item
        if operator == "failed":
            # Failure triage follows the exact active head. A failed head is
            # authoritative even though its live value is NULL.
            if value == "any":
                # outcome_sql_list interpolates module constants only; the
                # user-supplied value never reaches SQL text.
                outcome_predicate = (
                    "res.outcome IN "
                    f"({outcome_sql_list(FAILURE_OUTCOMES + TERMINAL_FAILURE_OUTCOMES)})"
                )
                outcome_params: list[Any] = []
            else:
                outcome_predicate = "res.outcome = ?"
                outcome_params = [value]
            if is_generation_managed(column):
                where.append(
                    "EXISTS (SELECT 1 FROM cell_result_heads head "
                    "JOIN results res ON res.run_id = head.run_id "
                    "AND res.row_id = head.row_id "
                    "AND res.column_id = head.column_id "
                    "WHERE head.row_id = r.id AND head.column_id = ? "
                    f"AND {outcome_predicate})"
                )
                where_params.extend([column["id"], *outcome_params])
                continue
            where.append("0=1")
            continue
        value_sql, value_params = sheet_live_value_sql(
            "r", column, preserve_invalid=operator == "date_invalid"
        )
        if operator == "entity_eq":
            # Rows carrying at least one matching entity in a marked
            # entity_mentions JSON column. The guarded json_each above means a
            # malformed / scalar / string / NULL cell simply matches nothing.
            # Placeholder order follows SQL TEXT order: the guard's three
            # copies of the live-value subquery sit inside json_each() in the
            # FROM clause, so they bind BEFORE the selector comparisons.
            entity_type, selector_kind, selector_value = value
            predicates = [
                ENTITY_MENTION_ITEM_PREDICATE,
                f"json_extract({ENTITY_MENTION_ALIAS}.value, '$.type') = ?",
            ]
            selector_params: list[Any] = [entity_type]
            if selector_kind is not None:
                # Path comes from a closed table, never from the payload.
                predicates.append(f"{_ENTITY_EQ_SELECTOR_SQL[selector_kind]} = ?")
                selector_params.append(selector_value)
            where.append(
                f"EXISTS (SELECT 1 FROM {entity_mention_source_sql(value_sql)} "
                f"WHERE {' AND '.join(predicates)})"
            )
            where_params.extend(guarded_entity_array_params(value_params))
            where_params.extend(selector_params)
            continue
        if operator == "list_contains_any":
            # Element-level containment on a JSON array. The same guard used
            # for entity mentions turns malformed/scalar/object cells into an
            # empty array, so an untrusted stored value cannot abort a grid
            # query. Selectors have already been closed and type-checked by
            # the parser; only their values become SQL parameters here.
            selector_predicates: list[str] = []
            selector_params: list[Any] = []
            selector_kind = value[0][0]
            if selector_kind == "entity":
                for _kind, entity_type, text in value:
                    selector_predicates.append(
                        "("
                        + " AND ".join(
                            (
                                ENTITY_MENTION_ITEM_PREDICATE,
                                f"json_extract({ENTITY_MENTION_ALIAS}.value, '$.type') = ?",
                                f"json_extract({ENTITY_MENTION_ALIAS}.value, '$.text') = ?",
                            )
                        )
                        + ")"
                    )
                    selector_params.extend((entity_type, text))
            else:
                for _kind, scalar_type, scalar in value:
                    if scalar_type == "boolean":
                        selector_predicates.append(f"{ENTITY_MENTION_ALIAS}.type = ?")
                        selector_params.append("true" if scalar else "false")
                    elif scalar_type == "text":
                        selector_predicates.append(
                            f"({ENTITY_MENTION_ALIAS}.type = 'text' "
                            f"AND {ENTITY_MENTION_ALIAS}.value = ?)"
                        )
                        selector_params.append(scalar)
                    else:
                        # Direct equality preserves SQLite integers while also
                        # matching the equivalent JSON real (1 and 1.0 share
                        # the browser number contract).
                        selector_predicates.append(
                            f"({ENTITY_MENTION_ALIAS}.type IN ('integer', 'real') "
                            f"AND {ENTITY_MENTION_ALIAS}.value = ?)"
                        )
                        selector_params.append(scalar)
            where.append(
                f"EXISTS (SELECT 1 FROM {entity_mention_source_sql(value_sql)} "
                f"WHERE {' OR '.join(selector_predicates)})"
            )
            where_params.extend(guarded_entity_array_params(value_params))
            where_params.extend(selector_params)
            continue
        if operator == "bbox":
            # geo_point JSON is {lat,lon}; extract each axis and range-test.
            min_lon, min_lat, max_lon, max_lat = value
            lon_sql = f"CAST(json_extract({value_sql}, '$.lon') AS REAL)"
            lat_sql = f"CAST(json_extract({value_sql}, '$.lat') AS REAL)"
            where.append(
                f"({lon_sql} >= ? AND {lon_sql} <= ? "
                f"AND {lat_sql} >= ? AND {lat_sql} <= ?)"
            )
            where_params.extend(value_params)
            where_params.append(min_lon)
            where_params.extend(value_params)
            where_params.append(max_lon)
            where_params.extend(value_params)
            where_params.append(min_lat)
            where_params.extend(value_params)
            where_params.append(max_lat)
            continue
        use_boolean_literal = column["type"] == "boolean" and operator in {
            "eq",
            "in",
            "neq",
        }
        range_value_kind = range_facet_value_kind(str(column["type"]))
        is_numeric_range = range_value_kind in {"integer", "number"} and operator in {
            "gte",
            "lte",
            "between",
        }
        filter_value = (
            value_sql
            if use_boolean_literal
            else (
                (
                    f"{_SQL_SIGNED_INT64}({value_sql})"
                    if range_value_kind == "integer"
                    else (
                        "CASE WHEN "
                        f"json_type({value_sql}, '$') IN ('integer', 'real') "
                        f"THEN CAST(json_extract({value_sql}, '$') AS REAL) END"
                    )
                )
                if is_numeric_range
                else _filter_value_sql(value_sql, column)
            )
        )
        filter_value_params = value_params * (
            2 if is_numeric_range and range_value_kind == "number" else 1
        )
        compare_value = (
            _validate_boolean_filter_literal(str(column["name"]), value)
            if use_boolean_literal and operator != "in"
            else value
        )
        date_value = (
            f"{_SQL_UTC_DATE}({filter_value})"
            if column["type"] == "date" or range_value_kind == "date"
            else None
        )
        compared_value = date_value or filter_value
        if operator == "eq":
            where.append(f"{compared_value} = ?")
            where_params.extend(filter_value_params)
        elif operator == "in":
            placeholders = ", ".join("?" for _ in value)
            where.append(f"{compared_value} IN ({placeholders})")
            where_params.extend(filter_value_params)
            where_params.extend(
                _validate_boolean_filter_literal(str(column["name"]), item)
                if use_boolean_literal
                else item
                for item in value
            )
            continue
        elif operator == "neq":
            where.append(f"({compared_value} IS NULL OR {compared_value} != ?)")
            where_params.extend(filter_value_params)
            where_params.extend(filter_value_params)
        elif operator == "contains":
            where.append(f"{filter_value} COLLATE NOCASE LIKE ? ESCAPE '\\'")
            where_params.extend(filter_value_params)
        elif operator == "gte":
            where.append(f"{compared_value} >= ?")
            where_params.extend(filter_value_params)
        elif operator == "lte":
            where.append(f"{compared_value} <= ?")
            where_params.extend(filter_value_params)
        elif operator == "between":
            start, end = value
            where.append(f"({compared_value} >= ? AND {compared_value} <= ?)")
            where_params.extend(filter_value_params)
            where_params.append(start)
            where_params.extend(filter_value_params)
            where_params.append(end)
            continue
        elif operator == "date_relative":
            amount, unit = value
            start = _relative_date_start(today, amount, unit)
            where.append(f"({date_value} >= ? AND {date_value} <= ?)")
            where_params.extend(value_params)
            where_params.append(start.isoformat())
            where_params.extend(value_params)
            where_params.append(today.isoformat())
            continue
        elif operator == "date_this_year":
            where.append(f"substr({date_value}, 1, 4) = ?")
            where_params.extend(value_params)
            where_params.append(f"{today.year:04d}")
            continue
        elif operator == "date_ytd":
            where.append(f"({date_value} >= ? AND {date_value} <= ?)")
            where_params.extend(value_params)
            where_params.append(date(today.year, 1, 1).isoformat())
            where_params.extend(value_params)
            where_params.append(today.isoformat())
            continue
        elif operator == "date_year":
            where.append(f"CAST(strftime('%Y', {date_value}) AS INTEGER) = ?")
            where_params.extend(value_params)
            where_params.append(value)
            continue
        elif operator == "date_month":
            where.append(f"CAST(strftime('%m', {date_value}) AS INTEGER) = ?")
            where_params.extend(value_params)
            where_params.append(value)
            continue
        elif operator == "date_weekday":
            where.append(f"CAST(strftime('%w', {date_value}) AS INTEGER) = ?")
            where_params.extend(value_params)
            where_params.append(value)
            continue
        elif operator == "date_invalid":
            where.append(
                f"({filter_value} IS NOT NULL AND trim({filter_value}) != '' "
                f"AND {date_value} IS NULL)"
            )
            where_params.extend(value_params)
            where_params.extend(value_params)
            where_params.extend(value_params)
            continue
        else:
            raise AssertionError(f"unhandled filter operator: {operator}")
        if operator == "contains":
            where_params.append(_like_contains_pattern(value))
        else:
            where_params.append(compare_value)

    order_parts: list[str] = []
    order_params: list[Any] = []
    for column, direction in sorts:
        value_sql, value_params = sheet_live_value_sql("r", column)
        sort_value = f"json_extract({value_sql}, '$')"
        order_parts.append(f"{sort_value} IS NULL")
        order_parts.append(
            f"{sort_value}{_sort_collation_sql(column)} {direction.upper()}"
        )
        order_params.extend(value_params)
        order_params.extend(value_params)
    order_parts.append("r.position ASC")
    order_parts.append("r.id ASC")

    return cols, " AND ".join(where), where_params, order_parts, order_params


def resolve_sheet_filter_rows(
    project: Project,
    sheet_id: int,
    *,
    parent_row_id: int | None = None,
    filter_: str | None = None,
    sort: str | None = None,
    limit: int = 500,
    offset: int = 0,
    reference_date: date | None = None,
) -> SheetFilterRowSet:
    _, where_sql, where_params, order_parts, order_params = sheet_row_scope_query(
        project,
        sheet_id,
        parent_row_id=parent_row_id,
        filter_=filter_,
        sort=sort,
        reference_date=reference_date,
    )
    try:
        bounded_limit = max(0, int(limit))
        bounded_offset = max(0, int(offset))
    except (TypeError, ValueError):
        raise SheetRowSetError("limit and offset must be integers") from None
    order_clause = ", ".join(order_parts) or "r.position ASC"
    # Count ignores ordering, so only WHERE parameters are needed here.
    total = int(
        project.db.execute(
            f"SELECT COUNT(*) FROM rows r WHERE {where_sql}", where_params
        ).fetchone()[0]
        or 0
    )
    rows = project.db.execute(
        f"""
        SELECT r.id AS row_id
        FROM rows r
        WHERE {where_sql}
        ORDER BY {order_clause}
        LIMIT ? OFFSET ?
        """,
        [*where_params, *order_params, bounded_limit, bounded_offset],
    ).fetchall()
    return SheetFilterRowSet(
        sheet_id=sheet_id,
        row_ids=[int(row["row_id"]) for row in rows],
        total=total,
        limit=bounded_limit,
        offset=bounded_offset,
        filter=filter_,
        sort=sort,
    )


def validate_sheet_filter_sort(
    project: Project,
    sheet_id: int,
    *,
    filter_: str | None = None,
    sort: str | None = None,
) -> None:
    columns_by_name = {c["name"]: c for c in project.columns(sheet_id)}
    _parse_sheet_filter(filter_, columns_by_name, project)
    _parse_sheet_sort(sort, columns_by_name)


def sheet_live_value_sql(
    row_alias: str,
    column: Any,
    *,
    preserve_invalid: bool = False,
) -> tuple[str, list[Any]]:
    """Lookup the same current value used by ordinary cell readers."""
    value = (
        "live.value"
        if preserve_invalid
        else ("CASE WHEN live.validity='valid' THEN live.value END")
    )
    return (
        f"(SELECT {value} "
        "FROM current_cells live "
        f"WHERE live.column_id=? AND live.row_id={row_alias}.id)",
        [column["id"]],
    )


def _like_contains_pattern(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _sort_collation_sql(column: Any) -> str:
    return (
        " COLLATE NOCASE"
        if column["type"]
        in {"text", "category", "date", "link", "image", "audio", "video", "file"}
        else ""
    )


def _filter_value_sql(value_sql: str, column: Any) -> str:
    return f"CAST(json_extract({value_sql}, '$') AS TEXT)"


_BOOLEAN_TRUE_LITERALS = frozenset({"true", "1", "yes", "y", "on"})
_BOOLEAN_FALSE_LITERALS = frozenset({"false", "0", "no", "n", "off"})


def _validate_boolean_filter_literal(column_name: str, value: Any) -> str:
    if not isinstance(value, str):
        raise SheetRowSetError(f"boolean filter for {column_name} must be a string")
    normalized = value.strip().lower()
    if normalized in _BOOLEAN_TRUE_LITERALS:
        return json.dumps(True)
    if normalized in _BOOLEAN_FALSE_LITERALS:
        return json.dumps(False)
    raise SheetRowSetError(f"invalid boolean filter for {column_name}")


def _validate_date_filter_bound(column_name: str, value: Any) -> str:
    if not isinstance(value, str):
        raise SheetRowSetError(f"date filter for {column_name} must be a string")
    bound = normalize_utc_calendar_date(value)
    if bound is None:
        raise SheetRowSetError(f"invalid date filter for {column_name}")
    return bound


def _validate_numeric_filter_bound(column_name: str, value: Any) -> float:
    if isinstance(value, bool):
        raise SheetRowSetError(
            f"numeric range filter for {column_name} requires a numeric value"
        )
    try:
        bound = float(value)
    except (TypeError, ValueError):
        raise SheetRowSetError(
            f"numeric range filter for {column_name} requires a numeric value"
        ) from None
    if not math.isfinite(bound):
        raise SheetRowSetError(
            f"numeric range filter for {column_name} requires a finite value"
        )
    return bound


_CANONICAL_INTEGER = re.compile(r"-?(?:0|[1-9]\d*)\Z")
_SQLITE_INTEGER_MIN = -(2**63)
_SQLITE_INTEGER_MAX = 2**63 - 1


def _signed_int64_json_value(value: Any) -> int | None:
    """SQLite UDF: exact native JSON signed-int64 value, else NULL."""
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return None
    if type(parsed) is int and _SQLITE_INTEGER_MIN <= parsed <= _SQLITE_INTEGER_MAX:
        return parsed
    return None


def _validate_integer_filter_bound(column_name: str, value: Any) -> int:
    if not isinstance(value, str) or _CANONICAL_INTEGER.fullmatch(value) is None:
        raise SheetRowSetError(
            f"integer range filter for {column_name} requires a canonical decimal string"
        )
    bound = int(value)
    if not _SQLITE_INTEGER_MIN <= bound <= _SQLITE_INTEGER_MAX:
        raise SheetRowSetError(
            f"integer range filter for {column_name} exceeds signed 64-bit range"
        )
    return bound


def _subtract_calendar_months(value: date, months: int) -> date:
    target_index = value.year * 12 + value.month - 1 - months
    target_year, target_month_index = divmod(target_index, 12)
    target_month = target_month_index + 1
    target_day = min(value.day, monthrange(target_year, target_month)[1])
    return date(target_year, target_month, target_day)


def _relative_date_start(today: date, amount: int, unit: str) -> date:
    if unit == "days":
        return today - timedelta(days=amount - 1)
    if unit == "weeks":
        return today - timedelta(days=amount * 7 - 1)
    return _subtract_calendar_months(today, amount)


_BUILTIN_FILTER_OPERATORS = (
    "eq",
    "in",
    "neq",
    "contains",
    "gte",
    "lte",
    "between",
    "date_relative",
    "date_this_year",
    "date_ytd",
    "date_year",
    "date_month",
    "date_weekday",
    "date_invalid",
    "bbox",
    "entity_eq",
    "list_contains_any",
    "failed",
)

_DATE_FILTER_OPERATORS = frozenset(
    {
        "gte",
        "lte",
        "between",
        "date_relative",
        "date_this_year",
        "date_ytd",
        "date_year",
        "date_month",
        "date_weekday",
        "date_invalid",
    }
)
_DATE_RELATIVE_UNITS = frozenset({"days", "weeks", "months"})


def _validate_date_relative_value(column_name: str, value: Any) -> tuple[int, str]:
    if not isinstance(value, dict) or set(value) != {"amount", "unit"}:
        raise SheetRowSetError(
            f"relative date filter for {column_name} requires amount and unit"
        )
    amount = value.get("amount")
    unit = value.get("unit")
    if (
        isinstance(amount, bool)
        or not isinstance(amount, int)
        or not 1 <= amount <= 10000
    ):
        raise SheetRowSetError(
            f"relative date filter for {column_name} requires amount from 1 to 10000"
        )
    if unit not in _DATE_RELATIVE_UNITS:
        raise SheetRowSetError(
            f"relative date filter for {column_name} requires days, weeks, or months"
        )
    return amount, str(unit)


def _validate_date_part(column_name: str, operator: str, value: Any) -> int:
    if isinstance(value, bool):
        raise SheetRowSetError(f"invalid date-part filter for {column_name}") from None
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and value.strip().isdecimal():
        parsed = int(value.strip())
    else:
        raise SheetRowSetError(f"invalid date-part filter for {column_name}")
    bounds = {
        "date_year": (1, 9999),
        "date_month": (1, 12),
        "date_weekday": (0, 6),
    }
    low, high = bounds[operator]
    if not low <= parsed <= high:
        raise SheetRowSetError(f"invalid date-part filter for {column_name}")
    return parsed


_MAX_IN_FILTER_VALUES = 100
_MAX_SAFE_JAVASCRIPT_INTEGER = 2**53 - 1

# The closed `entity_eq` payload: `type` plus AT MOST ONE
# of text/fingerprint. Anything else is rejected loudly rather than narrowed
# to a filter that quietly selects the wrong rows.
_ENTITY_EQ_KEYS = ("type", "text", "fingerprint")
_ENTITY_EQ_SELECTOR_KEYS = ("text", "fingerprint")

# Legal `failed` operator values: a single failure bucket from the result
# outcome taxonomy, or "any" for all of them. Validated here so the WHERE
# builder only ever sees taxonomy members.
_FAILED_FILTER_VALUES = frozenset(
    {"any", *FAILURE_OUTCOMES, *TERMINAL_FAILURE_OUTCOMES}
)


def _validated_list_scalar_filter_value(
    column_name: str, value: Any
) -> tuple[str, str | bool | int | float]:
    """Validate one generic JSON-list selector value.

    The selector travels through JSON to a browser and back, so unsupported
    JSON shapes, nulls, non-finite values, and numbers outside the exact
    JavaScript-safe range are rejected rather than quietly changing meaning.
    """
    if isinstance(value, str):
        return "text", value
    if isinstance(value, bool):
        return "boolean", value
    if type(value) is int:
        if abs(value) <= _MAX_SAFE_JAVASCRIPT_INTEGER:
            return "number", value
    elif type(value) is float:
        if math.isfinite(value) and abs(value) <= _MAX_SAFE_JAVASCRIPT_INTEGER:
            return "number", int(value) if value.is_integer() else value
    raise SheetRowSetError(
        f"list_contains_any filter for {column_name} accepts string, boolean, "
        "or finite JavaScript-safe number scalar values"
    )


def _parse_list_contains_any_filter(
    column: Any, column_name: str, value: Any
) -> tuple[tuple[Any, ...], ...]:
    """Close a list-facet payload into replayable scalar/entity selectors."""
    if column["type"] != "json":
        raise SheetRowSetError(
            f"list_contains_any filter requires a json column: {column_name}"
        )
    if not isinstance(value, list) or not value:
        raise SheetRowSetError(
            f"list_contains_any filter for {column_name} must be a non-empty array"
        )
    if len(value) > _MAX_IN_FILTER_VALUES:
        raise SheetRowSetError(
            f"list_contains_any filter for {column_name} supports at most "
            f"{_MAX_IN_FILTER_VALUES} selectors"
        )

    expected_kind = "entity" if is_entity_mentions_column(column) else "scalar"
    parsed: list[tuple[Any, ...]] = []
    seen: set[tuple[Any, ...]] = set()
    for selector in value:
        if not isinstance(selector, dict):
            raise SheetRowSetError(
                f"list_contains_any filter for {column_name} requires selector objects"
            )
        if selector.get("kind") != expected_kind:
            raise SheetRowSetError(
                f"list_contains_any filter for {column_name} requires {expected_kind} selectors"
            )
        expected_keys = (
            {"kind", "type", "text"} if expected_kind == "entity" else {"kind", "value"}
        )
        if set(selector) != expected_keys:
            raise SheetRowSetError(
                f"list_contains_any filter for {column_name} has invalid selector keys"
            )
        if expected_kind == "entity":
            entity_type = selector.get("type")
            text = selector.get("text")
            if (
                not isinstance(entity_type, str)
                or not entity_type
                or not isinstance(text, str)
                or not text
            ):
                raise SheetRowSetError(
                    f"list_contains_any filter for {column_name} requires non-empty entity type and text"
                )
            normalized: tuple[Any, ...] = ("entity", entity_type, text)
        else:
            scalar_type, scalar_value = _validated_list_scalar_filter_value(
                column_name, selector.get("value")
            )
            normalized = ("scalar", scalar_type, scalar_value)
        if normalized not in seen:
            seen.add(normalized)
            parsed.append(normalized)
    return tuple(parsed)


def _parse_sheet_filter(
    raw: str | None, columns_by_name: dict[str, Any], project: Project
) -> list[SheetFilter]:
    if raw is None or raw == "":
        return []
    try:
        spec = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SheetRowSetError(f"invalid filter JSON: {exc.msg}") from exc
    if not isinstance(spec, dict):
        raise SheetRowSetError("filter must be a JSON object")

    out: list[SheetFilter] = []
    runtime_bindings = _runtime_operator_bindings(project)
    allowed = {*_BUILTIN_FILTER_OPERATORS, *runtime_bindings}
    for name, condition in spec.items():
        if not isinstance(name, str) or name not in columns_by_name:
            raise SheetRowSetError(f"unknown filter column: {name}")
        if not isinstance(condition, dict):
            raise SheetRowSetError(f"filter for {name} must be an object")
        unsupported = set(condition) - allowed
        if unsupported:
            raise SheetRowSetError(f"unsupported filter for {name}")
        active = [
            (op, condition[op])
            for op in (*_BUILTIN_FILTER_OPERATORS, *runtime_bindings)
            if op in condition and condition[op] is not None
        ]
        if len(active) != 1:
            raise SheetRowSetError(f"filter for {name} must specify one operator")
        op, value = active[0]
        column = columns_by_name[name]
        range_value_kind = range_facet_value_kind(str(column["type"]))
        if op in runtime_bindings:
            binding = runtime_bindings[op]
            if binding.handler is None:
                raise SheetRowSetError(f"runtime operator handler is unavailable: {op}")
            out.append(
                RuntimeSheetFilter(
                    column=column,
                    operator=op,
                    value=value,
                    binding=binding,
                )
            )
            continue
        if op == "failed":
            # Failed-cell predicate ("show the rows that failed"), honored by
            # /data, saved views, CSV export, and the map like every builtin.
            if not isinstance(value, str) or value not in _FAILED_FILTER_VALUES:
                raise SheetRowSetError(
                    f"failed filter for {name} must be 'any' or a failure "
                    "outcome from the result taxonomy"
                )
            out.append((column, "failed", value))
            continue
        if op == "bbox":
            # Geographic bounding box on a geo_point ({lat,lon}) column — the
            # "filter to viewport" operator, honored by /data, saved views, map.
            if column["type"] != "geo_point":
                raise SheetRowSetError(
                    f"bbox filter requires a geo_point column: {name}"
                )
            if not isinstance(value, dict):
                raise SheetRowSetError(f"bbox filter for {name} must be an object")
            try:
                bounds = (
                    float(value["min_lon"]),
                    float(value["min_lat"]),
                    float(value["max_lon"]),
                    float(value["max_lat"]),
                )
            except (KeyError, TypeError, ValueError):
                raise SheetRowSetError(
                    f"bbox filter for {name} requires numeric "
                    "min_lon/min_lat/max_lon/max_lat"
                ) from None
            if bounds[0] > bounds[2] or bounds[1] > bounds[3]:
                raise SheetRowSetError(f"bbox filter for {name} is reversed")
            out.append((column, "bbox", bounds))
            continue
        if op == "list_contains_any":
            out.append(
                (column, op, _parse_list_contains_any_filter(column, name, value))
            )
            continue
        if op == "entity_eq":
            # Entity-mention predicate on a marked entity JSON column — the
            # operator the Mentions panel emits, honored by /data, saved
            # views, CSV export, watches, map points and embedding scopes
            # because every consumer funnels through this parser.
            if not is_entity_mentions_column(column):
                raise SheetRowSetError(
                    "entity_eq filter requires a json column marked "
                    f"semantic_type='{ENTITY_MENTIONS_SEMANTIC_TYPE}': {name}"
                )
            if not isinstance(value, dict):
                raise SheetRowSetError(f"entity_eq filter for {name} must be an object")
            unknown = sorted(set(value) - set(_ENTITY_EQ_KEYS))
            if unknown:
                raise SheetRowSetError(
                    f"entity_eq filter for {name} has unknown keys: "
                    f"{', '.join(unknown)}"
                )
            entity_type = value.get("type")
            if not isinstance(entity_type, str) or not entity_type:
                raise SheetRowSetError(
                    f"entity_eq filter for {name} requires a non-empty type"
                )
            selectors = [key for key in _ENTITY_EQ_SELECTOR_KEYS if key in value]
            if len(selectors) > 1:
                raise SheetRowSetError(
                    f"entity_eq filter for {name} accepts at most one of "
                    "text/fingerprint"
                )
            selector_kind = selectors[0] if selectors else None
            selector_value = value[selector_kind] if selector_kind else None
            if selector_kind is not None and (
                not isinstance(selector_value, str) or not selector_value
            ):
                raise SheetRowSetError(
                    f"entity_eq filter for {name} requires a non-empty {selector_kind}"
                )
            out.append(
                (column, "entity_eq", (entity_type, selector_kind, selector_value))
            )
            continue
        if column["type"] == "boolean" and op == "contains":
            raise SheetRowSetError("contains is not supported for boolean columns")
        if op == "in":
            if not isinstance(value, list) or not value:
                raise SheetRowSetError(
                    f"in filter for {name} must be a non-empty array"
                )
            if len(value) > _MAX_IN_FILTER_VALUES:
                raise SheetRowSetError(
                    f"in filter for {name} supports at most {_MAX_IN_FILTER_VALUES} values"
                )
            if any(not isinstance(item, str) for item in value):
                raise SheetRowSetError(
                    f"in filter for {name} accepts string values only"
                )
            values = list(dict.fromkeys(value))
            if column["type"] == "date":
                values = [_validate_date_filter_bound(name, item) for item in values]
            elif column["type"] == "boolean":
                for item in values:
                    _validate_boolean_filter_literal(name, item)
            out.append((column, op, values))
            continue
        if column["type"] == "boolean" and op in {"eq", "neq"}:
            _validate_boolean_filter_literal(name, value)
        if (
            op in _DATE_FILTER_OPERATORS - {"gte", "lte", "between"}
            and column["type"] != "date"
        ):
            raise SheetRowSetError(f"date filter requires date column: {name}")
        if (
            column["type"] == "date"
            and op not in {"eq", "neq"}
            and op not in _DATE_FILTER_OPERATORS
        ):
            raise SheetRowSetError(f"unsupported date filter for {name}")
        if column["type"] == "date" and op in {"eq", "neq"}:
            out.append((column, op, _validate_date_filter_bound(name, value)))
        elif (column["type"] == "date" and op in _DATE_FILTER_OPERATORS) or (
            range_value_kind == "date" and op in {"gte", "lte", "between"}
        ):
            if op == "between":
                if not isinstance(value, dict):
                    raise SheetRowSetError(
                        f"between filter for {name} must be an object"
                    )
                if "start" not in value or "end" not in value:
                    raise SheetRowSetError(
                        f"between filter for {name} requires start and end"
                    )
                start = _validate_date_filter_bound(name, value.get("start"))
                end = _validate_date_filter_bound(name, value.get("end"))
                if start > end:
                    raise SheetRowSetError(f"between filter for {name} is reversed")
                out.append((column, op, (start, end)))
            elif op in {"gte", "lte"}:
                out.append((column, op, _validate_date_filter_bound(name, value)))
            elif op == "date_relative":
                out.append((column, op, _validate_date_relative_value(name, value)))
            elif op in {"date_year", "date_month", "date_weekday"}:
                out.append((column, op, _validate_date_part(name, op, value)))
            else:
                if value != "true":
                    raise SheetRowSetError(f"invalid date filter for {name}")
                out.append((column, op, value))
        elif op in {"gte", "lte", "between"}:
            if range_value_kind not in {"integer", "number"}:
                raise SheetRowSetError(
                    f"numeric range filter requires numeric column: {name}"
                )
            if op == "between":
                if not isinstance(value, dict):
                    raise SheetRowSetError(
                        f"between filter for {name} must be an object"
                    )
                if "start" not in value or "end" not in value:
                    raise SheetRowSetError(
                        f"between filter for {name} requires start and end"
                    )
                try:
                    validate_bound = (
                        _validate_integer_filter_bound
                        if range_value_kind == "integer"
                        else _validate_numeric_filter_bound
                    )
                    start = validate_bound(name, value.get("start"))
                    end = validate_bound(name, value.get("end"))
                except SheetRowSetError as exc:
                    raise SheetRowSetError(
                        f"numeric range filter for {name} requires numeric start and end"
                    ) from exc
                if start > end:
                    raise SheetRowSetError(f"between filter for {name} is reversed")
                out.append((column, op, (start, end)))
            else:
                validate_bound = (
                    _validate_integer_filter_bound
                    if range_value_kind == "integer"
                    else _validate_numeric_filter_bound
                )
                out.append((column, op, validate_bound(name, value)))
        else:
            out.append((column, op, str(value)))
    return out


def _runtime_operator_bindings(project: Project) -> dict[str, Any]:
    from frisket.authoring.plugin_registry import default_registry
    from frisket.authoring.workbench.plugin_runtime_capabilities import (
        project_runtime_binding,
    )

    return {
        spec.kind: binding
        for spec in default_registry().runtime_binding_specs("operators")
        if (
            binding := project_runtime_binding(
                project, binding_type="operators", kind=spec.kind
            )
        )
        is not None
    }


def _runtime_operator_where(
    project: Project,
    sheet_id: int,
    filter_item: RuntimeSheetFilter,
    *,
    base_where: list[str],
    base_params: list[Any],
) -> tuple[str, list[Any]]:
    candidate_rows = project.db.execute(
        f"SELECT r.id FROM rows r WHERE {' AND '.join(base_where)}",
        list(base_params),
    ).fetchall()
    candidate_row_ids = [int(row["id"]) for row in candidate_rows]
    binding = filter_item.binding
    handler = binding.handler
    payload = {
        "schemaVersion": "frisket.runtime_operator_request.v1",
        "projectId": getattr(project, "id", ""),
        "sheetId": sheet_id,
        "candidateRowIds": candidate_row_ids,
        "column": {
            "id": int(filter_item.column["id"]),
            "name": filter_item.column["name"],
            "type": filter_item.column["type"],
        },
        "operatorKind": filter_item.operator,
        "value": filter_item.value,
        "pluginId": binding.plugin,
        "handlerKey": binding.handler_key,
    }
    try:
        if binding.handler_api == "plugin_operator_subprocess":
            from frisket.authoring.workbench.plugin_subprocess_operators import (
                run_plugin_operator_subprocess,
            )

            result = run_plugin_operator_subprocess(
                project,
                binding=binding,
                payload=payload,
            )
        else:
            if handler is None:
                raise SheetRowSetError(
                    f"runtime operator handler is unavailable: {filter_item.operator}"
                )
            result = handler({**payload, "project": project})
    except Exception as exc:
        raise SheetRowSetError(
            f"runtime operator handler failed: {filter_item.operator}"
        ) from exc
    row_ids = _validate_runtime_operator_plan(
        result,
        candidate_row_ids=set(candidate_row_ids),
    )
    if not row_ids:
        return "0=1", []
    placeholders = ",".join("?" * len(row_ids))
    return f"r.id IN ({placeholders})", row_ids


def _validate_runtime_operator_plan(
    result: object,
    *,
    candidate_row_ids: set[int],
) -> list[int]:
    if not isinstance(result, dict):
        raise SheetRowSetError("invalid runtime operator plan")
    if result.get("schemaVersion") != "frisket.runtime_operator_plan.v1":
        raise SheetRowSetError("invalid runtime operator plan")
    if set(result) != {"schemaVersion", "rowIds"}:
        raise SheetRowSetError("invalid runtime operator plan")
    raw_row_ids = result.get("rowIds")
    if not isinstance(raw_row_ids, list):
        raise SheetRowSetError("invalid runtime operator plan")
    row_ids: list[int] = []
    seen: set[int] = set()
    for raw_row_id in raw_row_ids:
        if isinstance(raw_row_id, bool) or not isinstance(raw_row_id, int):
            raise SheetRowSetError("invalid runtime operator plan")
        if raw_row_id not in candidate_row_ids:
            raise SheetRowSetError("invalid runtime operator plan")
        if raw_row_id in seen:
            raise SheetRowSetError("invalid runtime operator plan")
        seen.add(raw_row_id)
        row_ids.append(raw_row_id)
    return row_ids


def _parse_sheet_sort(
    raw: str | None, columns_by_name: dict[str, Any]
) -> list[tuple[Any, str]]:
    if raw is None or raw == "":
        return []
    try:
        spec = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SheetRowSetError(f"invalid sort JSON: {exc.msg}") from exc
    if not isinstance(spec, list):
        raise SheetRowSetError("sort must be a JSON array")

    out: list[tuple[Any, str]] = []
    for item in spec:
        if not isinstance(item, dict):
            raise SheetRowSetError("sort entries must be objects")
        name = item.get("column")
        if not isinstance(name, str) or name not in columns_by_name:
            raise SheetRowSetError(f"unknown sort column: {name}")
        direction = str(item.get("dir", "asc")).lower()
        if direction not in {"asc", "desc"}:
            raise SheetRowSetError(f"unsupported sort direction: {direction}")
        out.append((columns_by_name[name], direction))
    return out
