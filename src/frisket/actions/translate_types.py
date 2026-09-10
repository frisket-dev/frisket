"""Translation values and semantic options shared by admission and execution."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from pydantic import (
    BaseModel,
    Field,
    StrictBool,
    StrictStr,
    field_validator,
)

from frisket.actions.types import ActionParams, Row
from frisket.actions.media_types import DetectedLanguage
from frisket.contracts.actions.schemas._language import canonicalize_languages
from frisket.actions.translation_languages import translate_language_declaration

TRANSLATION_ENGINES = frozenset(
    {"llm", "deepl", "google_translate", "opus_mt", "hy_mt2"}
)


def translation_text(values: Mapping[str, Any]) -> str:
    """The exact unlabelled text priced and sent to hosted/local MT engines."""
    bits = [
        str(value)
        for value in values.values()
        if value is not None and not isinstance(value, dict)
    ]
    return "\n\n".join(bit for bit in bits if bit.strip())


class TranslationOptions(ActionParams):
    target_language: StrictStr = "English"
    language: list[StrictStr] | None = Field(default=None, validate_default=True)
    save_detected_language: StrictBool = False

    @field_validator("target_language")
    @classmethod
    def _target(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("target_language must not be blank")
        return value

    @field_validator("language", mode="before")
    @classmethod
    def _language(cls, value: Any) -> Any:
        if value is not None and not isinstance(value, list):
            raise ValueError("language must be a list")
        return canonicalize_languages(value)

    def normalize(self, engine: str) -> dict[str, Any]:
        if engine not in TRANSLATION_ENGINES:
            raise ValueError("invalid_translation_engine")
        options = TranslationOptions.model_validate(self.model_dump())
        languages = list(options.language or [])
        declaration = translate_language_declaration(engine)
        if declaration.mode == "auto_only" and languages:
            raise ValueError("invalid_language_selection")
        if declaration.mode == "single" and (
            len(languages) > 1 or not declaration.allows_auto and not languages
        ):
            raise ValueError("invalid_language_selection")
        if options.save_detected_language and not (
            declaration.detects
            or declaration.mode == "single"
            and not declaration.allows_auto
        ):
            raise ValueError("detected_language_unsupported_engine")
        if engine in {"deepl", "google_translate"}:
            from frisket.ops.integrations.translate_common import (
                TranslateEngineError,
                normalize_language,
            )

            try:
                normalize_language(engine, options.target_language, "target")
                if languages:
                    normalize_language(engine, languages[0], "source")
            except TranslateEngineError as error:
                raise ValueError(f"{error.code}: {error}") from error
        return {
            "target_language": options.target_language,
            "language": languages,
            "save_detected_language": options.save_detected_language,
        }


class TranslateOutput(BaseModel):
    translation: str
    detected_language: DetectedLanguage | None = None


class Translator(Protocol):
    """Translate actual text through the invocation's selected provider."""

    async def translate(
        self, row: Row, text: str, *, options: TranslationOptions
    ) -> TranslateOutput: ...
