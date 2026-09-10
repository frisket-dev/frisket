"""Preparation without rendering or output publication; source facts may be cached."""

from dataclasses import dataclass
from typing import Annotated, Protocol

from pydantic import BaseModel, Field

from frisket.actions.temporal_types import (
    TemporalMediaColumn,
    TranscriptColumnSelection,
    TranscriptRangeSelection,
    TranscriptValueSelection,
)


TemporalExtractSelection = Annotated[
    TranscriptColumnSelection | TranscriptRangeSelection | TranscriptValueSelection,
    Field(discriminator="kind"),
]


@dataclass(frozen=True, eq=False)
class PreparedTemporalExtract:
    """An invocation-owned plan; only its issuing host may publish it."""

    selected_row_count: int


class ExtractedRanges(BaseModel):
    row_count: int
    column_count: int


class TemporalExtractor(Protocol):
    def prepare(
        self, source: TemporalMediaColumn, selection: TemporalExtractSelection
    ) -> PreparedTemporalExtract:
        """Bind ranges without rendering/output publication; may cache source facts."""
        ...
