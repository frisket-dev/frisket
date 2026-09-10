"""Extract one source-bound media range per selected row."""

from frisket.actions.core import ActionCategory, action
from frisket.actions.types import ActionParams
from frisket.actions.temporal_types import (
    TemporalMediaColumn,
    TranscriptColumnSelection,
)
from frisket.actions.temporal_extract_types import (
    PreparedTemporalExtract,
    TemporalExtractor,
    TemporalExtractSelection,
)


class ExtractRangeParams(ActionParams):
    source: TemporalMediaColumn
    selection: TemporalExtractSelection


def extract_range(
    params: ExtractRangeParams, extractor: TemporalExtractor
) -> PreparedTemporalExtract:
    return extractor.prepare(params.source, params.selection)


EXTRACT_RANGE = action(
    name="extract_range",
    title="Extract media range",
    description="Extract one clip per row, retaining compatible transcripts and annotations.",
    category=ActionCategory.EXTRACT,
    examples=(
        ExtractRangeParams(
            source="media",
            selection=TranscriptColumnSelection(kind="column", column="range"),
        ),
    ),
    run=extract_range,
)
