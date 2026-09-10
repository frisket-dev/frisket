"""Private physical plan for one admitted grouped summary."""

from typing import Any

from pydantic import BaseModel, ConfigDict


class GroupSummaryPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    action_kind: str
    request: dict[str, Any]
    sheet_id: int
    input_columns: list[str]
    group_by: str | None
    model: str
    instruction: str
    target_sheet_name: str
    group_column_name: str
    row_count_column_name: str
    summary_column_name: str
    row_ids: list[int] | None
