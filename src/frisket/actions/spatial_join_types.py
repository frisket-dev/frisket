"""Point-in-polygon spatial joins over two admitted sheet snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from frisket.actions.join_types import JoinColumnName
from frisket.actions.types import (
    ColumnRef,
    DynamicTableResult,
    GeoPoint,
    SheetRef,
)

SpatialJoinMode = Literal["left", "inner"]
SpatialJoinMatch = Literal["all", "first"]


@dataclass(frozen=True, slots=True)
class SpatialJoinSpec:
    """Structured parameters passed to an admitted ``SpatialJoinReader``."""

    point_column: ColumnRef[GeoPoint] | str
    right_sheet: SheetRef
    shape_column: JoinColumnName
    how: SpatialJoinMode = "left"
    match: SpatialJoinMatch = "all"
    right_columns: list[JoinColumnName] | None = None
    indicator: bool = False
    max_output_rows: int = 1_000_000


class SpatialJoinReader(Protocol):
    def read(self, spec: SpatialJoinSpec) -> DynamicTableResult: ...
