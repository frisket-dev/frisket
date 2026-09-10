"""Shared source-bound temporal selections and admitted media reads."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any, ClassVar, Protocol
from collections.abc import Iterable

from pydantic import BaseModel, Field

from frisket.actions.types import ColumnRef, DynamicTableResult, SheetRows, Row
from frisket.features.temporal_values import TimelinePointsValue, TimelineRangesValue
from frisket.contracts.actions.schemas.temporal import (
    TemporalColumnSelection,
    TemporalDraftPointsSelection,
    TemporalDraftPoint,
    TemporalDraftRange,
    TemporalDraftRangeSelection,
    TemporalDraftRangesSelection,
    TemporalTypedValueSelection,
)


class TemporalMediaColumn(ColumnRef[Any]):
    accepted_column_types: ClassVar[tuple[str, ...]] = ("audio", "video")


class TemporalSelectionColumn(ColumnRef[Any]):
    accepted_column_types: ClassVar[tuple[str, ...]] = (
        "timeline_point",
        "timeline_points",
        "timeline_range",
        "timeline_ranges",
    )


class TranscriptColumnSelection(TemporalColumnSelection):
    column: TemporalSelectionColumn


class TranscriptRangeSelection(TemporalDraftRangeSelection):
    repeat_for_rows: bool = Field(default=False, strict=True)


class TranscriptPointsSelection(TemporalDraftPointsSelection):
    items: list[TemporalDraftPoint] = Field(min_length=1)
    repeat_for_rows: bool = Field(default=False, strict=True)


class TranscriptRangesSelection(TemporalDraftRangesSelection):
    items: list[TemporalDraftRange] = Field(min_length=1)
    repeat_for_rows: bool = Field(default=False, strict=True)


class TranscriptValueSelection(TemporalTypedValueSelection):
    repeat_for_rows: bool = Field(default=False, strict=True)


TranscriptSelection = Annotated[
    TranscriptColumnSelection
    | TranscriptRangeSelection
    | TranscriptPointsSelection
    | TranscriptRangesSelection
    | TranscriptValueSelection,
    Field(discriminator="kind"),
]

_LITERAL_SELECTIONS = (
    TranscriptRangeSelection,
    TranscriptPointsSelection,
    TranscriptRangesSelection,
    TranscriptValueSelection,
)


def validate_transcript_selection_scope(value: Any, scope: SheetRows) -> None:
    """Apply the literal-repeat rule wherever this semantic value is declared."""
    if not isinstance(scope, SheetRows):
        raise ValueError("transcript tables require a sheet_rows scope")
    if isinstance(value, _LITERAL_SELECTIONS):
        if scope.row_ids is None:
            raise ValueError("literal transcript selections require explicit rows")
        if len(scope.row_ids) > 1 and not value.repeat_for_rows:
            raise ValueError("literal_selection_requires_confirmation")
    elif isinstance(value, BaseModel):
        for name in type(value).model_fields:
            validate_transcript_selection_scope(getattr(value, name), scope)
    elif isinstance(value, dict):
        for item in value.values():
            validate_transcript_selection_scope(item, scope)
    elif isinstance(value, (list, tuple)):
        for item in value:
            validate_transcript_selection_scope(item, scope)


@dataclass(frozen=True, eq=False)
class TemporalMediaValue:
    """Invocation-owned staged clip or dependent grounded value."""


class TemporalMediaReader(Protocol):
    def read(
        self, source: TemporalMediaColumn, selection: TranscriptSelection
    ) -> DynamicTableResult:
        """Stage exact media segments, preserving their temporal evidence."""
        ...


class VisualCutsReader(Protocol):
    async def read(self, row: Row, source: ColumnRef[Any]) -> TimelinePointsValue:
        """Find local visual cuts in the admitted video cell, retaining its clock."""
        ...


class TopicSectionsReader(Protocol):
    async def read(
        self, row: Row, source: ColumnRef[Any], *, engine: str, settings: dict[str, Any]
    ) -> TimelineRangesValue:
        """Analyze an admitted transcript with the exact selected local engine."""
        ...


def topic_sidecar_names(keys: Iterable[str]) -> dict[str, str]:
    """Host-owned column names, independent of the reporter's output names."""
    from frisket.features.topic_segmentation.contracts import (
        TOPIC_ANALYSIS_SIDECAR_COLUMN,
    )

    keys = tuple(keys)
    return {
        key: TOPIC_ANALYSIS_SIDECAR_COLUMN
        if len(keys) == 1
        else f"{TOPIC_ANALYSIS_SIDECAR_COLUMN}_{key}"
        for key in keys
    }
