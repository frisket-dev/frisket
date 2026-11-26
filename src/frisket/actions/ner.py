"""Typed named-entity inputs and pure model request construction."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
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
    DynamicOutput,
    EngineRef,
    ModelPrompt,
    ModelRef,
    Row,
    RowResult,
    Template,
)
from frisket.ai.message_content import render_input_block
from frisket.ops.entities import canonicalize_entities, canonicalize_entity_type
from frisket.ops.ner_llm import entities_extract_instruction
from frisket.ops.ner_text import ner_text, text_occurrences


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
    extra_instructions: StrictStr = Field(
        default="",
        json_schema_extra={"x-frisket-input": "textarea"},
    )

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


def _requested_types(params: NerParams) -> list[str]:
    return list(
        dict.fromkeys(canonicalize_entity_type(label) for label in params.labels)
    )


def ner_response_schema(params: NerParams) -> dict[str, Any]:
    """Entities of the requested types; the server owns their positions.

    Offsets are nullable because ``ner_complete`` locates every entity in the
    source text itself. Fingerprints and metadata are server-derived and never
    requested.
    """

    entity = {
        "type": "object",
        "properties": {
            "text": {"type": "string"},
            "type": {"type": "string", "enum": _requested_types(params)},
            "start": {"type": ["integer", "null"]},
            "end": {"type": ["integer", "null"]},
            "score": {"type": ["number", "null"]},
        },
        "required": ["text", "type", "start", "end"],
    }
    return {
        "type": "object",
        "properties": {"entities": {"type": "array", "items": entity}},
        "required": ["entities"],
    }


_NOT_IN_SOURCE = "text not found in the source"
_REPEATED = "it repeats a mention already returned"


def _located_entities(
    source: str, entities: Sequence[Mapping[str, Any]]
) -> tuple[list[dict[str, Any]], tuple[str, ...]]:
    """Place each model entity on a distinct occurrence of its text.

    Stated offsets that already select the text are kept; every other entity
    then takes the free occurrence nearest its stated start, or the first.
    """

    spans: list[tuple[int, int] | None] = [None] * len(entities)
    taken: set[tuple[int, int]] = set()
    # Positions the model got right are reserved before any other is repaired.
    for index, entity in enumerate(entities):
        text, start, end = entity["text"], entity["start"], entity["end"]
        if (
            text
            and start is not None
            and end is not None
            and start >= 0
            and end - start == len(text)
            and source[start:end] == text
            and (start, end) not in taken
        ):
            spans[index] = (start, end)
            taken.add((start, end))
    occurrences: dict[str, list[tuple[int, int]]] = {}
    discarded = {_NOT_IN_SOURCE: 0, _REPEATED: 0}
    located: list[dict[str, Any]] = []
    for index, entity in enumerate(entities):
        span = spans[index]
        if span is None:
            text = entity["text"]
            if text not in occurrences:
                occurrences[text] = text_occurrences(source, text)
            free = [found for found in occurrences[text] if found not in taken]
            if not free:
                discarded[_REPEATED if occurrences[text] else _NOT_IN_SOURCE] += 1
                continue
            start = entity["start"]
            span = (
                min(free, key=lambda found: abs(found[0] - start))
                if start is not None
                else free[0]
            )
            taken.add(span)
        located.append({**entity, "start": span[0], "end": span[1]})
    warnings = tuple(
        f"{count} {'entity' if count == 1 else 'entities'} the model returned "
        f"{'was' if count == 1 else 'were'} discarded: {reason}."
        for reason, count in discarded.items()
        if count
    )
    return located, warnings


def normalize_ner_output(raw: Sequence[Mapping[str, Any]]) -> NerOutput:
    entities = canonicalize_entities(
        [dict(item) for item in raw if isinstance(item, Mapping)]
    )
    return NerOutput.model_validate({"entities": entities})


def ner_prompt(params: NerParams, row: Row) -> ModelPrompt[DynamicOutput]:
    system = "\n\n".join(
        (
            "You extract named entities from documents for an investigative "
            "journalist. Extract only entities that are actually present in "
            "the text; never invent one.",
            entities_extract_instruction(
                _requested_types(params), params.extra_instructions
            ),
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
        ),
        response_schema=ner_response_schema(params),
    )


async def ner_direct(
    params: NerParams, row: Row, extractor: NerExtractor
) -> RowResult[NerOutput]:
    entities = await extractor.extract(
        ner_text(row.values), labels=params.labels, threshold=params.threshold
    )
    return RowResult(output=normalize_ner_output(entities))


def ner_complete(
    params: NerParams, row: Row, response: DynamicOutput
) -> RowResult[NerOutput]:
    del params
    entities, warnings = _located_entities(
        ner_text(row.values), response.root["entities"]
    )
    return RowResult(output=normalize_ner_output(entities), warnings=warnings)


NER = action(
    name="ner",
    title="Extract named entities",
    description="Extract labeled named entities with spaCy, GLiNER, or an LLM.",
    category=ActionCategory.EXTRACT,
    examples=(
        NerParams(source=[NerColumn("text")], labels=list(RECOMMENDED_NER_LABELS)),
    ),
    run=model_rows(ner_prompt, direct=ner_direct, complete=ner_complete),
)
