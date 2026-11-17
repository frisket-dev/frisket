"""Actual grouped-summary arguments and invocation-owned preparation."""

from dataclasses import dataclass
from typing import Annotated, Any, ClassVar, Protocol

from pydantic import Field, StrictStr

from frisket.actions.types import ActionParams, ColumnRef, ModelRef, TableColumn
from frisket.contracts.actions.schemas._validators import nonblank


GROUP_SUMMARY_COLUMNS = (
    TableColumn("group", "text"),
    TableColumn("rows", "integer"),
    TableColumn("summary", "text"),
)


class GroupSummaryColumn(ColumnRef[Any]):
    excluded_column_types: ClassVar[tuple[str, ...]] = ("file",)


class GroupSummaryOptions(ActionParams):
    model: ModelRef
    instruction: Annotated[StrictStr, nonblank("instruction must not be blank")] = (
        Field(
            min_length=1,
            json_schema_extra={"x-frisket-input": "textarea"},
        )
    )
    group_by: ColumnRef[Any] | None = None


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
