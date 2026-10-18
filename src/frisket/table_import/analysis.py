"""Bounded preview and column-statistics accumulator signatures."""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
import re

from .models import (
    CellConversion,
    ColumnStats,
    ColumnStatsSnapshot,
    ConversionCounts,
    PreviewRow,
    PreviewSnapshot,
    RawCell,
    SourceColumn,
    TableRow,
    TargetType,
)
from .conversion import convert_cell


class PreviewAccumulator:
    def __init__(
        self, *, head_rows: int, tail_rows: int = 2, column_limit: int = 8
    ) -> None:
        if head_rows < 0 or tail_rows < 0 or column_limit < 1:
            raise ValueError("preview bounds are invalid")
        self._head_rows = head_rows
        self._tail_rows = tail_rows
        self._column_limit = column_limit
        self._head: list[PreviewRow] = []
        self._tail: deque[PreviewRow] = deque(maxlen=tail_rows or None)
        self._rows_seen = 0

    def accept(self, row: TableRow) -> None:
        preview = PreviewRow(
            role=row.role,
            record_ordinal=row.source.record_ordinal,
            physical_line_start=row.source.physical_line_start,
            physical_line_end=row.source.physical_line_end,
            cells=tuple(cell.text for cell in row.cells[: self._column_limit]),
            hidden_cell_count=max(0, len(row.cells) - self._column_limit),
            issues=row.issues,
        )
        self._rows_seen += 1
        if len(self._head) < self._head_rows:
            self._head.append(preview)
        elif self._tail_rows:
            self._tail.append(preview)

    def snapshot(self, *, complete: bool) -> PreviewSnapshot:
        tail = tuple(self._tail) if complete else ()
        omitted = (
            max(0, self._rows_seen - len(self._head) - len(tail)) if complete else None
        )
        return PreviewSnapshot(
            head=tuple(self._head),
            tail=tail,
            rows_seen=self._rows_seen,
            omitted_rows=omitted,
            complete=complete,
            tail_pending=not complete,
        )


@dataclass
class _MutableCounts:
    rows_checked: int = 0
    valid: int = 0
    blank: int = 0
    changed: int = 0
    invalid: int = 0
    leading_zero_loss: int = 0
    time_dropped: int = 0

    def freeze(self) -> ConversionCounts:
        return ConversionCounts(**vars(self))


_ANALYZED_TYPES: tuple[TargetType, ...] = (
    "text",
    "integer",
    "number",
    "date",
    "datetime",
    "boolean",
)


class ColumnStatsAccumulator:
    def __init__(
        self,
        columns: Sequence[SourceColumn],
        *,
        inference_limit: int = 100_000,
        preview_values: int = 5,
        decimal_separator: str = ".",
        thousands_separator: str | None = ",",
    ) -> None:
        if inference_limit < 1 or preview_values < 0:
            raise ValueError("column-stat bounds are invalid")
        self._columns = tuple(columns)
        self._inference_limit = inference_limit
        self._preview_values = preview_values
        self._decimal_separator = decimal_separator
        self._thousands_separator = thousands_separator
        self._rows_checked = 0
        self._samples: list[list[str]] = [[] for _ in self._columns]
        self._full = [
            {target: _MutableCounts() for target in _ANALYZED_TYPES}
            for _ in self._columns
        ]
        self._inference = [
            {target: _MutableCounts() for target in _ANALYZED_TYPES}
            for _ in self._columns
        ]
        self._leading_zero = [False for _ in self._columns]
        self._long_integer = [False for _ in self._columns]
        self._saw_time = [False for _ in self._columns]
        self._saw_naive_datetime = [False for _ in self._columns]
        self._saw_aware_datetime = [False for _ in self._columns]
        self._ambiguous_date = [False for _ in self._columns]
        self._clear_date = [False for _ in self._columns]

    def accept(self, row: TableRow) -> None:
        if row.role != "data":
            return
        if len(row.cells) != len(self._columns):
            raise ValueError("row width does not match analyzed columns")
        self._rows_checked += 1
        in_inference = self._rows_checked <= self._inference_limit
        for position, cell in enumerate(row.cells):
            text = cell.text
            if text and len(self._samples[position]) < self._preview_values:
                self._samples[position].append(text)
            if in_inference and _has_leading_zero_integer(text):
                self._leading_zero[position] = True
            if in_inference and _is_long_integer(text):
                self._long_integer[position] = True
            if in_inference and (cell.kind == "datetime" or _has_time(text)):
                self._saw_time[position] = True
                awareness = _datetime_awareness(text)
                self._saw_naive_datetime[position] |= awareness is False
                self._saw_aware_datetime[position] |= awareness is True
            if in_inference:
                date_shape = _numeric_date_shape(text)
                self._ambiguous_date[position] |= date_shape == "ambiguous"
                self._clear_date[position] |= date_shape == "clear"
            for target in _ANALYZED_TYPES:
                converted = convert_cell(
                    cell,
                    target,
                    decimal_separator=self._decimal_separator,
                    thousands_separator=self._thousands_separator,
                )
                _record(
                    self._full[position][target],
                    converted,
                    cell,
                    target=target,
                )
                if in_inference:
                    _record(
                        self._inference[position][target],
                        converted,
                        cell,
                        target=target,
                    )

    def snapshot(self, *, complete: bool) -> ColumnStatsSnapshot:
        columns: list[ColumnStats] = []
        for position, column in enumerate(self._columns):
            mixed_datetime_awareness = (
                self._saw_naive_datetime[position]
                and self._saw_aware_datetime[position]
            )
            suggested = _suggest_type(
                self._inference[position],
                leading_zero=self._leading_zero[position],
                long_integer=self._long_integer[position],
                saw_time=self._saw_time[position],
                mixed_datetime_awareness=mixed_datetime_awareness,
            )
            needs_review: list[str] = []
            if self._leading_zero[position]:
                needs_review.append("leading_zero")
            if self._long_integer[position]:
                needs_review.append("long_integer")
            if mixed_datetime_awareness:
                needs_review.append("mixed_datetime_awareness")
            suggested_counts = self._full[position].get(suggested)
            if suggested_counts is not None and suggested_counts.invalid:
                needs_review.append("invalid_values")
            columns.append(
                ColumnStats(
                    column=column,
                    sample_values=tuple(self._samples[position]),
                    suggested_type=suggested,
                    needs_review=tuple(needs_review),
                    ambiguous_date_order=(
                        self._ambiguous_date[position]
                        and not self._clear_date[position]
                    ),
                    counts_by_type={
                        target: counts.freeze()
                        for target, counts in self._full[position].items()
                    },
                )
            )
        return ColumnStatsSnapshot(
            columns=tuple(columns),
            rows_checked=self._rows_checked,
            inference_rows_checked=min(self._rows_checked, self._inference_limit),
            complete=complete,
        )


