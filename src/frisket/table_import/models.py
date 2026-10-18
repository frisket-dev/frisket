"""Value objects shared by table import readers and their consumers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypeAlias

SourceKind: TypeAlias = Literal["delimited", "xlsx", "xls", "ods"]
CellKind: TypeAlias = Literal[
    "blank", "text", "number", "date", "datetime", "boolean", "error"
]
RowRole: TypeAlias = Literal["skipped_start", "header", "data", "blank", "skipped_end"]
TargetType: TypeAlias = Literal[
    "text", "integer", "number", "date", "datetime", "boolean", "omit"
]
DateOrder: TypeAlias = Literal["month_first", "day_first"]
ConversionOutcome: TypeAlias = Literal["unchanged", "changed", "blank", "invalid"]


@dataclass(frozen=True)
class TableReadSettings:
    """Fully resolved settings used identically by preview and execution."""

    kind: SourceKind
    delimiter: str | None = None
    encoding: str | None = None
    decimal_separator: Literal[".", ","] = "."
    thousands_separator: str | None = ","
    worksheet: str | None = None
    skip_rows_start: int = 0
    first_row_is_header: bool = True
    skip_rows_end: int = 0

    def __post_init__(self) -> None:
        if self.kind == "delimited":
            if self.delimiter is None or len(self.delimiter) != 1:
                raise ValueError("delimited input requires a one-character delimiter")
            if not self.encoding:
                raise ValueError("delimited input requires an encoding")
            if self.worksheet is not None:
                raise ValueError("delimited input does not have a worksheet")
        elif self.delimiter is not None or self.encoding is not None:
            raise ValueError("workbook input does not use delimiter or encoding")
        if self.skip_rows_start < 0 or self.skip_rows_end < 0:
            raise ValueError("skipped row counts cannot be negative")
        if self.thousands_separator is not None and len(self.thousands_separator) != 1:
            raise ValueError("thousands separator must be one character")
        if self.thousands_separator == self.decimal_separator:
            raise ValueError("decimal and thousands separators must differ")


@dataclass(frozen=True)
class SheetInfo:
    index: int
    name: str
    hidden: bool
    empty: bool


@dataclass(frozen=True)
class TableSourceInfo:
    kind: SourceKind
    sheets: tuple[SheetInfo, ...] = ()
    detected_delimiter: str | None = None
    detected_encoding: str | None = None
    detected_decimal_separator: Literal[".", ","] | None = None
    detected_thousands_separator: str | None = None


@dataclass(frozen=True)
class RawCell:
    """One source cell before the user-selected type is applied."""

    text: str
    kind: CellKind = "text"


@dataclass(frozen=True)
class RowIssue:
    code: Literal[
        "extra_values",
        "missing_values",
        "malformed_record",
        "unreadable_cell",
    ]
    message: str
    original_values: tuple[str, ...] = ()


@dataclass(frozen=True)
class RawRow:
    """One logical parser record with its physical source location."""

    record_ordinal: int
    physical_line_start: int | None
    physical_line_end: int | None
    cells: tuple[RawCell, ...]
    issues: tuple[RowIssue, ...] = ()


@dataclass(frozen=True)
class SourceColumn:
    """A stable, zero-based source column and its normalized name."""

    index: int
    name: str
    original_name: str | None
    name_status: Literal["original", "duplicate", "missing"]


@dataclass(frozen=True)
class TableRow:
    role: RowRole
    source: RawRow
    cells: tuple[RawCell, ...]
    extra_values: tuple[RawCell, ...] = ()
    issues: tuple[RowIssue, ...] = ()


@dataclass(frozen=True)
class ConversionIssue:
    code: str
    message: str
    original: str


@dataclass(frozen=True)
class CellConversion:
    original: str
    value: str | int | float | bool | None
    display: str
    outcome: ConversionOutcome
    issue: ConversionIssue | None = None


@dataclass(frozen=True)
class ConversionCounts:
    rows_checked: int = 0
    valid: int = 0
    blank: int = 0
    changed: int = 0
    invalid: int = 0
    leading_zero_loss: int = 0
    time_dropped: int = 0


@dataclass(frozen=True)
class ColumnStats:
    column: SourceColumn
    sample_values: tuple[str, ...]
    suggested_type: TargetType
    needs_review: tuple[str, ...]
    ambiguous_date_order: bool
    counts_by_type: dict[TargetType, ConversionCounts]


@dataclass(frozen=True)
class ColumnStatsSnapshot:
    columns: tuple[ColumnStats, ...]
    rows_checked: int
    inference_rows_checked: int
    complete: bool


@dataclass(frozen=True)
class PreviewRow:
    role: RowRole
    record_ordinal: int
    physical_line_start: int | None
    physical_line_end: int | None
    cells: tuple[str, ...]
    hidden_cell_count: int
    issues: tuple[RowIssue, ...]


@dataclass(frozen=True)
class PreviewSnapshot:
    head: tuple[PreviewRow, ...]
    tail: tuple[PreviewRow, ...]
    rows_seen: int
    omitted_rows: int | None
    complete: bool
    tail_pending: bool
