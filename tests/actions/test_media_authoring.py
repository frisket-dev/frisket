from pydantic import BaseModel
import pytest

from frisket.actions.core import (
    ActionCategory,
    ActionNamespace,
    ActionRegistry,
    action,
    map_rows,
)
from frisket.actions.media_options import OcrOptions, TranscriptionOptions
from frisket.actions.media_types import (
    AudioColumn,
    OcrReader,
    OcrText,
    Transcriber,
    TranscribedMedia,
)
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.types import (
    ActionParams,
    ActionRequest,
    EngineRef,
    Row,
    RowResult,
    SheetRows,
)
from frisket.engine.executor.map_rows_action import _typed_map_rows_plan


class Params(ActionParams):
    recording: AudioColumn
    backend: EngineRef[Transcriber] = EngineRef[Transcriber]("parakeet-tdt")
    identify_speakers: bool = False


def options(params: Params) -> TranscriptionOptions:
    return TranscriptionOptions(diarize=params.identify_speakers)


async def transcribe(
    params: Params, row: Row, service: Transcriber
) -> RowResult[TranscribedMedia]:
    raise AssertionError("Planning must not execute the handler")


def registered(callback=options):
    return ActionRegistry(
        (
            ActionNamespace(
                "custom",
                actions=(
                    action(
                        name="transcribe",
                        title="Transcribe",
                        description="Read audio.",
                        category=ActionCategory.EXTRACT,
                        run=map_rows(
                            transcribe,
                            engine_options=callback,
                            active_outputs=lambda params: ("text", "segments"),
                        ),
                    ),
                ),
            ),
        )
    ).get("custom.transcribe")


def test_renamed_derived_options_reach_admission_without_invoking_handler():
    action = registered()
    request = ActionRequest(
        action_id=action.action_id,
        scope=SheetRows(sheet_id=1),
        params={"recording": "interview", "identify_speakers": True},
        idempotency_key="custom-audio",
    )
    plan = _typed_map_rows_plan(BoundTypedActionRequest.bind(action, request))
    assert plan.spec["engine"] == "parakeet-tdt"
    assert plan.spec["diarize"] is True
    assert plan.spec["input_columns"] == ["interview"]
    assert "identify_speakers" not in plan.spec
    assert "vad" not in plan.spec
    assert [(f["name"], f["column_type"]) for f in plan.output_fields] == [
        ("text", "timestamped_transcript"),
        ("segments", "json"),
    ]


def test_option_callback_cannot_return_the_other_capability_options():
    terminal = registered(lambda params: OcrOptions()).definition.run
    with pytest.raises(TypeError, match="TranscriptionOptions"):
        terminal.resolve_engine_options(Params(recording="audio"), "parakeet-tdt")


def test_option_callback_must_be_pure_synchronous_shape():
    async def asynchronous(params):
        return TranscriptionOptions()

    with pytest.raises(TypeError, match="synchronous"):
        registered(asynchronous)
    with pytest.raises(TypeError, match="exactly"):
        registered(lambda params, row: TranscriptionOptions())


def test_ocr_semantic_text_remains_a_plain_string_column():
    class Output(BaseModel):
        text: OcrText

    async def recognize(
        params: ActionParams, row: Row, reader: OcrReader
    ) -> RowResult[Output]: ...

    terminal = map_rows(recognize)
    assert terminal.output_fields[0].column_type == "text"
    assert Output(text="recognized").model_dump(mode="json") == {"text": "recognized"}


def test_builtin_media_definitions_keep_options_and_independent_output_keys():
    from frisket.actions.media import OCR, TRANSCRIBE, OcrParams, TranscribeParams
    from frisket.contracts.actions.schemas._engines import (
        OCR_ENGINE_TABLE,
        TRANSCRIBE_ENGINE_TABLE,
        engine_ids,
    )
    from frisket.contracts.transcription_language import transcribe_language_declaration

    for engine in engine_ids(OCR_ENGINE_TABLE):
        params = OcrParams(source="page", engine=engine)
        assert [field.key for field in OCR.run.resolve_output_fields(params)] == [
            "text",
            "blocks",
        ]
    params = OcrParams(source="page", searchable_pdf=True)
    assert [field.key for field in OCR.run.resolve_output_fields(params)] == [
        "text",
        "blocks",
        "pdf",
    ]
    with pytest.raises(ValueError, match="searchable_pdf_unsupported_engine"):
        OcrParams(source="page", engine="openai/gpt-4.1-mini", searchable_pdf=True)
    for engine in engine_ids(TRANSCRIBE_ENGINE_TABLE):
        params = TranscribeParams(source="recording", engine=engine)
        outputs = [field.key for field in TRANSCRIBE.run.resolve_output_fields(params)]
        assert outputs == ["text", "segments"] + (
            ["detected_language"]
            if transcribe_language_declaration(engine).detects
            else []
        )
        options = TRANSCRIBE.run.resolve_engine_options(params, engine)
        if engine == "mai-transcribe-2":
            assert options["diarize"] is True
