"""Typed translation with one inspected model request or an admitted MT call."""

from __future__ import annotations

from typing import Annotated, Any, ClassVar, TypeAlias

from pydantic import Field, StrictStr, field_validator, model_validator

from frisket.actions.core import (
    ActionCategory,
    ActionComparison,
    ActionComparisonKind,
    _validate_model_rows_source,
    action,
    model_rows,
)
from frisket.actions.model_rows import (
    FILE_SOURCE_CONVERSION_HINT,
    RICH_COLUMN_TYPES,
    RichTemplate,
)
from frisket.actions.media_types import DetectedLanguage
from frisket.actions.translate_types import (
    TranslationOptions,
    TranslateOutput,
    Translator,
    translation_text,
)
from frisket.actions.types import (
    ColumnRef,
    DynamicOutput,
    EngineRef,
    ModelPrompt,
    ModelRef,
    Row,
    RowResult,
    RowError,
)
from frisket.ai.message_content import render_input_block
from frisket.ops.integrations.translate_common import normalize_detected_bcp47

# Long enough for any language name, short enough to keep a row warning
# readable when the model writes prose into the field.
_SHOWN_LANGUAGE_CHARS = 40


class TranslationColumn(ColumnRef[Any]):
    accepted_column_types: ClassVar[tuple[str, ...]] = (*RICH_COLUMN_TYPES, "link")


TranslationColumns: TypeAlias = Annotated[list[TranslationColumn], Field(min_length=1)]


class TranslationTemplate(RichTemplate):
    accepted_column_types: ClassVar[tuple[str, ...]] = (
        TranslationColumn.accepted_column_types
    )


TranslationSource: TypeAlias = Annotated[
    TranslationColumns | TranslationTemplate,
    Field(description=FILE_SOURCE_CONVERSION_HINT),
]


class TranslateParams(TranslationOptions):
    source: TranslationSource
    engine: EngineRef[Translator] = EngineRef[Translator]("llm")
    model: ModelRef | None = None
    context: StrictStr = Field(
        default="",
        description="Optional dataset context for LLM translation.",
        json_schema_extra={"x-frisket-input": "textarea"},
    )

    @field_validator("source")
    @classmethod
    def _source(cls, value: TranslationSource) -> TranslationSource:
        return _validate_model_rows_source(value)

    @model_validator(mode="after")
    def _route_options(self):
        translate_options(self).normalize(self.engine.root)
        if self.engine.root == "llm":
            if self.model is None:
                raise ValueError("LLM translation requires model")
        elif self.model is not None or self.context.strip():
            raise ValueError("model and context require the LLM engine")
        return self


def translate_options(params: TranslateParams) -> TranslationOptions:
    return TranslationOptions.model_validate(
        {key: getattr(params, key) for key in TranslationOptions.model_fields}
    )


def translation_outputs(params: TranslateParams) -> tuple[str, ...]:
    return (
        ("translation", "detected_language")
        if params.save_detected_language
        else ("translation",)
    )


def translate_prompt(params: TranslateParams, row: Row) -> ModelPrompt[DynamicOutput]:
    source = next(iter(params.language or ()), None)
    instruction = (
        f"Translate the content into {params.target_language}."
        + (
            f" The source language is {source}."
            if source
            else " Auto-detect the source language."
        )
        + " Preserve names, numbers, and formatting. Translate faithfully — do not summarize or omit."
        + (
            " Report `detected_language` as a BCP-47 language tag (e.g. en, de, pt-BR), not a language name."
            if params.save_detected_language
            else ""
        )
    )
    system = "You are a precise document translator for a newsroom."
    if params.context.strip():
        system += f"\n\nDataset context: {params.context.strip()}"
    user_parts = render_input_block(dict(row.values))
    user_parts.append({"type": "text", "text": f"\n{instruction}"})
    return ModelPrompt(
        messages=(
            {"role": "system", "content": system},
            {"role": "user", "content": user_parts},
        ),
        response_schema={
            "type": "object",
            "properties": {
                "translation": {"type": "string"},
                **(
                    {"detected_language": {"type": ["string", "null"]}}
                    if params.save_detected_language
                    else {}
                ),
            },
            "required": list(translation_outputs(params)),
            "additionalProperties": False,
        },
    )


def _unrecognised_language_warning(raw: str) -> str:
    shown = " ".join(raw.split())
    if len(shown) > _SHOWN_LANGUAGE_CHARS:
        shown = shown[:_SHOWN_LANGUAGE_CHARS] + "…"
    return (
        f'The model\'s detected language "{shown}" was not recognised and was '
        "left empty."
    )


def complete_translation(
    params: TranslateParams, row: Row, response: DynamicOutput
) -> RowResult[TranslateOutput]:
    if params.save_detected_language and "detected_language" not in response.root:
        raise RowError(
            "return_schema_mismatch",
            "Translation response is missing detected_language.",
        )
    values = TranslateOutput.model_validate(response.root)
    raw_language = (
        values.detected_language.root
        if params.save_detected_language and values.detected_language is not None
        else None
    )
    normalized_language = normalize_detected_bcp47(raw_language)
    warnings = (
        (_unrecognised_language_warning(raw_language),)
        if raw_language and raw_language.strip() and normalized_language is None
        else ()
    )
    return RowResult(
        output=TranslateOutput(
            # The model's answer is stored as given, empty included; whether
            # it is right is not this action's judgement.
            translation=values.translation,
            detected_language=(
                DetectedLanguage(normalized_language)
                if normalized_language is not None
                else None
            ),
        ),
        warnings=warnings,
    )


async def translate_direct(
    params: TranslateParams, row: Row, translator: Translator
) -> RowResult[TranslateOutput]:
    return RowResult(
        output=await translator.translate(
            row,
            translation_text(row.values),
            options=translate_options(params),
        )
    )


TRANSLATE = action(
    name="translate",
    title="Translate rows",
    description="Translate selected row content, optionally saving its detected source language.",
    category=ActionCategory.TEXT,
    comparison=ActionComparison(
        ActionComparisonKind.ROW_OVERLAY,
        structural_params=("target_language", "save_detected_language"),
    ),
    examples=(
        TranslateParams(
            source=[TranslationColumn("text")],
            model=ModelRef("anthropic/claude-haiku-4-5"),
        ),
    ),
    run=model_rows(
        translate_prompt,
        direct=translate_direct,
        complete=complete_translation,
        active_outputs=translation_outputs,
        engine_options=translate_options,
    ),
)
