"""Post-read conversion API; implementations never mutate source text."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from .models import (
    CellConversion,
    DateOrder,
    RawCell,
    RowIssue,
    SourceColumn,
    TargetType,
)


def convert_cell(
    cell: RawCell,
    target_type: TargetType,
    *,
    decimal_separator: str = ".",
    thousands_separator: str | None = ",",
    date_order: DateOrder = "month_first",
) -> CellConversion:
    raise NotImplementedError


def convert_row(
    cells: Sequence[RawCell],
    columns: Sequence[SourceColumn],
    target_types: Mapping[int, TargetType],
    *,
    decimal_separator: str = ".",
    thousands_separator: str | None = ",",
    date_orders: Mapping[int, DateOrder] | None = None,
) -> tuple[dict[str, str | int | float | bool | None], tuple[RowIssue, ...]]:
    raise NotImplementedError
