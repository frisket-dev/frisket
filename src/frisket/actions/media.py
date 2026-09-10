"""OCR and transcription share typed options with their injected services."""

from pydantic import Field, model_validator

from frisket.actions.core import ActionCategory, action, map_rows
from frisket.actions.media_options import OcrOptions, TranscriptionOptions
from frisket.actions.media_types import (
    AudioColumn,
    OcrColumn,
    OcrReader,
    RecognizedDocument,
    TranscribedMedia,
    Transcriber,
)
from frisket.actions.types import EngineRef, Row, RowResult
from frisket.contracts.transcription_language import transcribe_language_declaration


class OcrParams(OcrOptions):
    source: OcrColumn = Field(
        description="Choose an image or PDF column. Import or download the media first."
    )
    engine: EngineRef[OcrReader] = EngineRef[OcrReader]("rapidocr")

    @model_validator(mode="after")
    def _validate_options(self):
        ocr_options(self).normalize(self.engine.root)
        return self


class TranscribeParams(TranscriptionOptions):
    source: AudioColumn = Field(
        description="Choose an audio or video column. Import or download the media first."
    )
    engine: EngineRef[Transcriber] = EngineRef[Transcriber]("faster_whisper")

    @model_validator(mode="after")
    def _validate_options(self):
        transcribe_options(self).normalize(self.engine.root)
        return self


def ocr_options(params: OcrParams) -> OcrOptions:
    return OcrOptions.model_validate(
        params.model_dump(exclude={"source", "engine"}, exclude_unset=True)
    )


def transcribe_options(params: TranscribeParams) -> TranscriptionOptions:
    return TranscriptionOptions.model_validate(
        params.model_dump(exclude={"source", "engine"}, exclude_unset=True)
    )


def _ocr_outputs(params: OcrParams):
    return ("text", "blocks", "pdf") if params.searchable_pdf else ("text", "blocks")


def _transcribe_outputs(params: TranscribeParams):
    return (
        ("text", "segments", "detected_language")
        if transcribe_language_declaration(params.engine.root).detects
        else ("text", "segments")
    )


async def ocr(
    params: OcrParams, row: Row, reader: OcrReader
) -> RowResult[RecognizedDocument]:
    return RowResult(
        output=await reader.recognize(row, params.source, options=ocr_options(params))
    )


async def transcribe(
    params: TranscribeParams, row: Row, reader: Transcriber
) -> RowResult[TranscribedMedia]:
    return RowResult(
        output=await reader.transcribe(
            row, params.source, options=transcribe_options(params)
        )
    )


OCR = action(
    name="ocr",
    title="OCR",
    description="Read text from images and documents with the selected OCR engine.",
    category=ActionCategory.EXTRACT,
    examples=(OcrParams(source="document"),),
    run=map_rows(ocr, engine_options=ocr_options, active_outputs=_ocr_outputs),
)

TRANSCRIBE = action(
    name="transcribe",
    title="Transcribe",
    description="Transcribe audio and video with timestamps using the selected speech engine.",
    category=ActionCategory.EXTRACT,
    examples=(TranscribeParams(source="recording"),),
    run=map_rows(
        transcribe,
        engine_options=transcribe_options,
        active_outputs=_transcribe_outputs,
    ),
)
