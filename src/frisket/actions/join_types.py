"""Exact joins over two admitted sheet snapshots."""

from typing import Annotated, Literal, Protocol

from pydantic import StringConstraints

from frisket.actions.types import ActionParams, DynamicTableResult, SheetRef

JoinColumnName = Annotated[
    str, StringConstraints(strict=True, min_length=1, pattern=r"\S")
]
JoinMode = Literal["inner", "left", "right", "outer"]


class JoinKeyPair(ActionParams):
    left_column: JoinColumnName
    right_column: JoinColumnName


class JoinColumnPick(ActionParams):
    side: Literal["left", "right"]
    column: JoinColumnName


class JoinedTablesReader(Protocol):
    def read(
        self,
        right: SheetRef,
        *,
        join_keys: list[JoinKeyPair],
        how: JoinMode = "inner",
        columns: list[JoinColumnPick] | None = None,
        indicator: bool = False,
        max_output_rows: int = 1_000_000,
    ) -> DynamicTableResult: ...
