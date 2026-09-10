"""Typed temporal analysis over admitted source rows."""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, Field, JsonValue, field_validator, model_validator

from frisket.actions.core import ActionCategory, action, map_rows
from frisket.actions.temporal_types import TopicSectionsReader, VisualCutsReader
from frisket.actions.transcript_types import TranscriptColumn
from frisket.actions.types import ActionParams, ColumnRef, EngineRef, Row, RowResult
from frisket.features.temporal_values import TimelinePointsValue, TimelineRangesValue


class VideoColumn(ColumnRef[Any]):
    accepted_column_types: ClassVar[tuple[str, ...]] = ("video",)


class VisualCutsParams(ActionParams):
    source: VideoColumn = Field(
        description="Video column to analyze for scene boundaries."
    )


class VisualCutsOutput(BaseModel):
    cuts: TimelinePointsValue


async def find_visual_cuts(
    params: VisualCutsParams, row: Row, visual_cuts: VisualCutsReader
) -> RowResult[VisualCutsOutput]:
    return RowResult(
        output=VisualCutsOutput(cuts=await visual_cuts.read(row, params.source))
    )


FIND_VISUAL_CUTS = action(
    name="find_visual_cuts",
    title="Find visual cuts",
    description=(
        "Find scene boundaries in each selected video and write editable, "
        "source-bound timestamps."
    ),
    category=ActionCategory.EXTRACT,
    run=map_rows(find_visual_cuts),
    examples=(VisualCutsParams(source="video"),),
)


class TopicSectionsParams(ActionParams):
    source: TranscriptColumn
    engine: EngineRef[TopicSectionsReader] = EngineRef[TopicSectionsReader](
        "texttiling"
    )
    settings: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("engine")
    @classmethod
    def _engine(
        cls, value: EngineRef[TopicSectionsReader]
    ) -> EngineRef[TopicSectionsReader]:
        from frisket.features.topic_segmentation.engines import get_segmenter

        get_segmenter(value.root)
        return value

    @model_validator(mode="after")
    def _settings(self) -> TopicSectionsParams:
        if not self.settings:
            return self
        from frisket.features.topic_segmentation.engines import get_segmenter

        get_segmenter(self.engine.root).validate_settings(self.settings)
        return self


class TopicSectionsOutput(BaseModel):
    sections: TimelineRangesValue


async def find_topic_sections(
    params: TopicSectionsParams, row: Row, topics: TopicSectionsReader
) -> RowResult[TopicSectionsOutput]:
    return RowResult(
        output=TopicSectionsOutput(
            sections=await topics.read(
                row, params.source, engine=params.engine.root, settings=params.settings
            )
        )
    )


FIND_TOPIC_SECTIONS = action(
    name="find_topic_sections",
    title="Find topic changes",
    description=(
        "Segment each selected timestamped transcript and write complete, "
        "editable topic-section ranges."
    ),
    category=ActionCategory.EXTRACT,
    run=map_rows(find_topic_sections),
    examples=(TopicSectionsParams(source="transcript"),),
)
