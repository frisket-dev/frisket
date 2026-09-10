"""Media action schemas."""

from __future__ import annotations


from typing import Literal


from frisket.contracts.actions.schemas._engines import (
    OCR_ENGINE_TABLE,
    TO_MARKDOWN_ENGINE_TABLE,
    TRANSCRIBE_ENGINE_TABLE,
    TranscriptionDiarizationMode,
    TranscriptionEngineCapabilities,
    alias_map,
    symbolic_engine_names,
    transcription_engine_capabilities as _transcription_engine_capabilities,
)
from frisket.contracts.transcription_sidecar import (
    TRANSCRIPTION_CONTEXT_MAX_CHARS,
)


TRANSCRIBE_SYMBOLIC_ENGINES = symbolic_engine_names(TRANSCRIBE_ENGINE_TABLE)

TRANSCRIBE_ENGINE_ALIASES: dict[str, str] = alias_map(TRANSCRIBE_ENGINE_TABLE)


def transcribe_engine_capabilities(engine: str) -> TranscriptionEngineCapabilities:
    """Return the selected engine's exhaustive, fail-closed declaration."""

    return _transcription_engine_capabilities(TRANSCRIBE_ENGINE_TABLE, engine)


def transcribe_diarization_mode(engine: str) -> TranscriptionDiarizationMode:
    """Whether diarization is unavailable, request-driven, or intrinsic."""

    return transcribe_engine_capabilities(engine).diarization_mode


def transcribe_supports_diarization(engine: str) -> bool:
    """Whether an engine can emit speaker-labelled transcription segments."""

    return transcribe_diarization_mode(engine) != "none"


def transcribe_max_speakers(engine: str) -> int | None:
    """The released-checkpoint speaker cap for a diarizing engine (Sortformer =
    4), or None when the engine does not diarize / has no fixed cap. A
    num/min/max speaker hint above this is rejected (`diarization_speaker_cap`)."""
    return transcribe_engine_capabilities(engine).max_speakers


def transcribe_speaker_hint(engine: str) -> Literal["none", "count"]:
    """Whether a diarizing transcribe engine takes a speaker-count hint
    (``"count"`` -> render num/min/max inputs) or ignores one entirely
    (``"none"`` -> render no count control; Sortformer's fixed 4-channel
    offline pass). Only meaningful when `transcribe_supports_diarization` is
    True; the catalog hint omits this field entirely for a non-diarizing
    engine."""
    return transcribe_engine_capabilities(engine).speaker_hint


OCR_SYMBOLIC_ENGINES = symbolic_engine_names(OCR_ENGINE_TABLE)


TO_MARKDOWN_SYMBOLIC_ENGINES = symbolic_engine_names(TO_MARKDOWN_ENGINE_TABLE)


MAX_TRANSCRIBE_CONTEXT_CHARS = TRANSCRIPTION_CONTEXT_MAX_CHARS
