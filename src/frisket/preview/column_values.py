"""Read-only column distinct-values preview (no receipt, no side effects).

The authoring surfaces for resolve.substitute / resolve.combine enumerate a
column's distinct values with counts (frequency-sorted, searchable, paged).
The ``value_hash`` remains for the legacy cluster consumer.
"""

from __future__ import annotations

import json
import math
from collections import Counter
from datetime import date, timedelta
from typing import Any

from frisket.authoring.column_types import range_facet_value_kind, validate_value
from frisket.calendar_dates import parse_utc_calendar_date
from frisket.preview.cluster import hash_column_values
from frisket.preview.common import (
    ColumnPreviewError,
    require_visible_column,
    require_visible_sheet,
)
from frisket.querysets import ENTITY_MENTIONS_SEMANTIC_TYPE, column_semantic_type
from frisket.engine.store import Project
from frisket.engine.store.current_value_windows import read_current_column
from frisket.engine.store.project import ProjectReadSnapshot

COLUMN_VALUES_PREVIEW_SCHEMA_VERSION = "frisket.column_values_preview.v1"
COLUMN_VALUES_DEFAULT_LIMIT = 500
COLUMN_VALUES_MAX_LIMIT = 2_000
FACET_HISTOGRAM_BINS = 12
COLUMN_VALUES_RESPONSE_BYTE_LIMIT = 64 * 1024
_MAX_SAFE_JAVASCRIPT_INTEGER = 2**53 - 1


class ColumnValuesPreviewError(ColumnPreviewError):
    pass


def _scoped_bin_counts(
    indexes: list[int], in_scope: list[bool] | None, bin_count: int
) -> list[int] | None:
    """Per-bin counts of the in-scope values, binned exactly like the full column."""
    if in_scope is None:
        return None
    counts = [0] * bin_count
    for index, scoped in zip(indexes, in_scope, strict=True):
        if scoped:
            counts[index] += 1
    return counts


def _with_scoped_counts(
    distribution: dict[str, Any], scoped_counts: list[int] | None
) -> dict[str, Any]:
    if scoped_counts is not None:
        distribution["scoped_counts"] = scoped_counts
    return distribution


def _number_distribution(
    values: list[float], in_scope: list[bool] | None = None
) -> dict[str, Any] | None:
    if not values:
        return None
    lower = min(values)
    upper = max(values)
    if lower == upper:
        return _with_scoped_counts(
            {
                "kind": "number",
                "min": lower,
                "max": upper,
                "bins": [{"start": lower, "end": upper, "count": len(values)}],
            },
            _scoped_bin_counts([0] * len(values), in_scope, 1),
        )
    scale = max(abs(lower), abs(upper), 1.0)
    normalized_lower = lower / scale
    normalized_upper = upper / scale
    normalized_width = (normalized_upper - normalized_lower) / FACET_HISTOGRAM_BINS
    if not math.isfinite(normalized_width) or normalized_width <= 0:
        # Two distinct finite subnormal values can have a span that underflows
        # to zero after normalization. Keep the preview useful and, critically,
        # never divide by that rounded-away span.
        return _with_scoped_counts(
            {
                "kind": "number",
                "min": lower,
                "max": upper,
                "bins": [{"start": lower, "end": upper, "count": len(values)}],
            },
            _scoped_bin_counts([0] * len(values), in_scope, 1),
        )
    counts = [0] * FACET_HISTOGRAM_BINS
    indexes = [
        min(
            int(((value / scale) - normalized_lower) / normalized_width),
            FACET_HISTOGRAM_BINS - 1,
        )
        for value in values
    ]
    for index in indexes:
        counts[index] += 1
    return _with_scoped_counts(
        {
            "kind": "number",
            "min": lower,
            "max": upper,
            "bins": [
                {
                    "start": (
                        lower * (1 - index / FACET_HISTOGRAM_BINS)
                        + upper * (index / FACET_HISTOGRAM_BINS)
                    ),
                    "end": upper
                    if index == FACET_HISTOGRAM_BINS - 1
                    else (
                        lower * (1 - (index + 1) / FACET_HISTOGRAM_BINS)
                        + upper * ((index + 1) / FACET_HISTOGRAM_BINS)
                    ),
                    "count": count,
                }
                for index, count in enumerate(counts)
            ],
        },
        _scoped_bin_counts(indexes, in_scope, FACET_HISTOGRAM_BINS),
    )


