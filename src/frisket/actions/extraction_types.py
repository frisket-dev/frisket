"""The field definitions shared by extraction requests, replies, and outputs."""

from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING, Any, Literal, Self

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import Field, StrictStr, model_validator

from frisket.actions.types import ActionParams, Outcome
from frisket.actions.python_types import NamedResultTarget
from frisket.output_names import (
    validate_runner_output_family,
    validate_runner_output_name,
)

if TYPE_CHECKING:
    from frisket.actions.core import OutputField


_FIELD_TYPES = {
    "score": (int, "integer", "integer"),
    "integer": (int, "integer", "integer"),
    "number": (float, "number", "number"),
    "boolean": (bool, "boolean", "boolean"),
    "text": (str, "text", "string"),
    "category": (str, "category", "string"),
    "date": (str, "date", "string"),
    "list": (list[Any], "json", "array"),
    "json": (dict[str, Any], "json", "object"),
}


class ExtractField(ActionParams):
    name: StrictStr
    type: Literal[
        "score",
        "integer",
        "number",
        "boolean",
        "text",
        "category",
        "date",
        "list",
        "json",
    ]
    description: StrictStr = ""
    labels: list[StrictStr] = Field(default_factory=list)
    items: dict[str, Any] | None = None
    properties: dict[str, Any] | None = None
    required: bool = False

    @model_validator(mode="after")
    def valid_field(self) -> Self:
        self.name = validate_runner_output_name(self.name)
        if self.type == "category":
            if not self.labels or len(self.labels) != len(set(self.labels)):
                raise ValueError("A category field needs unique labels")
        elif self.labels:
            raise ValueError("Only category fields accept labels")
        if self.items is not None and self.type != "list":
            raise ValueError("Only list fields accept an item schema")
        if self.properties is not None and self.type != "json":
            raise ValueError("Only JSON fields accept object properties")
        try:
            Draft202012Validator.check_schema(self.value_schema())
        except SchemaError as error:
            raise ValueError(
                "The extraction field has an invalid JSON schema"
            ) from error
        return self

    @property
    def value_type(self) -> Any:
        return _FIELD_TYPES[self.type][0]

    @property
    def column_type(self) -> str:
        return _FIELD_TYPES[self.type][1]

    def value_schema(self) -> dict[str, Any]:
        schema: dict[str, Any] = {"type": _FIELD_TYPES[self.type][2]}
        if self.description:
            schema["description"] = self.description
        if self.type == "score":
            schema.update(minimum=0, maximum=10)
            schema.setdefault("description", "Score from 0 to 10")
        elif self.type == "category":
            schema["enum"] = list(self.labels)
        elif self.type == "list":
            schema["items"] = (
                deepcopy(self.items) if self.items is not None else {"type": "string"}
            )
        elif self.type == "json" and self.properties:
            schema["properties"] = deepcopy(self.properties)
            schema["required"] = list(self.properties)
        return schema


class ExtractGrounding(ActionParams):
    enabled: bool = False
    citation_required: bool | None = None
    allowed_methods: list[
        Literal[
            "model_bbox",
            "model_text_offset",
            "exact_quote",
            "posthoc_alignment",
            "file_page_range",
        ]
    ] = Field(default_factory=list)
    include_stale_inputs: bool = False


class ExtractEvidencePolicy(ActionParams):
    citation_required: bool = False


def extraction_output_fields(
    fields: list[ExtractField], *, include_confidence: bool = False
) -> dict[str, OutputField]:
    """One declaration produces published schemas, value types, and list routes."""
    from frisket.actions.core import OutputField

    names = [field.name for field in fields]
    confidence_name = f"{names[0]}_confidence" if names and include_confidence else None
    validate_runner_output_family(names)
    outputs = {
        field.name: OutputField(
            key=field.name,
            column_type=field.column_type,
            schema=field.value_schema(),
            annotation=Outcome[field.value_type],
            named_result=(
                NamedResultTarget(
                    kind="named_result",
                    schema=f"{field.name}_list",
                    may_feed=["derive.table_from_list"],
                )
                if field.type == "list"
                else None
            ),
        )
        for field in fields
    }
    if confidence_name:
        outputs[confidence_name] = OutputField(
            key=confidence_name,
            column_type="number",
            annotation=Outcome[float],
            schema={
                "type": "number",
                "minimum": 0,
                "maximum": 1,
                "description": "Self-assessed confidence 0.0-1.0",
            },
        )
    return outputs
