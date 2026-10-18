"""Bounded reader analysis and HTTP projection for table drafts."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any, Mapping, Sequence

from frisket.table_import import (
    ColumnStatsAccumulator,
    PreviewAccumulator,
    RawCell,
    TableReadSettings,
    convert_cell,
    count_records,
    open_table,
    read_prefix,
)


PREFIX_RECORDS = 64
PREVIEW_HEAD = 12
PREVIEW_COLUMNS = 8


def build_prefix_scan(
    source_path: Path, settings: TableReadSettings, *, settings_revision: int
) -> dict[str, Any]:
    prefix = read_prefix(source_path, settings, record_limit=PREFIX_RECORDS)
    preview = PreviewAccumulator(
        head_rows=PREVIEW_HEAD, tail_rows=2, column_limit=PREVIEW_COLUMNS
    )
    stats = ColumnStatsAccumulator(
        prefix.columns,
        decimal_separator=settings.decimal_separator,
        thousands_separator=settings.thousands_separator,
    )
    issues = zero_issue_counts()
    data_rows = 0
    for row in prefix.rows:
        preview.accept(row)
        stats.accept(row)
        data_rows += int(row.role == "data")
        count_issues(issues, row.issues)
    return _scan_payload(
        settings_revision=settings_revision,
        record_count=len(prefix.rows),
        data_rows=data_rows,
        columns=prefix.columns,
        preview=preview.snapshot(complete=False),
        stats=stats.snapshot(complete=False),
        issues=issues,
        encoding=settings.encoding,
    )


def build_complete_scan(
    source_path: Path, settings: TableReadSettings, *, settings_revision: int
) -> dict[str, Any]:
    total = count_records(source_path, settings) if settings.skip_rows_end else None
    preview = PreviewAccumulator(
        head_rows=PREVIEW_HEAD, tail_rows=2, column_limit=PREVIEW_COLUMNS
    )
    issues = zero_issue_counts()
    with open_table(source_path, settings, total_record_count=total) as reader:
        stats = ColumnStatsAccumulator(
            reader.columns,
            decimal_separator=settings.decimal_separator,
            thousands_separator=settings.thousands_separator,
        )
        record_count = data_rows = 0
        for row in reader:
            record_count = row.source.record_ordinal
            preview.accept(row)
            stats.accept(row)
            data_rows += int(row.role == "data")
            count_issues(issues, row.issues)
        return _scan_payload(
            settings_revision=settings_revision,
            record_count=record_count,
            data_rows=data_rows,
            columns=reader.columns,
            preview=preview.snapshot(complete=True),
            stats=stats.snapshot(complete=True),
            issues=issues,
            encoding=settings.encoding,
        )


def public_columns(
    scan: Mapping[str, Any],
    choices: Sequence[Mapping[str, Any]],
    settings: Mapping[str, Any],
) -> list[dict[str, Any]]:
    by_choice = {int(item["source_index"]): item for item in choices}
    result = []
    for stat in scan["stats"]["columns"]:
        source = stat["column"]
        choice = by_choice.get(
            int(source["index"]),
            {
                "included": stat["suggested_type"] != "omit",
                "target_type": stat["suggested_type"],
                "date_order": stat.get("suggested_date_order"),
            },
        )
        target = choice["target_type"]
        date_order = choice.get("date_order")
        counts = _chosen_counts(stat, target, date_order)
        samples = [
            conversion_json(
                value,
                target,
                date_order,
                decimal_separator=str(settings.get("decimal_separator", ".")),
                thousands_separator=settings.get("thousands_separator", ","),
            )
            for value in stat["sample_values"][:5]
        ]
        needs = [
            {"leading_zero": "leading_zeros"}.get(item, item)
            for item in stat["needs_review"]
        ]
        if stat["ambiguous_date_order"] and "ambiguous_date_order" not in needs:
            needs.append("ambiguous_date_order")
        if counts.get("time_dropped") and "time_dropped" not in needs:
            needs.append("time_dropped")
        result.append(
            {
                "source_index": source["index"],
                "name": source["name"],
                "original_name": source["original_name"],
                "name_status": source["name_status"],
                "included": bool(choice["included"]),
                "target_type": target,
                "date_order": date_order,
                "suggested_type": stat["suggested_type"],
                "ambiguous_date_order": stat["ambiguous_date_order"],
                "needs_review": needs,
                "samples": samples,
                "conversion_counts": counts,
                "stats_complete": bool(scan["stats"]["complete"]),
            }
        )
    return result


def default_choices(scan: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "source_index": int(item["column"]["index"]),
            "included": item["suggested_type"] != "omit",
            "target_type": item["suggested_type"],
            "date_order": item.get("suggested_date_order")
            or ("month_first" if item["ambiguous_date_order"] else None),
        }
        for item in scan["stats"]["columns"]
    ]


def zero_issue_counts() -> dict[str, int]:
    return {
        "extra_values": 0,
        "missing_values": 0,
        "malformed_records": 0,
        "unreadable_cells": 0,
    }


def count_issues(counts: dict[str, int], issues: Sequence[Any]) -> None:
    names = {
        "extra_values": "extra_values",
        "missing_values": "missing_values",
        "malformed_record": "malformed_records",
        "unreadable_cell": "unreadable_cells",
    }
    for issue in issues:
        counts[names[issue.code]] += int(getattr(issue, "count", 1))


def conversion_json(
    original: str,
    target: str,
    date_order: str | None,
    *,
    decimal_separator: str,
    thousands_separator: str | None,
) -> dict[str, Any]:
    converted = convert_cell(
        RawCell(original),
        target,
        date_order=date_order or "month_first",
        decimal_separator=decimal_separator,
        thousands_separator=thousands_separator,
    )
    return {
        "original": converted.original,
        "value": converted.value,
        "display": converted.display,
        "outcome": converted.outcome,
    }


def _scan_payload(
    *,
    settings_revision: int,
    record_count: int,
    data_rows: int,
    columns: Sequence[Any],
    preview: Any,
    stats: Any,
    issues: Mapping[str, int],
    encoding: str | None,
) -> dict[str, Any]:
    return {
        "settings_revision": settings_revision,
        "record_count": record_count,
        "data_rows": data_rows,
        "preview": _preview_json(columns, preview, issues, encoding),
        "stats": {
            "columns": [asdict(item) for item in stats.columns],
            "rows_checked": stats.rows_checked,
            "inference_rows_checked": stats.inference_rows_checked,
            "complete": stats.complete,
        },
        "issue_counts": dict(issues),
        "complete": bool(preview.complete),
        "error": None,
    }


def _preview_json(
    columns: Sequence[Any],
    preview: Any,
    issues: Mapping[str, int],
    encoding: str | None,
) -> dict[str, Any]:
    payload = asdict(preview)
    payload["columns"] = [asdict(item) for item in columns]
    payload["expected_values"] = len(columns)
    payload["notice"] = _notice(len(columns), issues, preview.complete, encoding)
    return payload


def _notice(
    width: int, issues: Mapping[str, int], complete: bool, encoding: str | None
) -> dict[str, Any]:
    if issues["extra_values"]:
        kind, count = "extra_values", issues["extra_values"]
    elif issues["unreadable_cells"]:
        kind, count = "unreadable_characters", issues["unreadable_cells"]
    elif width <= 1:
        kind, count = "one_column", None
    elif issues["missing_values"]:
        kind, count = "some_wrong_width", issues["missing_values"]
    elif not complete:
        kind, count = "counting", None
    else:
        kind, count = "consistent", None
    return {
        "kind": kind,
        "count": count,
        "common_values": None,
        "expected_values": width,
        "encoding": encoding,
    }


def _chosen_counts(
    stat: Mapping[str, Any], target: str, date_order: str | None
) -> dict[str, int]:
    by_order = stat.get("counts_by_date_order")
    if target in {"date", "datetime"} and date_order and isinstance(by_order, dict):
        order_counts = by_order.get(date_order)
        if isinstance(order_counts, dict) and target in order_counts:
            return dict(order_counts[target])
    counts = stat["counts_by_type"].get(target)
    return dict(counts) if isinstance(counts, dict) else _zero_conversion_counts()


def _zero_conversion_counts() -> dict[str, int]:
    return {
        "rows_checked": 0,
        "valid": 0,
        "blank": 0,
        "changed": 0,
        "invalid": 0,
        "leading_zero_loss": 0,
        "time_dropped": 0,
    }


__all__ = [
    "build_complete_scan",
    "build_prefix_scan",
    "default_choices",
    "public_columns",
    "zero_issue_counts",
]