def _integer_distribution(
    values: list[int], in_scope: list[bool] | None = None
) -> dict[str, Any] | None:
    if not values:
        return None
    lower = min(values)
    upper = max(values)
    if lower == upper:
        return _with_scoped_counts(
            {
                "kind": "integer",
                "min": str(lower),
                "max": str(upper),
                "bins": [
                    {"start": str(lower), "end": str(upper), "count": len(values)}
                ],
            },
            _scoped_bin_counts([0] * len(values), in_scope, 1),
        )
    span = upper - lower + 1
    width = max(1, (span + FACET_HISTOGRAM_BINS - 1) // FACET_HISTOGRAM_BINS)
    bin_count = min(FACET_HISTOGRAM_BINS, (span + width - 1) // width)
    counts = [0] * bin_count
    indexes = [min((value - lower) // width, bin_count - 1) for value in values]
    for index in indexes:
        counts[index] += 1
    return _with_scoped_counts(
        {
            "kind": "integer",
            "min": str(lower),
            "max": str(upper),
            "bins": [
                {
                    "start": str(lower + index * width),
                    "end": str(min(upper, lower + (index + 1) * width - 1)),
                    "count": count,
                }
                for index, count in enumerate(counts)
            ],
        },
        _scoped_bin_counts(indexes, in_scope, bin_count),
    )


def _date_distribution(
    values: list[date], in_scope: list[bool] | None = None
) -> dict[str, Any] | None:
    if not values:
        return None
    lower = min(values)
    upper = max(values)
    span_days = (upper - lower).days + 1
    width_days = max(1, math.ceil(span_days / FACET_HISTOGRAM_BINS))
    bin_count = min(FACET_HISTOGRAM_BINS, math.ceil(span_days / width_days))
    counts = [0] * bin_count
    indexes = [
        min((value - lower).days // width_days, bin_count - 1) for value in values
    ]
    for index in indexes:
        counts[index] += 1
    return _with_scoped_counts(
        {
            "kind": "date",
            "min": lower.isoformat(),
            "max": upper.isoformat(),
            "bins": [
                {
                    "start": (lower + timedelta(days=index * width_days)).isoformat(),
                    "end": min(
                        upper,
                        lower + timedelta(days=(index + 1) * width_days - 1),
                    ).isoformat(),
                    "count": count,
                }
                for index, count in enumerate(counts)
            ],
        },
        _scoped_bin_counts(indexes, in_scope, bin_count),
    )


def _column_distribution(
    column_type: str,
    row_ids: list[int],
    raw_values: dict[int, Any],
    scope_row_ids: frozenset[int] | None = None,
) -> dict[str, Any] | None:
    """Histogram of the column; with a scope, also each bin's in-scope count."""
    value_kind = range_facet_value_kind(column_type)
    kept_rows: list[int] = []
    if value_kind == "integer":
        integer_values: list[int] = []
        for row_id in row_ids:
            raw = raw_values.get(row_id)
            if type(raw) is int and validate_value("integer", raw):
                integer_values.append(raw)
                kept_rows.append(row_id)
        return _integer_distribution(
            integer_values, _scope_mask(kept_rows, scope_row_ids)
        )
    if value_kind == "number":
        values: list[float] = []
        for row_id in row_ids:
            raw = raw_values.get(row_id)
            # Match the SQL range predicate exactly: JSON integer/real cells
            # only. Strings that merely look numeric and booleans are not
            # number cells and must not appear in the histogram.
            if type(raw) is not int and type(raw) is not float:
                continue
            value = float(raw)
            if math.isfinite(value):
                values.append(value)
                kept_rows.append(row_id)
        return _number_distribution(values, _scope_mask(kept_rows, scope_row_ids))
    if value_kind == "date":
        values_date: list[date] = []
        for row_id in row_ids:
            raw = raw_values.get(row_id)
            if raw is None:
                continue
            parsed = parse_utc_calendar_date(raw)
            if parsed is not None:
                values_date.append(parsed)
                kept_rows.append(row_id)
        return _date_distribution(values_date, _scope_mask(kept_rows, scope_row_ids))
    return None


def _scope_mask(
    row_ids: list[int], scope_row_ids: frozenset[int] | None
) -> list[bool] | None:
    if scope_row_ids is None:
        return None
    return [row_id in scope_row_ids for row_id in row_ids]


def _validated_limit(limit: Any) -> int:
    if limit is None:
        return COLUMN_VALUES_DEFAULT_LIMIT
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ColumnValuesPreviewError(
            "invalid_params", "limit must be an integer", field="limit"
        )
    if limit < 1:
        raise ColumnValuesPreviewError(
            "invalid_params", "limit must be >= 1", field="limit"
        )
    # over-asks clamp to the ceiling rather than erroring: the UI can always
    # request "as much as you'll give me" without tracking the server maximum
    return min(limit, COLUMN_VALUES_MAX_LIMIT)


def _validated_offset(offset: Any) -> int:
    if offset is None:
        return 0
    if isinstance(offset, bool) or not isinstance(offset, int):
        raise ColumnValuesPreviewError(
            "invalid_params", "offset must be an integer", field="offset"
        )
    if offset < 0:
        raise ColumnValuesPreviewError(
            "invalid_params", "offset must be >= 0", field="offset"
        )
    return offset


def _validated_search(search: Any) -> str | None:
    if search is None:
        return None
    if not isinstance(search, str):
        raise ColumnValuesPreviewError(
            "invalid_params", "search must be a string", field="search"
        )
    return search


def _scalar_list_selector(value: Any) -> tuple[str, dict[str, Any], str] | None:
    """Return a stable selector identity, wire selector, and display label.

    Generic JSON-list facets deliberately stop at scalar array items. Object
    shapes are data-dependent and have no safe universal comparison contract;
    marked entity mention columns get their own explicit projection below.
    """
    if isinstance(value, str):
        selector: dict[str, Any] = {"kind": "scalar", "value": value}
        return (
            json.dumps(selector, sort_keys=True, separators=(",", ":")),
            selector,
            value,
        )
    if isinstance(value, bool):
        selector = {"kind": "scalar", "value": value}
        return (
            json.dumps(selector, sort_keys=True, separators=(",", ":")),
            selector,
            "true" if value else "false",
        )
    if type(value) is int:
        if abs(value) > _MAX_SAFE_JAVASCRIPT_INTEGER:
            return None
        normalized: int | float = value
    elif type(value) is float:
        if not math.isfinite(value) or abs(value) > _MAX_SAFE_JAVASCRIPT_INTEGER:
            return None
        # JSON has one number value space in the browser: normalize integer
        # and real spellings so a returned selector remains replayable.
        normalized = int(value) if value.is_integer() else value
    else:
        return None
    selector = {"kind": "scalar", "value": normalized}
    return (
        json.dumps(selector, sort_keys=True, separators=(",", ":")),
        selector,
        json.dumps(normalized, separators=(",", ":")),
    )


def _is_strict_json_compatible(value: Any) -> bool:
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError, OverflowError, RecursionError):
        return False
    return True


def _list_facet(
    *,
    row_ids: list[int],
    raw_values: dict[int, Any],
    is_entity_mentions: bool,
    search: str | None,
    limit: int,
    offset: int,
) -> dict[str, Any]:
    """Element-level facet facts for one JSON column.

    Each selector is counted once per row, even if that element occurs more
    than once in the cell. Non-arrays and unsupported item shapes contribute
    no choices rather than guessing an object schema.
    """
    choices: dict[str, dict[str, Any]] = {}
    for row_id in row_ids:
        array = raw_values.get(row_id)
        if not isinstance(array, list) or not _is_strict_json_compatible(array):
            continue
        row_keys: set[str] = set()
        for item in array:
            if is_entity_mentions:
                if not isinstance(item, dict):
                    continue
                entity_type = item.get("type")
                text = item.get("text")
                if (
                    not isinstance(entity_type, str)
                    or not entity_type
                    or not isinstance(text, str)
                    or not text
                ):
                    continue
                selector = {"kind": "entity", "type": entity_type, "text": text}
                key = json.dumps(selector, sort_keys=True, separators=(",", ":"))
                label = text
            else:
                scalar = _scalar_list_selector(item)
                if scalar is None:
                    continue
                key, selector, label = scalar
            if key in row_keys:
                continue
            row_keys.add(key)
            choice = choices.setdefault(
                key,
                {"key": key, "label": label, "count": 0, "selector": selector},
            )
            choice["count"] += 1

    ordered = sorted(
        choices.values(), key=lambda item: (-item["count"], item["label"], item["key"])
    )
    if search is not None and search != "":
        needle = search.casefold()
        filtered = [item for item in ordered if needle in item["label"].casefold()]
    else:
        filtered = ordered
    page = filtered[offset : offset + limit]
    return {
        "distinct": len(choices),
        "offset": offset,
        "limit": limit,
        "truncated": offset + len(page) < len(filtered),
        "search": search,
        "choices": page,
    }


def _bound_response(payload: dict[str, Any]) -> None:
    """Fit complete selectable entries within the automatic preview budget."""
    values = payload["values"]
    payload["values"] = []
    facet = payload.get("list_facet")
    choices = facet["choices"] if facet is not None else []
    if facet is not None:
        facet["choices"] = []
    # Leave room for serialization defaults and response-model additions.
    remaining = (
        COLUMN_VALUES_RESPONSE_BYTE_LIMIT
        - 2048
        - len(json.dumps(payload, ensure_ascii=True).encode("utf-8"))
    )
    # Collection facets select members; whole-cell Resolve values are secondary.
    sections: list[tuple[list[Any], list[Any], dict[str, Any]]] = []
    if facet is not None:
        sections.append((choices, facet["choices"], facet))
    sections.append((values, payload["values"], payload))
    for entries, target, envelope in sections:
        for entry in entries:
            cost = len(json.dumps(entry, ensure_ascii=True).encode("utf-8")) + 2
            if cost > remaining:
                payload["inventory_complete"] = False
                envelope["truncated"] = True
                break
            target.append(entry)
            remaining -= cost


def resolve_column_values_preview(
    project: Project,
    *,
    sheet_id: Any,
    input_column: Any,
    search: Any = None,
    limit: Any = None,
    offset: Any = None,
    value_mode: Any = "exact",
    scope_row_ids: frozenset[int] | None = None,
) -> dict[str, Any]:
    """Compute the distinct-values payload for one column.

    ``values`` carries the (searchable, paged) distinct non-blank cell values
    sorted count desc then value asc; ``distinct`` / ``missing`` /
    ``total_rows`` are UNFILTERED facts about the whole column; ``value_hash``
    hashes the full column read (never the filtered page). Values are the
    EXACT cell surfaces (no stripping) because substitute/combine mapping keys
    match cells exactly; only null and blank/whitespace-only cells are set
    aside as ``missing``. Automatic ``bounded`` previews omit cells above the
    shared read limit and return no action hash. ``inventory_complete`` and
    ``omitted_rows`` make partial counts explicit; complete entries may also
    be paged earlier to fit the response budget. Exact authoring is unchanged.

    ``scope_row_ids`` (the rows a selection keeps) adds ``scope``: the scoped
    row total, the scoped count of each returned value in the same order, and
    each histogram bin's scoped count. The inventory itself stays unscoped so a
    value with no scoped matches is still listed.
    """
    with project.read_snapshot() as snapshot:
        return _resolve_column_values_preview_snapshot(
            snapshot,
            sheet_id=sheet_id,
            input_column=input_column,
            search=search,
            limit=limit,
            offset=offset,
            value_mode=value_mode,
            scope_row_ids=scope_row_ids,
        )


def _resolve_column_values_preview_snapshot(
    project: Project | ProjectReadSnapshot,
    *,
    sheet_id: Any,
    input_column: Any,
    search: Any = None,
    limit: Any = None,
    offset: Any = None,
    value_mode: Any = "exact",
    scope_row_ids: frozenset[int] | None = None,
) -> dict[str, Any]:
    if not isinstance(sheet_id, int) or isinstance(sheet_id, bool) or sheet_id < 1:
        raise ColumnValuesPreviewError(
            "invalid_input_ref",
            "column values preview requires a positive sheet_id",
            field="sheet_id",
        )
    if not isinstance(input_column, str) or not input_column.strip():
        raise ColumnValuesPreviewError(
            "invalid_input_ref",
            "column values preview requires a non-empty input_column",
            field="input_column",
        )
    if value_mode not in ("exact", "bounded"):
        raise ColumnValuesPreviewError(
            "invalid_params", "value_mode must be exact or bounded", field="value_mode"
        )
    column = input_column.strip()
    effective_search = _validated_search(search)
    effective_limit = _validated_limit(limit)
    effective_offset = _validated_offset(offset)

    require_visible_sheet(
        project, sheet_id, error=ColumnValuesPreviewError, label="column values preview"
    )
    column_row = require_visible_column(
        project,
        sheet_id,
        column,
        error=ColumnValuesPreviewError,
        label="column values preview",
    )
    column_id = int(column_row["id"])
    row_ids = [int(rid) for rid in project.visible_row_ids(sheet_id)]

    omitted_rows = 0
    if value_mode == "bounded":
        reads = read_current_column(
            project.db,
            sheet_id=sheet_id,
            column_id=column_id,
            row_ids=row_ids,
            bounded=True,
        )
        inspected_rows = [row_id for row_id in row_ids if reads[row_id].value_present]
        omitted_rows = len(row_ids) - len(inspected_rows)
        raw_values = {row_id: reads[row_id].value for row_id in inspected_rows}
        # Automatic facets are not an action-authoring snapshot. Never hash
        # omitted values or expose a partial hash as a legacy action guard.
        value_hash = None
    else:
        inspected_rows = row_ids
        raw_values = project.get_values(
            sheet_id, column_id, tolerate_decode_errors=True
        )
        value_hash = hash_column_values(row_ids, raw_values)

    counts: Counter[str] = Counter()
    scoped_counts: Counter[str] = Counter()
    missing = 0
    for row_id in inspected_rows:
        raw = raw_values.get(row_id)
        if raw is None:
            missing += 1
            continue
        surface = str(raw)
        if not surface.strip():
            missing += 1
            continue
        counts[surface] += 1
        if scope_row_ids is not None and row_id in scope_row_ids:
            scoped_counts[surface] += 1

    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    if effective_search is not None and effective_search != "":
        needle = effective_search.casefold()
        filtered = [item for item in ordered if needle in item[0].casefold()]
    else:
        filtered = ordered

    page = filtered[effective_offset : effective_offset + effective_limit]
    truncated = effective_offset + len(page) < len(filtered)

    payload = {
        "schema_version": COLUMN_VALUES_PREVIEW_SCHEMA_VERSION,
        "sheet_id": sheet_id,
        "column_id": column_id,
        "input_column": column,
        "total_rows": len(row_ids),
        "distinct": len(counts),
        "missing": missing,
        "values": [{"value": value, "count": count} for value, count in page],
        "offset": effective_offset,
        "limit": effective_limit,
        "truncated": truncated,
        "value_hash": value_hash,
        "search": effective_search,
        "distribution": _column_distribution(
            str(column_row["type"]), inspected_rows, raw_values, scope_row_ids
        ),
    }
    distribution = payload["distribution"]
    scoped_bins = (
        distribution.pop("scoped_counts", None) if distribution is not None else None
    )
    if column_row["type"] == "json":
        payload["list_facet"] = _list_facet(
            row_ids=inspected_rows,
            raw_values=raw_values,
            is_entity_mentions=column_semantic_type(column_row)
            == ENTITY_MENTIONS_SEMANTIC_TYPE,
            search=effective_search,
            limit=effective_limit,
            offset=effective_offset,
        )
    if value_mode == "bounded":
        payload["inventory_complete"] = omitted_rows == 0
        payload["omitted_rows"] = omitted_rows
        _bound_response(payload)
    if scope_row_ids is not None:
        payload["scope"] = {
            "total_rows": sum(1 for row_id in row_ids if row_id in scope_row_ids),
            "values": [
                {"value": entry["value"], "count": scoped_counts[entry["value"]]}
                for entry in payload["values"]
            ],
            "distribution_counts": scoped_bins,
        }
    return payload
