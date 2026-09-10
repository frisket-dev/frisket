"""Physical source and materialization plan for an admitted exhaustive scan."""

from typing import Any

from pydantic import BaseModel, ConfigDict

from frisket.actions.extraction_types import ExtractField


class FindOperation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    action_kind: str
    request: dict[str, Any]
    sheet_id: int
    source_column: str
    row_ids: list[int] | None
    model: str
    instruction: str
    fields: list[ExtractField]
    target_sheet_name: str
    output_names: dict[str, str]
