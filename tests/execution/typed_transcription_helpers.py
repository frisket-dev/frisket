"""Real typed transcription requests for execution-boundary tests."""

from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.types import ActionRequest, SheetRows
from frisket.engine.executor.map_rows_action import (
    _typed_map_rows_plan,
    bound_typed_program_request_from_runner_spec,
)


def transcription_spec(
    sheet_id=1, *, source="media", engine="faster_whisper", **options
):
    action = ACTION_REGISTRY.get("media.transcribe")
    return _typed_map_rows_plan(
        BoundTypedActionRequest.bind(
            action,
            ActionRequest(
                action_id=action.action_id,
                scope=SheetRows(sheet_id=sheet_id),
                params={"source": source, "engine": engine, **options},
                output_names={"text": "transcript", "segments": "transcript_segments"},
                idempotency_key="execution-transcription-test",
            ),
        )
    ).spec_dict()


def transcription_program(spec):
    bound = bound_typed_program_request_from_runner_spec(spec)
    assert bound is not None
    return _typed_map_rows_plan(bound).program
