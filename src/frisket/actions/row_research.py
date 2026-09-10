"""Per-row web research, with one bounded loop behind an admitted capability."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator

from frisket.actions.core import (
    ActionCategory,
    _validate_model_rows_source,
    action,
    map_rows,
)
from frisket.actions.model_rows import RichSource
from frisket.actions.research_types import Researcher
from frisket.actions.types import ActionParams, ModelRef, Row, RowResult, Template


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
    title="Research each row",
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
