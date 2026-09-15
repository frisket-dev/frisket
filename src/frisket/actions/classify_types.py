"""Classification field definitions and direct-engine capability."""

from collections.abc import Mapping, Sequence
from typing import Any, Literal, Protocol

from pydantic import Field, ValidationInfo, field_validator
from pydantic_core import PydanticCustomError

from frisket.actions.types import ActionParams, Outcome, Row
from frisket.output_names import validate_runner_output_name


class ClassifyField(ActionParams):
    name: str = Field(min_length=1)
    type: Literal["category", "score", "integer", "number", "boolean", "text"] = (
        "category"
    )
    labels: list[str] = Field(default_factory=list, validate_default=True)
    label_descriptions: dict[str, str] = Field(
        default_factory=dict, validate_default=True
    )
    description: str = ""

    @field_validator("name")
    @classmethod
    def _safe_name(cls, value: str) -> str:
        return validate_runner_output_name(value)

    @field_validator("labels")
    @classmethod
    def _labels(cls, value: list[str], info: ValidationInfo) -> list[str]:
        if info.data.get("type", "category") == "category":
            if not value or any(not label.strip() for label in value):
                raise PydanticCustomError(
                    "category_labels_required",
                    "category fields require non-empty labels",
                )
            if len(value) != len(set(value)):
                raise PydanticCustomError(
                    "category_labels_unique", "labels must be unique"
                )
        elif value:
            raise PydanticCustomError(
                "category_labels_forbidden",
                "only category fields declare labels",
            )
        return value

    @field_validator("label_descriptions")
    @classmethod
    def _label_descriptions(
        cls, value: dict[str, str], info: ValidationInfo
    ) -> dict[str, str]:
        if info.data.get("type", "category") != "category":
            if value:
                raise PydanticCustomError(
                    "category_labels_forbidden",
                    "only category fields declare labels",
                )
            return value
        labels = info.data.get("labels", ())
        if any(label not in labels for label in value):
            raise PydanticCustomError(
                "category_label_descriptions_unknown",
                "label descriptions must describe declared labels",
            )
        if any(not text.strip() for text in value.values()):
            raise PydanticCustomError(
                "category_label_descriptions_required",
                "label descriptions must be non-empty",
            )
        return value


class Classifier(Protocol):
    """An admitted direct engine classifying the supplied text and fields."""

    async def classify(
        self, row: Row, text: str, fields: Sequence[ClassifyField]
    ) -> Mapping[str, Outcome[Any]]: ...
