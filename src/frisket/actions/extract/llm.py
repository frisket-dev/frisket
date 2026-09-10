from __future__ import annotations

from typing import Any, Self

from pydantic import Field, field_validator, model_validator

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
    ModelPrompt,
    Row,
    RowResult,
)
from frisket.ops.extraction import extract_response_schema, render_extract_messages


class ExtractParams(_ModelRowsParams):
    instruction: str = ""
    fields: list[ExtractField] = Field(min_length=1, max_length=64)
    include_confidence: bool = False
    source_document_columns: list[ColumnRef[Any]] = Field(default_factory=list)
    grounding: ExtractGrounding | None = None
    evidence_policy: ExtractEvidencePolicy | None = None

    @field_validator("instruction")
    @classmethod
    def _instruction(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def _outputs_are_unique(self) -> Self:
        extraction_output_fields(
            self.fields, include_confidence=self.include_confidence
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
        complete=complete_extract,
        dynamic_outputs=extract_outputs,
    ),
)
