"""Create transcript excerpts from source-bound temporal selections."""

from frisket.actions.core import ActionCategory, action, create_sheet
from frisket.actions.transcript_types import (
    TranscriptColumn,
    TranscriptColumnSelection,
    TranscriptReader,
    TranscriptSelection,
)
from frisket.actions.types import ActionParams, DynamicTableResult


class TranscriptSegmentsParams(ActionParams):
    source: TranscriptColumn
    selection: TranscriptSelection


def transcript_segments(
    params: TranscriptSegmentsParams, transcripts: TranscriptReader
) -> DynamicTableResult:
    return transcripts.read(params.source, params.selection)


TRANSCRIPT_SEGMENTS = action(
    name="transcript_segments",
    title="Split transcript",
    description="Create transcript excerpts using complete intersecting chunks and their exact timestamp evidence.",
    category=ActionCategory.CONVERT,
    run=create_sheet(transcript_segments),
    examples=(
        TranscriptSegmentsParams(
            source="transcript",
            selection=TranscriptColumnSelection(kind="column", column="sections"),
        ),
    ),
)
