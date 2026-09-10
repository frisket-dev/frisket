"""Pure exact-key equality, cardinality and deterministic lazy row expansion."""

from __future__ import annotations

from collections import Counter
from math import isfinite
from typing import Any

from frisket.contracts.action import canonical_column_type

_NUMERIC_TYPES = {"number", "integer"}
_NULL_KEY = object()


def _pair_mode(left_type: str, right_type: str) -> str:
    left = canonical_column_type(left_type)
    right = canonical_column_type(right_type)
    if left in _NUMERIC_TYPES and right in _NUMERIC_TYPES:
        return "numeric"
    if left == right and left == "boolean":
        return "boolean"
    if left == right:
        return "text"
    return "cross"  # cross-type → string fallback (warned)


def _normalize_key_cell(value: Any, mode: str) -> Any:
    if value is None:
        return _NULL_KEY
    if mode == "numeric":
        if isinstance(value, bool):
            return _NULL_KEY
        try:
            number = float(value)
        except (TypeError, ValueError):
            return _NULL_KEY
        if not isfinite(number):
            return _NULL_KEY
        return ("n", number)
    if mode == "boolean":
        if isinstance(value, bool):
            return ("b", value)
        return _NULL_KEY
    # text / date / category / json / cross-type → exact string form
    return ("s", str(value))


def _row_key(
    row_id: int,
    values_by_col: dict[int, dict[int, Any]],
    key_specs: list[dict[str, Any]],
    side: str,
) -> tuple | None:
    """Normalized multi-key tuple for one row, or None if any key is null
    (AND semantics and nulls never match)."""
    cells: list[Any] = []
    for spec in key_specs:
        col_id = spec[f"{side}_col_id"]
        raw = values_by_col[col_id].get(row_id)
        normalized = _normalize_key_cell(raw, spec["mode"])
        if normalized is _NULL_KEY:
            return None
        cells.append(normalized)
    return tuple(cells)


def _widen_type(left_type: str, right_type: str) -> str:
    left = canonical_column_type(left_type)
    right = canonical_column_type(right_type)
    if left == right:
        return left
    if left in _NUMERIC_TYPES and right in _NUMERIC_TYPES:
        return "number"
    return "text"


def _side_read_ids(
    key_specs: list[dict[str, Any]],
    output_columns: list[dict[str, Any]],
    side: str,
) -> list[int]:
    ids: list[int] = []
    for spec in key_specs:
        ids.append(spec[f"{side}_col_id"])
    for col in output_columns:
        if col["role"] == side and "source_col_id" in col:
            ids.append(col["source_col_id"])
    seen: set[int] = set()
    ordered: list[int] = []
    for col_id in ids:
        if col_id not in seen:
            seen.add(col_id)
            ordered.append(col_id)
    return ordered


def _estimated_rows(
    how: str, inner_rows: int, left_unmatched: int, right_unmatched: int
) -> int:
    if how == "inner":
        return inner_rows
    if how == "left":
        return inner_rows + left_unmatched
    if how == "right":
        return inner_rows + right_unmatched
    return inner_rows + left_unmatched + right_unmatched  # outer


def _top_fanout_keys(
    shared_keys: set,
    left_keys: list[tuple[int, Any]],
    right_index: dict[tuple, list[int]],
    limit: int = 10,
) -> list[dict[str, Any]]:
    left_counts: dict[Any, int] = {}
    for _rid, key in left_keys:
        if key is not None:
            left_counts[key] = left_counts.get(key, 0) + 1
    ranked = sorted(
        (
            {
                "key": _key_repr(key),
                "left": left_counts.get(key, 0),
                "right": len(right_index[key]),
                "rows": left_counts.get(key, 0) * len(right_index[key]),
            }
            for key in shared_keys
        ),
        key=lambda item: (-item["rows"], item["key"]),
    )
    return ranked[:limit]


def _key_repr(key: tuple) -> str:
    return "|".join(str(cell[1]) for cell in key)


def join_counts(left_keys, right_keys, how):
    left_counts = Counter(key for _, key in left_keys if key is not None)
    right_index = {}
    for row_id, key in right_keys:
        if key is not None:
            right_index.setdefault(key, []).append(row_id)
    shared = left_counts.keys() & right_index.keys()
    both = sum(left_counts[key] * len(right_index[key]) for key in shared)
    left_only = sum(key is None or key not in right_index for _, key in left_keys)
    right_only = sum(key is None or key not in left_counts for _, key in right_keys)
    stats = {
        "both": both,
        "left_only": left_only if how in ("left", "outer") else 0,
        "right_only": right_only if how in ("right", "outer") else 0,
        "matched_pairs": both,
        "distinct_matched_keys": len(shared),
        "max_fanout": max(
            (left_counts[key] * len(right_index[key]) for key in shared), default=0
        ),
        "null_left_rows": sum(key is None for _, key in left_keys),
        "null_right_rows": sum(key is None for _, key in right_keys),
    }
    return (
        right_index,
        set(left_counts),
        stats,
        _estimated_rows(how, both, left_only, right_only),
    )


def iter_join_records(
    *,
    how,
    output_columns,
    left_keys,
    right_keys,
    right_index,
    left_key_set,
    left_vals,
    right_vals,
):
    """Match the historical row-ID order without buffering the Cartesian product."""

    def record(left_row_id, right_row_id, merge):
        values = {}
        for column in output_columns:
            role = column["role"]
            if role == "key":
                left = left_vals[column["left_col_id"]].get(left_row_id)
                right = right_vals[column["right_col_id"]].get(right_row_id)
                value = left if left is not None else right
                if column["type"] == "text" and value is not None:
                    # Cross-type equality uses string form. Publish the widened
                    # key in that same form, without changing projected cells.
                    value = str(value)
            elif role == "indicator":
                value = merge
            else:
                value = (left_vals if role == "left" else right_vals)[
                    column["source_col_id"]
                ].get(left_row_id if role == "left" else right_row_id)
            values[column["name"]] = value
        return {
            "left_row_id": left_row_id,
            "right_row_id": right_row_id,
            "merge": merge,
            "values": values,
        }

    for left_row_id, key in sorted(left_keys):
        matches = right_index.get(key, ()) if key is not None else ()
        if matches:
            for right_row_id in sorted(matches):
                yield record(left_row_id, right_row_id, "both")
        elif how in ("left", "outer"):
            yield record(left_row_id, None, "left_only")
    if how in ("right", "outer"):
        for right_row_id, key in sorted(right_keys):
            if key is None or key not in left_key_set:
                yield record(None, right_row_id, "right_only")
