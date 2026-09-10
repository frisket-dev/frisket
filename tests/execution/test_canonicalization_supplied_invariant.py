"""Authored option presence must survive admission without materializing defaults."""

from __future__ import annotations

import dataclasses

import pytest

from frisket.actions import media_options
from frisket.actions.media import TRANSCRIBE, TranscribeParams, transcribe_options
from frisket.actions.media_options import TranscriptionOptions
from frisket.contracts.actions.schemas._engines import (
    TRANSCRIBE_ENGINE_TABLE,
    engine_ids,
    transcription_engine_capabilities,
)


def test_typed_transcription_owns_presence_sensitive_options():
    assert TRANSCRIBE.run.engine_options is transcribe_options
    assert {"vad", "clean"} <= TranscriptionOptions.model_fields.keys()
    absent = TranscribeParams(source="media")
    authored = TranscribeParams(source="media", vad=False)
    assert "vad" not in transcribe_options(absent).model_fields_set
    assert "vad" in transcribe_options(authored).model_fields_set
    assert absent.model_dump(exclude_unset=True) != authored.model_dump(
        exclude_unset=True
    )


@pytest.mark.parametrize("engine", engine_ids(TRANSCRIBE_ENGINE_TABLE))
def test_transcription_admission_never_materializes_vad(engine: str):
    params = TranscribeParams(source="media", engine=engine)
    assert "vad" not in params.model_fields_set
    options = transcribe_options(params)
    assert "vad" not in options.model_fields_set
    assert "vad" not in options.normalize(params.engine.root)
    assert "vad" not in params.model_dump(exclude_unset=True)


def test_an_authored_vad_survives_normalization():
    params = TranscribeParams(source="media", engine="faster_whisper", vad=False)
    assert transcribe_options(params).normalize(params.engine.root)["vad"] is False
    assert params.model_dump(exclude_unset=True)["vad"] is False


def test_v1_transport_declaring_vad_preserves_omission(monkeypatch):
    moss = next(engine for engine in TRANSCRIBE_ENGINE_TABLE if engine.id == "moss")
    armed = dataclasses.replace(
        moss,
        transcription=dataclasses.replace(moss.transcription, vad=True),
    )
    table = tuple(
        armed if engine.id == "moss" else engine for engine in TRANSCRIBE_ENGINE_TABLE
    )
    monkeypatch.setattr(media_options, "TRANSCRIBE_ENGINE_TABLE", table)
    capabilities = transcription_engine_capabilities(table, "moss")
    assert capabilities.transport == "frisket.transcription.v1"
    assert capabilities.vad is True

    params = TranscribeParams(source="media", engine="moss")
    assert "vad" not in transcribe_options(params).normalize(params.engine.root)
    assert "vad" not in params.model_dump(exclude_unset=True)
    authored = TranscribeParams(source="media", engine="moss", vad=True)
    assert transcribe_options(authored).normalize(authored.engine.root)["vad"] is True
