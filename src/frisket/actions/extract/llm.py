from __future__ import annotations

from typing import Any, Self

from pydantic import Field, field_validator, model_validator

from frisket.actions.classify_types import Classifier, ClassifyField
from frisket.actions.core import ActionCategory, action, model_rows
from frisket.actions.extract.grounding import complete_extraction
from frisket.actions.extraction_types import (
    ExtractEvidencePolicy,
    ExtractField,
    ExtractGrounding,
    extraction_output_fields,
)
from frisket.actions.model_rows import _ModelRowsParams
from frisket.actions.types import (
    ColumnRef,
    DynamicOutput,
    EngineRef,
    ModelPrompt,
    ModelRef,
    Outcome,
    Row,
    RowError,
    RowResult,
)
from frisket.contracts.classification import CLEF_ENGINE_IDS, validate_classification
from frisket.contracts.clef import (
    classification_context,
    classification_text,
    clef_questions,
    validate_clef_request,
)
from frisket.ops.extraction import extract_response_schema, render_extract_messages


class ExtractParams(_ModelRowsParams):
    engine: EngineRef[Classifier] = Field(
        default=EngineRef[Classifier]("llm"),
        title="Engine",
        description="Model, Clef-flash (model server), or Clef (Cloudflare).",
    )
    model: ModelRef | None = Field(
        default=None,
        description="Provider/model id; required only for the Model engine.",
    )
    instruction: str = Field(
        default="",
        json_schema_extra={"x-frisket-input": "textarea"},
    )
    fields: list[ExtractField] = Field(min_length=1, max_length=64)
    include_confidence: bool = False
    source_document_columns: list[ColumnRef[Any]] = Field(default_factory=list)
    grounding: ExtractGrounding | None = Field(default=ExtractGrounding(enabled=True))
    evidence_policy: ExtractEvidencePolicy | None = None

    @field_validator("engine")
    @classmethod
    def _known_engine(cls, value: EngineRef[Classifier]) -> EngineRef[Classifier]:
        if value.root not in {"llm", *CLEF_ENGINE_IDS}:
            raise ValueError("engine must be one of llm, clef-flash, clef")
        return value

    @field_validator("instruction")
    @classmethod
    def _instruction(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def _outputs_are_unique(self) -> Self:
        extraction_output_fields(
            self.fields, include_confidence=self.include_confidence
        )
        if self.engine.root == "llm":
            if self.model is None:
                raise ValueError("the llm engine requires a model")
            return self
        if self.model is not None:
            raise ValueError(f"the {self.engine.root} engine does not use a model")
        validate_classification(
            self.engine.root,
            [field.model_dump() for field in self.fields],
            include_confidence=self.include_confidence,
        )
        validate_clef_request(
            self.engine.root,
            clef_questions(
                [field.model_dump() for field in self.fields],
                classification_context(self.model_dump()),
            ),
        )
        if (
            self.grounding
            and (self.grounding.enabled or self.grounding.citation_required)
        ) or (self.evidence_policy and self.evidence_policy.citation_required):
            raise ValueError(
                "Clef does not support citations or grounding. "
                "Turn Citations Off or choose a compatible model; "
                "citation-required policies need a compatible model."
            )
        return self


def extract_outputs(params: ExtractParams):
    return extraction_output_fields(
        params.fields, include_confidence=params.include_confidence
    )


def extract(params: ExtractParams, row: Row) -> ModelPrompt[DynamicOutput]:
    fields = [
        {"name": key, "schema": dict(field.schema)}
        for key, field in extract_outputs(params).items()
    ]
    grounding = bool(params.grounding and params.grounding.enabled)
    return ModelPrompt(
        messages=tuple(
            render_extract_messages(
                dict(row.values),
                fields,
                instruction=params.instruction or "Extract the requested fields.",
                context=params.context,
                grounding_enabled=grounding,
            )
        ),
        response_schema=extract_response_schema(
            fields,
            grounding_enabled=grounding,
            grounding_fields={field.name for field in params.fields},
            nullable=True,
        ),
    )


def complete_extract(
    params: ExtractParams, row: Row, response: DynamicOutput
) -> RowResult[DynamicOutput]:
    del row
    return complete_extraction(
        fields=params.fields,
        include_confidence=params.include_confidence,
        grounding=params.grounding,
        response=response,
    )


async def extract_row(
    params: ExtractParams, row: Row, classifier: Classifier
) -> RowResult[DynamicOutput]:
    """Keep each admitted decision and probability in its original cell outcome."""
    fields = tuple(
        ClassifyField(
            name=field.name,
            type=field.type,
            description=field.description,
            labels=field.labels,
        )
        for field in params.fields
    )
    outcomes = await classifier.classify(row, classification_text(row.values), fields)
    missing = [field.name for field in params.fields if field.name not in outcomes]
    if missing:
        raise RowError(
            "classify_output_missing",
            f"classifier returned no outcome for {', '.join(missing)}",
        )
    values: dict[str, Any] = {
        field.name: outcomes[field.name] for field in params.fields
    }
    if params.include_confidence:
        first = outcomes[params.fields[0].name]
        values[f"{params.fields[0].name}_confidence"] = Outcome.ok(
            getattr(first, "confidence", None)
        )
    return RowResult(output=DynamicOutput(values))


EXTRACT = action(
    examples=(
        ExtractParams(
            source=["source"],
            model="anthropic/claude-haiku-4-5",
            fields=[ExtractField(name="summary", type="text")],
        ),
    ),
    name="extract",
    title="Extract structured fields",
    category=ActionCategory.TEXT,
    description="Extract typed fields from each row, with optional source citations.",
    run=model_rows(
        extract,
        source_param="source",
        direct=extract_row,
        complete=complete_extract,
        dynamic_outputs=extract_outputs,
    ),
)
