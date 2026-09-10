"""Ordinary key-equality joins, materialized by the table host."""

from pydantic import Field

from frisket.actions.core import ActionCategory, action, create_sheet
from frisket.actions.join_types import (
    JoinedTablesReader,
    JoinColumnPick,
    JoinKeyPair,
    JoinMode,
)
from frisket.actions.types import ActionParams, DynamicTableResult, SheetRef


class JoinParams(ActionParams):
    right: SheetRef
    join_keys: list[JoinKeyPair] = Field(min_length=1, max_length=8)
    how: JoinMode = "inner"
    columns: list[JoinColumnPick] | None = Field(default=None, min_length=1)
    indicator: bool = Field(default=False, strict=True)
    max_output_rows: int = Field(default=1_000_000, gt=0, strict=True)


def join(params: JoinParams, tables: JoinedTablesReader) -> DynamicTableResult:
    return tables.read(
        params.right,
        join_keys=params.join_keys,
        how=params.how,
        columns=params.columns,
        indicator=params.indicator,
        max_output_rows=params.max_output_rows,
    )


JOIN = action(
    name="join",
    title="Join tables",
    description="Match exact keys from two sheets and create a joined table.",
    category=ActionCategory.CONVERT,
    run=create_sheet(join),
    examples=(
        JoinParams(
            right=SheetRef(sheet_id=2),
            join_keys=[JoinKeyPair(left_column="id", right_column="id")],
        ),
    ),
)
