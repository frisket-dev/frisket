"""Transcript columns and invocation-owned projection capabilities."""

from dataclasses import dataclass
from typing import ClassVar, Protocol

from frisket.actions.types import ColumnRef, DynamicTableResult
from frisket.actions.temporal_types import (  # noqa: F401 -- public shared selections
    TemporalSelectionColumn,
    TranscriptColumnSelection,
    TranscriptRangeSelection,
    TranscriptPointsSelection,
    TranscriptRangesSelection,
    TranscriptValueSelection,
    TranscriptSelection,
    validate_transcript_selection_scope,
)


class TranscriptColumn(ColumnRef[str]):
    accepted_column_types: ClassVar[tuple[str, ...]] = ("timestamped_transcript",)


@dataclass(frozen=True, eq=False)
class TranscriptProjectionValue:
    """Opaque projection; only its issuing reader may publish its evidence."""


@dataclass(frozen=True, eq=False)
class TranscriptAnnotationValue:
    """Annotation projected onto the output occurrence's newly assigned clock."""


class TranscriptReader(Protocol):
    def read(
        self, source: TranscriptColumn, selection: TranscriptSelection
    ) -> DynamicTableResult:
        """Split admitted current transcripts, retaining exact chunk provenance."""
        ...
