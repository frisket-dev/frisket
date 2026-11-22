"""Point-in-polygon spatial joins, materialized by the table host."""

from __future__ import annotations

from pydantic import Field, field_validator

from frisket.actions.core import ActionCategory, action, create_sheet
from frisket.actions.join_types import JoinColumnName
from frisket.actions.spatial_join_types import (
    SpatialJoinMatch,
    SpatialJoinMode,
    SpatialJoinReader,
    SpatialJoinSpec,
)
from frisket.actions.types import (
    ActionParams,
    ColumnRef,
    DynamicTableResult,
    GeoPoint,
    SheetRef,
)


class SpatialJoinParams(ActionParams):
    point_column: ColumnRef[GeoPoint]
    right_sheet: SheetRef
    shape_column: JoinColumnName
    how: SpatialJoinMode = "left"
    match: SpatialJoinMatch = "all"
    right_columns: list[JoinColumnName] | None = Field(default=None, min_length=1)
    indicator: bool = Field(default=False, strict=True)
    max_output_rows: int = Field(default=1_000_000, gt=0, strict=True)

    @field_validator("right_columns")
    @classmethod
    def _unique_right_columns(
        cls, value: list[JoinColumnName] | None
    ) -> list[JoinColumnName] | None:
        if value is not None and len(set(value)) != len(value):
            raise ValueError("right_columns must not contain duplicates")
        return value


def spatial_join(
    params: SpatialJoinParams, tables: SpatialJoinReader
) -> DynamicTableResult:
    return tables.read(
        SpatialJoinSpec(
            point_column=params.point_column,
            right_sheet=params.right_sheet,
            shape_column=params.shape_column,
            how=params.how,
            match=params.match,
            right_columns=params.right_columns,
            indicator=params.indicator,
            max_output_rows=params.max_output_rows,
        )
    )


SPATIAL_JOIN = action(
    name="spatial_join",
    title="Spatial join (point in polygon)",
    description=(
        "Join each row's geo_point column against a target sheet's geo_shape "
        "polygon column to create a new sheet containing matched polygon attributes."
    ),
    category=ActionCategory.CONVERT,
    run=create_sheet(spatial_join),
    examples=(
        SpatialJoinParams(
            point_column=ColumnRef[GeoPoint]("location"),
            right_sheet=SheetRef(sheet_id=2),
            shape_column="geometry",
        ),
    ),
)
