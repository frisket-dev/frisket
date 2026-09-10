"""Exhaustive grounded finding options and invocation-owned preparation."""

from dataclasses import dataclass
from typing import Any, ClassVar, Literal, Protocol

from pydantic import Field, StrictStr, field_validator, model_validator

from frisket.actions.types import ActionParams, ColumnRef, ModelRef
from frisket.actions.extraction_types import ExtractField


class FindSourceColumn(ColumnRef[Any]):
    accepted_column_types: ClassVar[tuple[str, ...]] = (
        "text",
        "timestamped_transcript",
        "audio",
        "video",
        "image",
        "file",
    )


class FindOptions(ActionParams):
    instruction: StrictStr = Field(min_length=1)
    model: ModelRef
    fields: list[ExtractField] = Field(default_factory=list, max_length=63)

    @field_validator("instruction")
    @classmethod
    def nonblank_instruction(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("instruction must not be blank")
        return value

    @model_validator(mode="after")
    def valid_fields(self):
        names = [field.name.strip().casefold() for field in self.fields]
        if (
            any(not name for name in names)
            or len(names) != len(set(names))
            or "match" in names
        ):
            raise ValueError(
                "finding detail fields need distinct nonblank names other than match"
            )
        if any(field.required for field in self.fields):
            raise ValueError("finding detail fields must be optional")
        return self


@dataclass(frozen=True, eq=False)
class PreparedFind:
    source_row_count: int
    window_count: int


class FindOutput(ActionParams):
    source_row_count: int
    match_count: int
    coverage_status: Literal["complete", "incomplete"]


class FindScanner(Protocol):
    def prepare(
        self, source: FindSourceColumn, *, options: FindOptions
    ) -> PreparedFind: ...
