"""Exhaustive grounded finding options and invocation-owned preparation."""

from dataclasses import dataclass
from typing import Annotated, Any, ClassVar, Literal, Protocol

from pydantic import Field, StrictStr, model_validator

from frisket.actions.types import ActionParams, ColumnRef, ModelRef
from frisket.actions.extraction_types import ExtractField
from frisket.contracts.actions.schemas._validators import nonblank


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
    instruction: Annotated[StrictStr, nonblank("instruction must not be blank")] = (
        Field(
            title="Describe what to look for",
            min_length=1,
            json_schema_extra={"x-frisket-input": "textarea"},
        )
    )
    model: ModelRef
    fields: list[ExtractField] = Field(default_factory=list, max_length=63)

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
