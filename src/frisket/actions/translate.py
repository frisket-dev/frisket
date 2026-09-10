"""Typed translation with one inspected model request or an admitted MT call."""

from __future__ import annotations

from typing import Annotated, Any, ClassVar, TypeAlias

from pydantic import Field, StrictStr, field_validator, model_validator

from frisket.actions.core import (
    ActionCategory,
    _validate_model_rows_source,
    action,
    model_rows,
)
from frisket.actions.model_rows import (
    FILE_SOURCE_CONVERSION_HINT,
    RICH_COLUMN_TYPES,
    RichTemplate,
)
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
        default="", description="Optional dataset context for LLM translation."
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


def complete_translation(
    params: TranslateParams, row: Row, response: DynamicOutput
) -> RowResult[TranslateOutput]:
    if params.save_detected_language and "detected_language" not in response.root:
        raise RowError(
            "return_schema_mismatch",
            "Translation response is missing detected_language.",
        )
    values = TranslateOutput.model_validate(response.root)
    return RowResult(
        output=TranslateOutput(
            translation=values.translation,
            detected_language=(
                normalize_detected_bcp47(
                    values.detected_language.root
                    if values.detected_language is not None
                    else None
                )
                if params.save_detected_language
                else None
            ),
        )
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
    examples=(TranslateParams(source=["text"], model="anthropic/claude-haiku-4-5"),),
    run=model_rows(
        translate_prompt,
        direct=translate_direct,
        complete=complete_translation,
        active_outputs=translation_outputs,
        engine_options=translate_options,
    ),
)
