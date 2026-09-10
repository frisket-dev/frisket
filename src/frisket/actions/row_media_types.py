"""Typed local image results and their row-bound acquisition capabilities."""

from typing import Annotated, Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from frisket.actions.types import ActionParams, ColumnRef, Row, StagedImage


class FrameCount(ActionParams):
    kind: Literal["count"] = "count"
    count: int = Field(default=4, ge=1, le=200, strict=True)


class FrameInterval(ActionParams):
    kind: Literal["interval"] = "interval"
    seconds: float = Field(ge=0.1, strict=True, allow_inf_nan=False)


FrameSampling = Annotated[FrameCount | FrameInterval, Field(discriminator="kind")]


class Frame(BaseModel):
    model_config = ConfigDict(extra="forbid")

    t: float = Field(ge=0, allow_inf_nan=False)
    image: StagedImage


class Face(BaseModel):
    model_config = ConfigDict(extra="forbid")

    x: int = Field(ge=0, strict=True)
    y: int = Field(ge=0, strict=True)
    w: int = Field(gt=0, strict=True)
    h: int = Field(gt=0, strict=True)
    face: StagedImage


class FrameExtractor(Protocol):
    async def extract(
        self,
        row: Row,
        source: ColumnRef[Any],
        *,
        sampling: FrameSampling,
        max_dimension: int | None = None,
    ) -> list[Frame]: ...


class FaceExtractor(Protocol):
    async def extract(self, row: Row, source: ColumnRef[Any]) -> list[Face]: ...
