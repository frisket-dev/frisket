"""The semantic request consumed by the Google Sheets export terminal."""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, model_validator

from frisket.actions.types import ActionParams
from frisket.contracts.view import ViewSelection
from frisket.contracts.actions.schemas._validators import nonblank


class GoogleSheetsSource(ActionParams):
    kind: Literal["current_sheet", "current_view", "all_sheets"]
    sheet_id: int | None = Field(default=None, ge=1, strict=True)
    query: dict[str, Any] | None = None
    selection: ViewSelection | None = None
    scope_row_ids: list[int] | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def _source(self) -> "GoogleSheetsSource":
        if (self.kind == "all_sheets") != (self.sheet_id is None):
            raise ValueError("invalid_sheet_ref")
        if (self.kind == "current_view") != (
            self.query is not None or self.selection is not None
        ):
            raise ValueError("invalid_query_spec")
        if self.selection is not None:
            if (
                self.query is not None
                or self.scope_row_ids is not None
                or self.selection.scope.kind != "sheet"
                or self.selection.scope.sheet_id != self.sheet_id
            ):
                raise ValueError("invalid_view_selection")
        if self.scope_row_ids is not None:
            if not all(
                isinstance(row_id, int) and not isinstance(row_id, bool) and row_id > 0
                for row_id in self.scope_row_ids
            ) or len(set(self.scope_row_ids)) != len(self.scope_row_ids):
                raise ValueError("invalid_scope_row_ids")
        return self


class GoogleSheetsDestination(ActionParams):
    kind: Literal["google_sheets"]
    mode: Literal["new_spreadsheet", "update_existing"]
    spreadsheet_id: Annotated[
        str | None,
        nonblank("Google Sheets destination values must be non-empty", strip=False),
    ] = Field(default=None, min_length=1)
    spreadsheet_title: Annotated[
        str | None,
        nonblank("Google Sheets destination values must be non-empty", strip=False),
    ] = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _destination(self) -> "GoogleSheetsDestination":
        if (self.mode == "update_existing") != bool(self.spreadsheet_id):
            raise ValueError("invalid_export_destination")
        return self


class GoogleSheetsExportRequest(ActionParams):
    connection_id: Annotated[
        str, nonblank("connection_id must be non-empty", strip=False)
    ] = Field(min_length=1)
    source: GoogleSheetsSource
    destination: GoogleSheetsDestination
    write_policy: Literal["replace_managed_tabs"] = "replace_managed_tabs"


class GoogleSheetsExportOutput(BaseModel):
    connection_id: str
    spreadsheet_id: str
    spreadsheet_url: str | None = None
    updated_tabs: list[dict[str, Any]] = Field(default_factory=list)
    receipt_id: str | None = None
