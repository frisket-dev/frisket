"""Per-row web research, with one bounded loop behind an admitted capability."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from frisket.actions.core import (
    ActionCategory,
    OutputField,
    _validate_model_rows_source,
    action,
    map_rows,
)
from frisket.actions.extraction_types import ExtractField, extraction_output_fields
from frisket.actions.model_rows import RichSource
from frisket.actions.research_types import Researcher
from frisket.actions.types import (
    ActionParams,
    DynamicOutput,
    ModelRef,
    Outcome,
    Row,
    RowResult,
    Template,
)


class ResearchParams(ActionParams):
    source: RichSource
    model: ModelRef
    question: Template[Any]
    include_sources: bool = True

    @field_validator("source")
    @classmethod
    def _source(cls, value):
        return _validate_model_rows_source(value)


class AnswerOutput(BaseModel):
    answer: str
    sources: list[str] = Field(default_factory=list)


def source_values(row: Row, source: RichSource) -> dict[str, Any]:
    if isinstance(source, Template):
        return {"input": source.render(row)}
    return {column.name: column.read(row) for column in source}


async def answer(
    params: ResearchParams, row: Row, researcher: Researcher
) -> RowResult[AnswerOutput]:
    result = await researcher.answer(
        row, goal=params.question.render(row), context=source_values(row, params.source)
    )
    return RowResult(output=AnswerOutput(answer=result.answer, sources=result.sources))


ANSWER = action(
    name="answer",
    title="Agent Research",
    description="Search and read public sources to answer a question about each row.",
    category=ActionCategory.TEXT,
    run=map_rows(
        answer,
        active_outputs=lambda params: (
            ("answer", "sources") if params.include_sources else ("answer",)
        ),
    ),
    examples=(
        ResearchParams(
            source=["company"],
            question=Template(text="What does {{company}} manufacture?"),
            model="anthropic/claude-haiku-4-5",
        ),
    ),
)


class StructuredResearchParams(ResearchParams):
    fields: list[ExtractField] = Field(min_length=1, max_length=64)
    include_confidence: bool = False

    @model_validator(mode="after")
    def _output_names(self):
        structured_output_fields(self)
        return self


def structured_output_fields(params: StructuredResearchParams):
    outputs = extraction_output_fields(
        params.fields, include_confidence=params.include_confidence
    )
    if "sources" in outputs:
        raise ValueError(
            "Sources is reserved for research citations; choose another field name"
        )
    if params.include_sources:
        outputs["sources"] = OutputField(
            key="sources",
            column_type="json",
            schema={"type": "array", "items": {"type": "string"}},
            annotation=Outcome[list[str]],
        )
    return outputs


async def extract(
    params: StructuredResearchParams, row: Row, researcher: Researcher
) -> RowResult[DynamicOutput]:
    return RowResult(
        output=await researcher.extract(
            row,
            goal=params.question.render(row),
            context=source_values(row, params.source),
            fields=params.fields,
            include_confidence=params.include_confidence,
            include_sources=params.include_sources,
        )
    )


EXTRACT = action(
    name="extract",
    title="Structured Agent Research",
    description="Search and read public sources to fill typed fields for each row.",
    category=ActionCategory.TEXT,
    run=map_rows(extract, dynamic_outputs=structured_output_fields),
    examples=(
        StructuredResearchParams(
            source=["company"],
            question=Template(text="Research {{company}} using public filings."),
            model="anthropic/claude-haiku-4-5",
            fields=[
                ExtractField(name="headquarters", type="text"),
                ExtractField(name="employee_count", type="integer"),
            ],
        ),
    ),
)