def _record(
    counts: _MutableCounts,
    converted: CellConversion,
    cell: RawCell,
    *,
    target: TargetType,
) -> None:
    counts.rows_checked += 1
    outcome = converted.outcome
    if outcome == "blank":
        counts.blank += 1
    elif outcome == "invalid":
        counts.invalid += 1
    else:
        counts.valid += 1
        if outcome == "changed":
            counts.changed += 1
    if outcome == "changed" and _has_leading_zero_integer(cell.text):
        counts.leading_zero_loss += 1
    if (
        target == "date"
        and outcome != "invalid"
        and (cell.kind == "datetime" or _has_time(cell.text))
    ):
        counts.time_dropped += 1


def _has_leading_zero_integer(text: str) -> bool:
    value = text.strip()
    if value[:1] in {"+", "-"}:
        value = value[1:]
    return len(value) > 1 and value.isdigit() and value.startswith("0")


def _is_long_integer(text: str) -> bool:
    value = text.strip().lstrip("+-")
    return value.isdigit() and len(value) > 15


def _suggest_type(
    counts: dict[TargetType, _MutableCounts],
    *,
    leading_zero: bool,
    long_integer: bool,
    saw_time: bool,
    mixed_datetime_awareness: bool,
) -> TargetType:
    nonblank = counts["text"].valid
    if nonblank == 0:
        return "omit"
    if leading_zero or long_integer or mixed_datetime_awareness:
        return "text"
    boolean = counts["boolean"]
    integer = counts["integer"]
    if boolean.invalid == 0 and integer.invalid > 0:
        return "boolean"
    candidates: tuple[TargetType, ...] = (
        "integer",
        "number",
        "date",
        "datetime",
        "boolean",
    )
    if saw_time and counts["datetime"].valid > counts["datetime"].invalid:
        return "datetime"
    best = max(candidates, key=lambda target: counts[target].valid)
    return best if counts[best].valid > counts[best].invalid else "text"


def _has_time(text: str) -> bool:
    value = text.strip()
    return "T" in value or bool(re.search(r"\b\d{1,2}:\d{2}\b", value))


def _datetime_awareness(text: str) -> bool | None:
    try:
        parsed = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.utcoffset() is not None


def _numeric_date_shape(text: str) -> str | None:
    value = text.strip().split(" ", 1)[0]
    separator = "/" if "/" in value else "-" if "-" in value else None
    if separator is None:
        return None
    parts = value.split(separator)
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        return None
    first, second = int(parts[0]), int(parts[1])
    if len(parts[0]) == 4:
        return "clear"
    return "ambiguous" if first <= 12 and second <= 12 else "clear"
