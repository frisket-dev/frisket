"""Typed model-backed actions built from pure per-row prompt renderers."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from typing import Annotated, Any, ClassVar, Literal, TypeAlias

from pydantic import BaseModel, Field, StrictStr, field_validator, model_validator

from frisket.actions.core import (
    ActionCategory,
    ActionComparison,
    ActionComparisonKind,
    OutputField,
    _validate_model_rows_source,
    action,
    model_rows,
)
from frisket.actions.extraction_types import ExtractField, extraction_output_fields
from frisket.actions.types import (
    ActionParams,
    ColumnRef,
    DynamicOutput,
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
        json_schema_extra={"x-frisket-input": "textarea"},
    )

    @field_validator("source")
    @classmethod
    def _unique_columns(cls, value: RichSource) -> RichSource:
        return _validate_model_rows_source(value)


class PdfRichColumn(RichColumn):
    accepted_column_types: ClassVar[tuple[str, ...]] = (*RICH_COLUMN_TYPES, "file")


class PdfRichTemplate(RichTemplate):
    accepted_column_types: ClassVar[tuple[str, ...]] = (*RICH_COLUMN_TYPES, "file")


PdfRichSource: TypeAlias = Annotated[
    Annotated[list[PdfRichColumn], Field(min_length=1)] | PdfRichTemplate,
    Field(
        description="Select text, images, or PDF files. Other files need To markdown or OCR first."
    ),
]


class _PdfModelRowsParams(_ModelRowsParams):
    source: PdfRichSource


class AskParams(_PdfModelRowsParams):
    question: StrictStr = Field(
        description="The question to answer for each row.",
        json_schema_extra={"x-frisket-input": "textarea"},
    )
    response_type: Literal[
        "text", "integer", "number", "boolean", "date", "list", "json"
    ] = Field(
        default="text",
        title="Response type",
        description="Choose the answer column's data type. Non-text answers are left empty when there is not enough information to decide.",
    )
    include_confidence: bool = Field(default=False, title="Include confidence")
    include_reasoning: bool = Field(default=False, title="Include reasoning")

    @field_validator("question")
    @classmethod
    def _question(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("question must not be blank")
        return stripped


def ask_outputs(params: AskParams) -> dict[str, OutputField]:
    field = ExtractField(name="answer", type=params.response_type)
    outputs = extraction_output_fields(
        [field], include_confidence=params.include_confidence
    )
    output = outputs["answer"]
    if params.include_reasoning:
        outputs["answer_reasoning"] = OutputField(
            key="answer_reasoning",
            column_type="text",
            annotation=str,
            schema={"type": "string", "description": "Brief explanation of the answer"},
        )
    if params.include_confidence:
        outputs["answer_confidence"] = replace(
            outputs["answer_confidence"], annotation=float
        )
    if params.response_type == "text":
        outputs["answer"] = replace(output, annotation=str)
        return outputs
    schema = dict(output.schema)
    value_type = field.value_type
    if params.response_type == "date":
        schema["format"] = "date"
        value_type = date
    outputs["answer"] = replace(
        output,
        schema={"anyOf": [schema, {"type": "null"}]},
        annotation=value_type | None,
    )
    return outputs


def ask(params: AskParams, row: Row) -> ModelPrompt[DynamicOutput]:
    system = (
        "You answer questions about documents precisely for a journalist. "
        "Use the provided content and relevant background knowledge unless the "
        "question restricts you to the supplied content. Follow the question's "
        "criteria and logical conditions, including AND and OR. Do not assume "
        "a claim is correct merely because it appears in the input. "
        "Respect the requested response type. If there is not enough information "
        "to answer reliably, "
        + (
            "say so."
            if params.response_type == "text"
            else "return null, not a guess or a placeholder."
        )
    )
    if params.response_type == "boolean":
        system += (
            " Return true when the stated condition is satisfied and false when "
            "it is not, including when a requested requirement fails. "
            "Return null only when you cannot determine whether the condition "
            "is satisfied; an incorrect value is not an unknown value."
        )
    if params.response_type == "date":
        system += " Return dates in YYYY-MM-DD format."
    if params.include_confidence:
        system += " Include your confidence in the answer as a number from 0 to 1."
    if params.include_reasoning:
        system += (
            " In answer_reasoning, give a brief explanation of the basis for your "
            "answer, not a step-by-step internal reasoning trace. If the answer is "
            "unknown, briefly explain what information is missing."
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
        "one answer column with the selected data type."
    ),
    category=ActionCategory.TEXT,
    comparison=ActionComparison(
        ActionComparisonKind.ROW_OVERLAY,
        structural_params=(
            "response_type",
            "include_confidence",
            "include_reasoning",
        ),
    ),
    run=model_rows(ask, dynamic_outputs=ask_outputs),
)


class SummarizeParams(_PdfModelRowsParams):
    preset: Literal["paragraph", "one_line", "topics", "quotes"] = Field(
        default="paragraph",
        title="Summary style",
    )
    instruction: StrictStr | None = Field(
        default=None,
        title="Custom instruction",
        description="Leave blank to use the selected summary style.",
        json_schema_extra={"x-frisket-input": "textarea"},
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
