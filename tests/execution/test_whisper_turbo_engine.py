from __future__ import annotations

import pytest
from pydantic import ValidationError

from frisket.actions.media import TranscribeParams
from frisket.contracts.actions.schemas._engines import (
    TRANSCRIBE_ENGINE_TABLE,
    find_engine,
)
from frisket.execution.definitions import (
    StaticExecutionTargetProvider,
    build_static_targets,
)
from frisket.execution.provider import CompositionFacts
from frisket.execution.resolver import Resolution, ResolutionRequest, resolve


OPEN = CompositionFacts()


def test_whisper_turbo_is_one_fixed_gateway_engine() -> None:
    declaration = find_engine(TRANSCRIBE_ENGINE_TABLE, "whisper-turbo")
    assert declaration is not None
    assert declaration.label == "Whisper Turbo"
    assert declaration.aliases == ()
    assert declaration.transcription is not None
    assert declaration.transcription.model_size is False

    targets = {target.id: target for target in build_static_targets()}
    local_engines = {support.engine for support in targets["local"].engines}
    gateway_engines = {support.engine for support in targets["models-gateway"].engines}
    assert "faster_whisper" in local_engines
    assert "whisper-turbo" not in local_engines
    assert "whisper-turbo" in gateway_engines
    assert "faster_whisper" not in gateway_engines


def test_whisper_turbo_resolves_only_to_the_gateway(monkeypatch) -> None:
    monkeypatch.setenv("FRISKET_MODELS_URL", "http://models.test:8500")
    monkeypatch.setenv("FRISKET_MODELS_TOKEN", "gateway-token")
    result = resolve(
        ResolutionRequest(engine="whisper-turbo"),
        StaticExecutionTargetProvider(),
        OPEN,
    )
    assert isinstance(result, Resolution)
    assert result.target.id == "models-gateway"
    assert result.support.engine == "whisper-turbo"
    assert result.support.transport == "frisket.transcription.v1"


def test_whisper_turbo_has_no_model_size_control() -> None:
    with pytest.raises(ValidationError, match="transcription_option_unavailable"):
        TranscribeParams.model_validate(
            {
                "source": "audio",
                "engine": "whisper-turbo",
                "model_size": "large-v3-turbo",
            }
        )
