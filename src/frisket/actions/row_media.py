"""Extract local video frames and face crops into ordinary JSON list columns."""

from typing import Any, ClassVar

from pydantic import BaseModel, Field

from frisket.actions.core import ActionCategory, action, map_rows
from frisket.actions.row_media_types import (
    Face,
    FaceExtractor,
    Frame,
    FrameCount,
    FrameExtractor,
    FrameSampling,
)
from frisket.actions.types import ActionParams, ColumnRef, Row, RowResult


class VideoColumn(ColumnRef[Any]):
    accepted_column_types: ClassVar[tuple[str, ...]] = ("video", "file")


class ImageColumn(ColumnRef[Any]):
    accepted_column_types: ClassVar[tuple[str, ...]] = ("image", "file")


class VideoFramesParams(ActionParams):
    source: VideoColumn
    sampling: FrameSampling = FrameCount()
    max_dimension: int | None = Field(default=None, ge=16, le=4096, strict=True)


class VideoFramesOutput(BaseModel):
    frames: list[Frame]


async def video_frames(
    params: VideoFramesParams, row: Row, extractor: FrameExtractor
) -> RowResult[VideoFramesOutput]:
    return RowResult(
        output=VideoFramesOutput(
            frames=await extractor.extract(
                row,
                params.source,
                sampling=params.sampling,
                max_dimension=params.max_dimension,
            )
        )
    )


class ExtractFacesParams(ActionParams):
    source: ImageColumn


class ExtractFacesOutput(BaseModel):
    faces: list[Face]


async def extract_faces(
    params: ExtractFacesParams, row: Row, extractor: FaceExtractor
) -> RowResult[ExtractFacesOutput]:
    return RowResult(
        output=ExtractFacesOutput(faces=await extractor.extract(row, params.source))
    )


VIDEO_FRAMES = action(
    name="video_frames",
    title="Video frames",
    description="Extract still frames from each video with timestamps and optional downscaling.",
    category=ActionCategory.EXTRACT,
    examples=(VideoFramesParams(source="video"),),
    run=map_rows(video_frames),
)

EXTRACT_FACES = action(
    name="extract_faces",
    title="Extract faces",
    description="Detect faces in each image and return pixel bounding boxes and cropped face images.",
    category=ActionCategory.EXTRACT,
    examples=(ExtractFacesParams(source="image"),),
    run=map_rows(extract_faces),
)
