"""Local semantic and model classification sharing one output definition."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any, Literal, Self

from pydantic import Field, StrictStr, TypeAdapter, field_validator, model_validator

from frisket.actions.classify_types import Classifier, ClassifyField
from frisket.actions.core import (
    ActionCategory,
    _validate_model_rows_source,
    action,
    model_rows,
)
from frisket.actions.model_rows import RichSource
from frisket.actions.types import (
    ActionParams,
    DynamicOutput,
    EngineRef,
    ModelPrompt,
    ModelRef,
    Outcome,
    Row,
    RowError,
    RowResult,
)
from frisket.ai.message_content import render_input_block
from frisket.output_names import validate_runner_output_family


CLASSIFY_ENGINES = ("local_semantic", "llm")

Score = Annotated[int, Field(ge=0, le=10)]
Confidence = Annotated[float, Field(ge=0, le=1)]

# Value type each field materializes as; ``category`` is the closed label set.
_FIELD_VALUE_TYPES: dict[str, Any] = {
    "score": Score,
    "integer": int,
    "number": float,
    "boolean": bool,
    "text": str,
}


class ClassifyParams(ActionParams):
    source: RichSource = Field(title="Input columns")
    engine: EngineRef[Classifier] = Field(
        default=EngineRef[Classifier]("local_semantic"),
        title="Engine",
        description="Local semantic (FastEmbed) or Model (hosted LLM).",
    )
    model: ModelRef | None = Field(
        default=None,
        description="Provider/model id; required only for the Model engine.",
    )
    context: StrictStr = Field(
        default="",
        description="Optional context about the dataset.",
    )
    fields: list[ClassifyField] = Field(min_length=1, max_length=64)
    include_justification: bool = False
    include_confidence: bool = False

    @field_validator("source")
    @classmethod
    def _unique_source(cls, value: RichSource) -> RichSource:
        return _validate_model_rows_source(value)

    @field_validator("engine")
    @classmethod
    def _known_engine(cls, value: EngineRef[Classifier]) -> EngineRef[Classifier]:
        if value.root not in CLASSIFY_ENGINES:
            raise ValueError("engine must be local_semantic or llm")
        return value

    @model_validator(mode="after")
    def _engine_contract(self) -> Self:
        try:
            validate_runner_output_family(field.name for field in self.fields)
        except ValueError as error:
            raise ValueError("field names must be unique") from error
        if self.engine.root == "llm":
            if self.model is None:
                raise ValueError("the llm engine requires a model")
            return self
        if self.model is not None:
            raise ValueError("the local_semantic engine does not use a model")
        if len(self.fields) != 1 or self.fields[0].type != "category":
            raise ValueError("local_semantic requires exactly one category field")
        if self.include_justification or self.include_confidence:
            raise ValueError("local_semantic emits only the winning label")
        return self


def _field_value_type(field: ClassifyField) -> Any:
    if field.type == "category":
        # The closed label set: materializes as ``category``; a value outside
        # the declared labels fails the row.
        return Literal[tuple(field.labels)]  # type: ignore[valid-type]
    return _FIELD_VALUE_TYPES[field.type]


def _companion_outputs(params: ClassifyParams) -> dict[str, Any]:
    """The retired recipe's companion columns, preserved for the ``llm`` engine.

    Confidence and justification ride each field's ``Outcome`` (the host
    materializes them as cell metadata for review) AND, when requested, as
    plain columns so they stay filterable, sortable, exportable and chainable.
    Justification is per field; confidence is first-field-only, exactly as
    before. ``local_semantic`` emits only the winning label (its validator
    rejects ``include_*``), so it never declares these.
    """

    outputs: dict[str, Any] = {}
    if params.include_justification:
        for field in params.fields:
            outputs[f"{field.name}_justification"] = str | None
    if params.include_confidence:
        outputs[f"{params.fields[0].name}_confidence"] = Confidence | None
    return outputs


def classify_outputs(params: ClassifyParams) -> dict[str, Any]:
    """Project ``params.fields`` into the frozen dynamic output schema.

    Every field is one ``Outcome`` column of its declared value type, followed
    by any requested companion columns.
    """

    outputs: dict[str, Any] = {
        field.name: Outcome[_field_value_type(field)]  # type: ignore[valid-type]
        for field in params.fields
    }
    outputs.update(_companion_outputs(params))
    return outputs


def classify_response_schema(params: ClassifyParams) -> dict[str, Any]:
    """Provider values use exactly the constraints of the declared outputs."""

    properties: dict[str, dict[str, Any]] = {}
    for field in params.fields:
        schema = TypeAdapter(_field_value_type(field)).json_schema()
        if field.description:
            schema["description"] = field.description
        if field.type == "score":
            schema.setdefault("description", "Score from 0 to 10")
        properties[field.name] = schema
        if params.include_justification:
            properties[f"{field.name}_justification"] = {
                "type": "string",
                "description": f"One-sentence reason for {field.name}",
            }
    if params.include_confidence:
        properties[f"{params.fields[0].name}_confidence"] = {
            **TypeAdapter(Confidence).json_schema(),
            "description": "Self-assessed confidence 0.0-1.0",
        }
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties.keys()),
    }


def classify_prompt(params: ClassifyParams, row: Row) -> ModelPrompt[DynamicOutput]:
    """The complete request used for pricing, cache lookup, and execution."""

    field_lines: list[str] = []
    for field in params.fields:
        line = f"- {field.name}: {field.description or field.name}"
        if field.labels:
            choices = "; ".join(
                f"{label}: {field.label_descriptions[label]}"
                if field.label_descriptions.get(label)
                else label
                for label in field.labels
            )
            line += f" (one of: {choices})"
        field_lines.append(line)
    fields_desc = "\n".join(field_lines)
    system = (
        "You are a careful analyst classifying rows of a dataset for an "
        "investigative journalist. Judge only from the provided content. "
        "Scores run 0 (not at all) to 10 (extremely). "
        "Be consistent across rows; do not invent facts."
    )
    if params.context:
        system += f"\n\nDataset context: {params.context}"
    user_parts = render_input_block(dict(row.values))
    user_parts.append(
        {
            "type": "text",
            "text": f"\nAssess the content above on:\n{fields_desc}",
        }
    )
    return ModelPrompt(
        messages=(
            {"role": "system", "content": system},
            {"role": "user", "content": user_parts},
        ),
        response_schema=classify_response_schema(params),
    )


def _source_text(row_values: dict[str, Any]) -> str:
    return "\n".join(
        str(value)
        for value in row_values.values()
        if value is not None and not isinstance(value, dict)
    )


async def classify_row(
    params: ClassifyParams,
    row: Row,
    classifier: Classifier,
) -> RowResult[DynamicOutput]:
    """Execute the direct engine using its actual text and field arguments."""

    outcomes = await classifier.classify(
        row, _source_text(dict(row.values)), tuple(params.fields)
    )
    return _publish_outcomes(params, outcomes)


def _publish_outcomes(
    params: ClassifyParams, outcomes: Mapping[str, Outcome[Any]]
) -> RowResult[DynamicOutput]:
    missing = [field.name for field in params.fields if field.name not in outcomes]
    if missing:
        raise RowError(
            "classify_output_missing",
            f"classifier returned no outcome for {', '.join(missing)}",
        )
    values: dict[str, Any] = {
        field.name: outcomes[field.name] for field in params.fields
    }
    # Companion columns derive from the same Outcome the host keeps as cell
    # metadata: one source of truth, materialized both ways (see
    # _companion_outputs). Failed outcomes carry no assessment.
    if params.include_justification:
        for field in params.fields:
            outcome = outcomes[field.name]
            values[f"{field.name}_justification"] = getattr(
                outcome, "justification", None
            )
    if params.include_confidence:
        first = outcomes[params.fields[0].name]
        values[f"{params.fields[0].name}_confidence"] = getattr(
            first, "confidence", None
        )
    return RowResult(output=DynamicOutput(values))


def classify_complete(
    params: ClassifyParams, row: Row, response: DynamicOutput
) -> RowResult[DynamicOutput]:
    """Convert validated model values to cell outcomes and companion columns."""
    del row
    values = response.root
    outcomes = {}
    for index, field in enumerate(params.fields):
        outcomes[field.name] = Outcome.ok(
            values[field.name],
            justification=values.get(f"{field.name}_justification")
            if params.include_justification
            else None,
            confidence=values.get(f"{field.name}_confidence")
            if params.include_confidence and index == 0
            else None,
        )
    return _publish_outcomes(params, outcomes)


CLASSIFY = action(
    name="classify",
    title="Classify rows",
    description="Assign labels locally or classify structured fields with a model.",
    category=ActionCategory.TEXT,
    examples=(
        ClassifyParams(
            source=["story"],
            fields=[
                ClassifyField(name="topic", labels=["housing", "transit", "other"])
            ],
        ),
    ),
    run=model_rows(
        classify_prompt,
        direct=classify_row,
        complete=classify_complete,
        dynamic_outputs=classify_outputs,
    ),
)
