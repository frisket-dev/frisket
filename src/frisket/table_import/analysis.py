"""Bounded preview and column-statistics accumulator signatures."""

from __future__ import annotations

from collections.abc import Sequence

from .models import (
    ColumnStatsSnapshot,
    PreviewSnapshot,
    SourceColumn,
    TableRow,
)


class PreviewAccumulator:
    def __init__(
        self, *, head_rows: int, tail_rows: int = 2, column_limit: int = 8
    ) -> None:
        raise NotImplementedError

    def accept(self, row: TableRow) -> None:
        raise NotImplementedError

    def snapshot(self, *, complete: bool) -> PreviewSnapshot:
        raise NotImplementedError


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
        raise NotImplementedError

    def accept(self, row: TableRow) -> None:
        raise NotImplementedError

    def snapshot(self, *, complete: bool) -> ColumnStatsSnapshot:
        raise NotImplementedError
