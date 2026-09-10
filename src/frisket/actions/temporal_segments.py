"""Split temporal media into a grounded child table."""

from frisket.actions.core import ActionCategory, action, create_sheet
from frisket.actions.temporal_types import (
    TemporalMediaColumn,
    TemporalMediaReader,
    TranscriptColumnSelection,
    TranscriptSelection,
)
from frisket.actions.types import ActionParams, DynamicTableResult


class TemporalSegmentsParams(ActionParams):
    source: TemporalMediaColumn
    selection: TranscriptSelection


def temporal_segments(
    params: TemporalSegmentsParams, media: TemporalMediaReader
) -> DynamicTableResult:
    return media.read(params.source, params.selection)


TEMPORAL_SEGMENTS = action(
    name="temporal_segments",
    title="Split media",
    description="Split audio or video into exact clips with their transcripts and temporal annotations.",
    category=ActionCategory.CONVERT,
    run=create_sheet(temporal_segments),
    examples=(
        TemporalSegmentsParams(
            source="media",
            selection=TranscriptColumnSelection(kind="column", column="sections"),
        ),
    ),
)
