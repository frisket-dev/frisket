"""Actual grouped-summary arguments and invocation-owned preparation."""

from dataclasses import dataclass
from typing import Any, ClassVar, Protocol

from pydantic import Field, StrictStr, field_validator

from frisket.actions.types import ActionParams, ColumnRef, ModelRef, TableColumn


GROUP_SUMMARY_COLUMNS = (
    TableColumn("group", "text"),
    TableColumn("rows", "integer"),
    TableColumn("summary", "text"),
)


class GroupSummaryColumn(ColumnRef[Any]):
    excluded_column_types: ClassVar[tuple[str, ...]] = ("file",)


class GroupSummaryOptions(ActionParams):
    model: ModelRef
    instruction: StrictStr = Field(min_length=1)
    group_by: ColumnRef[Any] | None = None

    @field_validator("instruction")
    @classmethod
    def nonblank_instruction(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("instruction must not be blank")
        return value


@dataclass(frozen=True, eq=False)
class PreparedGroupSummary:
    row_count: int
    group_count: int


class GroupSummaryOutput(ActionParams):
    row_count: int
    group_count: int


class GroupSummarizer(Protocol):
    def prepare(
        self,
        source: list[GroupSummaryColumn],
        *,
        options: GroupSummaryOptions,
    ) -> PreparedGroupSummary: ...
