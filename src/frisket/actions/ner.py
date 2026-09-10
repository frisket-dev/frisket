"""Typed named-entity inputs and pure model request construction."""

from __future__ import annotations

from typing import Annotated, Any, ClassVar, TypeAlias

from pydantic import BaseModel, Field, StrictStr, field_validator, model_validator
from typing_extensions import NotRequired, TypedDict

from frisket.actions.core import (
    ActionCategory,
    _validate_model_rows_source,
    action,
    model_rows,
)
from frisket.actions.ner_types import (
    DEFAULT_NER_ENGINE,
    RECOMMENDED_NER_LABELS,
    NerExtractor,
)
from frisket.actions.model_rows import FILE_SOURCE_CONVERSION_HINT
from frisket.actions.types import (
    ActionParams,
    ColumnRef,
    EngineRef,
    ModelPrompt,
    ModelRef,
    Row,
    RowResult,
    Template,
)
from frisket.ai.message_content import render_input_block
from frisket.ops.entities import canonicalize_entities
from frisket.ops.ner_llm import entities_extract_instruction
from frisket.ops.ner_text import ner_text


class NerColumn(ColumnRef[Any]):
    accepted_column_types: ClassVar[tuple[str, ...]] = (
        "text",
        "timestamped_transcript",
    )


class NerTemplate(Template[Any]):
    excluded_column_types: ClassVar[tuple[str, ...]] = ("file",)


NerSource: TypeAlias = Annotated[
    Annotated[list[NerColumn], Field(min_length=1)] | NerTemplate,
    Field(description=FILE_SOURCE_CONVERSION_HINT),
]


class NerParams(ActionParams):
    source: NerSource
    labels: list[StrictStr] = Field(min_length=1)
    engine: EngineRef[NerExtractor] = EngineRef[NerExtractor](DEFAULT_NER_ENGINE)
    threshold: float = Field(default=0.5, ge=0, le=1)
    model: ModelRef | None = None
    extra_instructions: StrictStr = ""

    @field_validator("source")
    @classmethod
    def _source(cls, value: NerSource) -> NerSource:
        return _validate_model_rows_source(value)

    @field_validator("labels")
    @classmethod
    def _labels(cls, value: list[str]) -> list[str]:
        labels = [label.strip() for label in value]
        if any(not label for label in labels) or len(labels) != len(set(labels)):
            raise ValueError("entity labels must be nonblank and unique")
        return labels

    @field_validator("extra_instructions")
    @classmethod
    def _instructions(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def _engine_options(self) -> NerParams:
        if self.engine.root not in ("spacy", "gliner", "llm"):
            raise ValueError("unsupported NER engine")
        if self.engine.root == "llm" and self.model is None:
            raise ValueError("the LLM engine requires a model")
        if self.engine.root != "llm" and self.model is not None:
            raise ValueError("a model is only available for the LLM engine")
        if self.engine.root == "spacy":
            from frisket.ops.entities import canonicalize_entity_type
            from frisket.ops.spacy_ner import SPACY_CANONICAL_TYPES

            unsupported = [
                label
                for label in self.labels
                if canonicalize_entity_type(label) not in SPACY_CANONICAL_TYPES
            ]
            if unsupported:
                raise ValueError(
                    f"spacy does not support labels: {', '.join(unsupported)}"
                )
        return self


class EntityMention(TypedDict):
    text: str
    type: str
    start: int
    end: int
    score: float | None
    fingerprint: NotRequired[str]
    metadata: NotRequired[dict[str, str]]


class NerOutput(BaseModel):
    entities: list[EntityMention] = Field(
        json_schema_extra={
            "semantic_type": "entity_mentions",
            "named_result": {
                "schema": "entities_list",
                "may_feed": ["derive.table_from_list"],
            },
        }
    )


class EntityModelValue(TypedDict, total=False):
    text: str
    type: str
    label: str
    start: int
    end: int
    score: float | None


class NerModelOutput(BaseModel):
    """Provider values exclude server-derived fingerprints and metadata."""

    entities: list[EntityModelValue]


def normalize_ner_output(raw: list[dict[str, Any]]) -> NerOutput:
    return NerOutput(entities=canonicalize_entities(raw))


def ner_prompt(params: NerParams, row: Row) -> ModelPrompt[NerModelOutput]:
    system = "\n\n".join(
        (
            "You extract named entities from documents for an investigative "
            "journalist. Extract only entities that are actually present in "
            "the text; never invent one.",
            entities_extract_instruction(params.labels, params.extra_instructions),
            "Report entity text from the user message only. Character "
            "offsets are advisory; the server validates them independently.",
        )
    )
    user_content = (
        [{"type": "text", "text": ner_text(row.values)}]
        if len(row.values) == 1
        else render_input_block(dict(row.values))
    )
    return ModelPrompt(
        messages=(
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        )
    )


async def ner_direct(
    params: NerParams, row: Row, extractor: NerExtractor
) -> RowResult[NerOutput]:
    entities = await extractor.extract(
        ner_text(row.values), labels=params.labels, threshold=params.threshold
    )
    return RowResult(output=normalize_ner_output(entities))


def ner_complete(
    params: NerParams, row: Row, response: NerModelOutput
) -> RowResult[NerOutput]:
    return RowResult(output=normalize_ner_output(response.entities))


NER = action(
    name="ner",
    title="Extract named entities",
    description="Extract labeled named entities with spaCy, GLiNER, or an LLM.",
    category=ActionCategory.EXTRACT,
    examples=(NerParams(source=["text"], labels=list(RECOMMENDED_NER_LABELS)),),
    run=model_rows(ner_prompt, direct=ner_direct, complete=ner_complete),
)
