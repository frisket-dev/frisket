"""Typed model-backed actions built from pure per-row prompt renderers."""

from __future__ import annotations

from typing import Annotated, Any, ClassVar, Literal, TypeAlias

from pydantic import BaseModel, Field, StrictStr, field_validator, model_validator

from frisket.actions.core import (
    ActionCategory,
    _validate_model_rows_source,
    action,
    model_rows,
)
from frisket.actions.types import (
    ActionParams,
    ColumnRef,
    ModelPrompt,
    ModelRef,
    Row,
    Template,
)
from frisket.ai.message_content import render_input_block


RICH_COLUMN_TYPES = (
    "text",
    "timestamped_transcript",
    "category",
    "date",
    "number",
    "integer",
    "boolean",
    "json",
    "image",
)

FILE_SOURCE_CONVERSION_HINT = "For file contents, run To markdown or OCR first, then select the resulting text column."


class RichColumn(ColumnRef[Any]):
    accepted_column_types: ClassVar[tuple[str, ...]] = RICH_COLUMN_TYPES


class RichTemplate(Template[Any]):
    accepted_column_types: ClassVar[tuple[str, ...]] = RICH_COLUMN_TYPES

    @model_validator(mode="before")
    @classmethod
    def _template_value(cls, value: Any) -> Any:
        return value.model_dump() if isinstance(value, Template) else value


RichColumns: TypeAlias = Annotated[list[RichColumn], Field(min_length=1)]
RichSource: TypeAlias = Annotated[
    RichColumns | RichTemplate, Field(description=FILE_SOURCE_CONVERSION_HINT)
]


class _ModelRowsParams(ActionParams):
    source: RichSource
    model: ModelRef
    context: StrictStr = Field(
        default="",
        description="Optional context about the dataset.",
    )

    @field_validator("source")
    @classmethod
    def _unique_columns(cls, value: RichSource) -> RichSource:
        return _validate_model_rows_source(value)


class AskParams(_ModelRowsParams):
    question: StrictStr = Field(description="The question to answer for each row.")

    @field_validator("question")
    @classmethod
    def _question(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("question must not be blank")
        return stripped


class AskOutput(BaseModel):
    answer: str


def ask(params: AskParams, row: Row) -> ModelPrompt[AskOutput]:
    system = (
        "You answer questions about documents precisely for a journalist. "
        "Answer only from the provided content; if the content does not "
        "answer the question, say so. Preserve names, numbers, dates exactly."
    )
    if params.context:
        system += f"\n\nDataset context: {params.context}"
    user_parts = render_input_block(dict(row.values))
    user_parts.append({"type": "text", "text": f"\n{params.question}"})
    return ModelPrompt(
        messages=(
            {"role": "system", "content": system},
            {"role": "user", "content": user_parts},
        )
    )


ASK = action(
    examples=(
        AskParams(
            source=["source"],
            model="anthropic/claude-haiku-4-5",
            question="What does this row say?",
        ),
    ),
    name="ask",
    title="Ask a question of each row",
    description=(
        "Answer a free-form question about each row's selected content and write "
        "one text answer column."
    ),
    category=ActionCategory.TEXT,
    run=model_rows(ask),
)


class SummarizeParams(_ModelRowsParams):
    preset: Literal["paragraph", "one_line", "topics", "quotes"] = Field(
        default="paragraph",
        title="Summary style",
    )
    instruction: StrictStr | None = Field(
        default=None,
        title="Custom instruction",
        description="Leave blank to use the selected summary style.",
    )

    @field_validator("instruction")
    @classmethod
    def _instruction(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("instruction must not be blank")
        return stripped


class SummarizeOutput(BaseModel):
    summary: str


_SUMMARY_PRESETS = {
    "paragraph": "Write a single tight paragraph summarizing the content.",
    "one_line": "Summarize in one sentence of at most 25 words.",
    "topics": "List the 3-5 main topics discussed.",
    "quotes": "Pull out the 1-3 most newsworthy verbatim quotes.",
}


def summarize(params: SummarizeParams, row: Row) -> ModelPrompt[SummarizeOutput]:
    instruction = params.instruction or _SUMMARY_PRESETS[params.preset]
    system = (
        "You summarize documents precisely for a journalist. "
        "No editorializing; preserve names, numbers, dates exactly."
    )
    if params.context:
        system += f"\n\nDataset context: {params.context}"
    user_parts = render_input_block(dict(row.values))
    user_parts.append({"type": "text", "text": f"\n{instruction}"})
    return ModelPrompt(
        messages=(
            {"role": "system", "content": system},
            {"role": "user", "content": user_parts},
        )
    )


SUMMARIZE = action(
    examples=(SummarizeParams(source=["source"], model="anthropic/claude-haiku-4-5"),),
    name="summarize",
    title="Summarize rows",
    description=(
        "Summarize each row's selected content and write one text summary column."
    ),
    category=ActionCategory.TEXT,
    run=model_rows(summarize),
)
