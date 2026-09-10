"""Supplied-only options survive request serialization and semantic hashing."""

import pytest
from pydantic import BaseModel, Field

from frisket.actions.core import (
    ActionCategory,
    ActionNamespace,
    ActionRegistry,
    action,
    map_rows,
)
from frisket.actions.media_options import TranscriptionOptions
from frisket.actions.media_types import Transcriber
from frisket.actions.system import BoundTypedActionRequest, validate_root_action
from frisket.actions.types import (
    ActionParams,
    ActionRequest,
    ColumnRef,
    EngineRef,
    Row,
    RowResult,
    SheetRows,
)
from frisket.engine.executor.map_rows_action import (
    _typed_map_rows_plan,
    bound_typed_program_request_from_runner_spec,
    normalized_typed_request_identity,
    typed_request_hash,
)


class Params(ActionParams):
    recording: ColumnRef[str]
    backend: EngineRef[Transcriber] = EngineRef[Transcriber](
        "openrouter/microsoft/mai-transcribe-2"
    )
    identify_speakers: bool = False


class NestedParams(ActionParams):
    recording: ColumnRef[str]
    backend: EngineRef[Transcriber] = EngineRef[Transcriber](
        "openrouter/microsoft/mai-transcribe-2"
    )
    settings: TranscriptionOptions = Field(default_factory=TranscriptionOptions)


class Output(BaseModel):
    text: str


def options(params: Params) -> TranscriptionOptions:
    return (
        TranscriptionOptions(diarize=params.identify_speakers)
        if "identify_speakers" in params.model_fields_set
        else TranscriptionOptions()
    )


async def transcribe(
    params: Params, row: Row, service: Transcriber
) -> RowResult[Output]:
    raise AssertionError("Identity tests must not dispatch")


async def nested(
    params: NestedParams, row: Row, service: Transcriber
) -> RowResult[Output]:
    raise AssertionError("Identity tests must not dispatch")


@pytest.fixture
def registry(monkeypatch):
    import frisket.actions.registry as registry_module
    import frisket.actions.system as system

    registry = ActionRegistry(
        (
            ActionNamespace(
                "example",
                actions=(
                    action(
                        name="speech",
                        title="Speech",
                        description="Transcribe selected recordings.",
                        category=ActionCategory.EXTRACT,
                        run=map_rows(transcribe, engine_options=options),
                    ),
                    action(
                        name="nested",
                        title="Nested speech",
                        description="Transcribe selected recordings.",
                        category=ActionCategory.EXTRACT,
                        run=map_rows(nested, engine_options=lambda p: p.settings),
                    ),
                ),
            ),
        )
    )
    monkeypatch.setattr(registry_module, "ACTION_REGISTRY", registry)
    monkeypatch.setattr(system, "ACTION_REGISTRY", registry)
    return registry


def bind(registry, params, action_id="example.speech"):
    return BoundTypedActionRequest.bind(
        registry.get(action_id),
        ActionRequest(
            action_id=action_id,
            scope=SheetRows(sheet_id=7),
            params=params,
            idempotency_key="presence-test",
        ),
    )


def test_renamed_flag_default_does_not_collide_with_explicit_false(registry):
    omitted = bind(registry, {"recording": "audio"})
    explicit = bind(registry, {"recording": "audio", "identify_speakers": False})
    assert omitted.params.model_dump() == explicit.params.model_dump()
    assert _typed_map_rows_plan(omitted).spec["diarize"] is True
    assert _typed_map_rows_plan(explicit).spec["diarize"] is False
    assert typed_request_hash(omitted) != typed_request_hash(explicit)


@pytest.mark.parametrize("settings", [None, {}, {"diarize": False}])
def test_runtime_validation_and_receipt_identity_preserve_nested_presence(
    registry, settings
):
    authored = {"recording": "audio"}
    if settings is not None:
        authored["settings"] = settings
    original = bind(registry, authored, "example.nested")
    plan = _typed_map_rows_plan(original)
    assert normalized_typed_request_identity(original)["params"] == authored
    assert plan.request_identity["params"] == authored
    assert plan.spec["params"] == authored
    validated = validate_root_action(original.request.model_dump(mode="json"))
    assert validated.ok and validated.params == authored
    rebound = bound_typed_program_request_from_runner_spec(plan.spec)
    assert rebound is not None
    assert (
        rebound.params.settings.model_fields_set
        == original.params.settings.model_fields_set
    )
    assert _typed_map_rows_plan(rebound).spec["diarize"] == plan.spec["diarize"]
    assert typed_request_hash(rebound) == typed_request_hash(original)


def test_explicit_unsupported_null_is_not_erased(registry):
    original = bind(
        registry,
        {"recording": "audio", "settings": {"model_size": None}},
        "example.nested",
    )
    assert normalized_typed_request_identity(original)["params"]["settings"] == {
        "model_size": None
    }
    with pytest.raises(ValueError, match="transcription_option_unavailable"):
        typed_request_hash(original)
