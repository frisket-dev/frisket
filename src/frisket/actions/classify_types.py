"""Classification field definitions and direct-engine capability."""

from collections.abc import Mapping, Sequence
from typing import Any, Literal, Protocol, Self

from pydantic import Field, field_validator, model_validator

from frisket.actions.types import ActionParams, Outcome, Row
from frisket.output_names import validate_runner_output_name


class ClassifyField(ActionParams):
    name: str = Field(min_length=1)
    type: Literal["category", "score", "integer", "number", "boolean", "text"] = (
        "category"
    )
    labels: list[str] = Field(default_factory=list)
    label_descriptions: dict[str, str] = Field(default_factory=dict)
    description: str = ""

    @field_validator("name")
    @classmethod
    def _safe_name(cls, value: str) -> str:
        return validate_runner_output_name(value)

    @model_validator(mode="after")
    def _labels(self) -> Self:
        if self.type == "category":
            if not self.labels or any(not label.strip() for label in self.labels):
                raise ValueError("category fields require non-empty labels")
            if len(self.labels) != len(set(self.labels)):
                raise ValueError("labels must be unique")
            if any(label not in self.labels for label in self.label_descriptions):
                raise ValueError("label descriptions must describe declared labels")
            if any(not text.strip() for text in self.label_descriptions.values()):
                raise ValueError("label descriptions must be non-empty")
        elif self.labels or self.label_descriptions:
            raise ValueError("only category fields declare labels")
        return self


class Classifier(Protocol):
    """An admitted direct engine classifying the supplied text and fields."""

    async def classify(
        self, row: Row, text: str, fields: Sequence[ClassifyField]
    ) -> Mapping[str, Outcome[Any]]: ...
