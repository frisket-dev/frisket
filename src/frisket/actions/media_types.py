"""Plain media results and the row-bound capabilities that produce them."""

from typing import Any, ClassVar, Protocol

from pydantic import BaseModel, ConfigDict, Field, RootModel
from typing_extensions import TypedDict

from frisket.actions.media_options import OcrOptions, TranscriptionOptions
from frisket.actions.types import ColumnRef, Outcome, Row, StagedFile


class OcrColumn(ColumnRef[Any]):
    accepted_column_types: ClassVar[tuple[str, ...]] = ("image", "file")


class AudioColumn(ColumnRef[Any]):
    accepted_column_types: ClassVar[tuple[str, ...]] = ("audio", "video", "file")


class OcrText(RootModel[str]):
    """Text eligible for grounding against the actual OCR return."""

    model_config = ConfigDict(frozen=True, strict=True)


class TranscriptText(RootModel[str]):
    """Transcript text; timestamp eligibility requires matching host evidence."""

    model_config = ConfigDict(frozen=True, strict=True)


class DetectedLanguage(RootModel[str]):
    model_config = ConfigDict(frozen=True, strict=True)


class RecognizedDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: OcrText
    blocks: list[dict[str, Any]]
    pdf: Outcome[StagedFile | None] = Field(default_factory=lambda: Outcome.ok(None))


class TranscriptWord(TypedDict, total=False):
    __pydantic_config__ = ConfigDict(extra="allow")

    word: str
    start: float
    end: float


class TranscriptSegment(TypedDict, total=False):
    __pydantic_config__ = ConfigDict(extra="allow")

    segment_index: int
    start: float
    end: float
    text: str
    speaker: str
    speaker_confidence: str
    words: list[TranscriptWord]


class TranscribedMedia(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: TranscriptText
    segments: list[TranscriptSegment] = Field(
        json_schema_extra={
            "named_result": {
                "schema": "transcript_segments",
                "may_feed": ["derive.table_from_list"],
            }
        }
    )
    detected_language: DetectedLanguage | None = None


class OcrReader(Protocol):
    async def recognize(
        self, row: Row, source: ColumnRef[Any], *, options: OcrOptions
    ) -> RecognizedDocument: ...


class Transcriber(Protocol):
    async def transcribe(
        self, row: Row, source: ColumnRef[Any], *, options: TranscriptionOptions
    ) -> TranscribedMedia: ...
