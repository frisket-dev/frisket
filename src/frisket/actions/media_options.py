"""Semantic options shared by typed media admission and actual capability calls."""

from __future__ import annotations

from typing import Any

from pydantic import Field, field_validator

from frisket.actions.types import ActionParams
from frisket.contracts.actions.schemas._base import StrictPositiveInt, StrictString
from frisket.contracts.actions.schemas._engines import (
    OCR_DEAD_ENGINE_REPLACEMENTS,
    OCR_ENGINE_TABLE,
    TRANSCRIBE_DEAD_ENGINE_REPLACEMENTS,
    TRANSCRIBE_ENGINE_TABLE,
    alias_map,
    dead_engine_rejection,
    project_transcription_engine_options,
    symbolic_engine_names,
    transcription_engine_capabilities,
)
from frisket.contracts.actions.schemas._language import canonicalize_languages
from frisket.contracts.actions.schemas._validators import OptionalStrictNonBlankParam
from frisket.contracts.transcription_language import transcribe_language_declaration
from frisket.contracts.transcription_sidecar import TRANSCRIPTION_CONTEXT_MAX_CHARS


def _canonical_engine(engine: str, table, replacements, error: str) -> str:
    if not isinstance(engine, str) or not engine.strip():
        raise ValueError(error)
    engine = engine.strip()
    if "/" not in engine and engine not in symbolic_engine_names(table):
        reason = dead_engine_rejection(replacements, engine)
        raise ValueError(f"{error}: {reason}" if reason else error)
    return alias_map(table).get(engine, engine)


class OcrOptions(ActionParams):
    language: OptionalStrictNonBlankParam = None
    dpi: int = Field(default=200, ge=50, le=600, strict=True)
    searchable_pdf: bool = Field(default=False, strict=True)

    def normalize(self, engine: str) -> dict[str, Any]:
        """Validate actual values and return the same options used at admission."""
        engine = _canonical_engine(
            engine, OCR_ENGINE_TABLE, OCR_DEAD_ENGINE_REPLACEMENTS, "invalid_ocr_engine"
        )
        options = type(self).model_validate(self.model_dump())
        if options.searchable_pdf and "/" in engine:
            raise ValueError("searchable_pdf_unsupported_engine")
        normalized: dict[str, Any] = {
            "dpi": options.dpi,
            "searchable_pdf": options.searchable_pdf,
        }
        if options.language is not None:
            normalized["language"] = options.language
        return normalized


class TranscriptionOptions(ActionParams):
    language: list[StrictString] | None = Field(default=None, validate_default=True)
    model_size: OptionalStrictNonBlankParam = None
    context: StrictString | None = Field(
        default=None,
        json_schema_extra={"maxLength": TRANSCRIPTION_CONTEXT_MAX_CHARS},
    )
    clean: bool = Field(default=False, strict=True)
    vad: bool = Field(default=True, strict=True)
    diarize: bool = Field(default=False, strict=True)
    num_speakers: StrictPositiveInt | None = None
    min_speakers: StrictPositiveInt | None = None
    max_speakers: StrictPositiveInt | None = None

    @field_validator("context")
    @classmethod
    def _validate_context(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped or len(stripped) > TRANSCRIPTION_CONTEXT_MAX_CHARS:
            raise ValueError("invalid_params")
        return stripped

    @field_validator("language", mode="before")
    @classmethod
    def _canonicalize_language(cls, value: Any) -> Any:
        return canonicalize_languages(value)

    def normalize(self, engine: str) -> dict[str, Any]:
        """Normalize semantic kwargs without manufacturing supplied-only knobs.

        Explicit unsupported fields refuse even when null/false. Reparse all
        values before checking the original field-presence set, so mutation
        cannot bypass validation and normalization never mutates this object.
        """
        engine = _canonical_engine(
            engine,
            TRANSCRIBE_ENGINE_TABLE,
            TRANSCRIBE_DEAD_ENGINE_REPLACEMENTS,
            "invalid_transcription_engine",
        )
        options = type(self).model_validate(self.model_dump())
        supplied = self.model_fields_set
        capabilities = transcription_engine_capabilities(
            TRANSCRIBE_ENGINE_TABLE, engine
        )
        language = list(options.language or [])
        declaration = transcribe_language_declaration(
            engine, table=TRANSCRIBE_ENGINE_TABLE
        )
        if declaration.mode == "auto_only" and language:
            raise ValueError("invalid_language_selection")
        if declaration.mode == "fixed":
            allowed = (
                {declaration.fixed_language} if declaration.fixed_language else set()
            )
            if set(language) - allowed:
                raise ValueError("invalid_language_selection")
            language = []
        if declaration.mode == "single" and len(language) > 1:
            raise ValueError("invalid_language_selection")
        if declaration.choices is not None:
            allowed = {choice.value for choice in declaration.choices}
            if any(value not in allowed for value in language):
                raise ValueError("invalid_language_selection")

        if any(
            name in supplied and not getattr(capabilities, name)
            for name in ("context", "vad", "model_size", "clean")
        ):
            raise ValueError("transcription_option_unavailable")

        diarize = options.diarize
        if capabilities.diarization_default and "diarize" not in supplied:
            diarize = True
        speaker_fields = ("num_speakers", "min_speakers", "max_speakers")
        if capabilities.diarization_mode == "intrinsic" and any(
            name in supplied for name in ("diarize", *speaker_fields)
        ):
            raise ValueError("transcription_option_unavailable")
        hints = tuple(getattr(options, name) for name in speaker_fields)
        if options.num_speakers is not None and (
            options.min_speakers is not None or options.max_speakers is not None
        ):
            raise ValueError("invalid_params")
        if not diarize and any(hint is not None for hint in hints):
            raise ValueError("invalid_params")
        if (
            options.min_speakers is not None
            and options.max_speakers is not None
            and options.min_speakers > options.max_speakers
        ):
            raise ValueError("invalid_params")
        if diarize and capabilities.diarization_mode == "none":
            raise ValueError("diarization_unavailable")
        if capabilities.max_speakers is not None and any(
            hint is not None and hint > capabilities.max_speakers for hint in hints
        ):
            raise ValueError("diarization_speaker_cap")
        if capabilities.speaker_hint == "none" and any(
            hint is not None for hint in hints
        ):
            raise ValueError("transcription_option_unavailable")

        values = {name: getattr(options, name) for name in supplied}
        values.update(language=language, diarize=diarize)
        if capabilities.clean:
            values["clean"] = options.clean
        return project_transcription_engine_options(
            engine, values, table=TRANSCRIBE_ENGINE_TABLE
        )
