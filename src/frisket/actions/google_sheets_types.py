"""The semantic request consumed by the Google Sheets export terminal."""

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from frisket.actions.types import ActionParams


class GoogleSheetsSource(ActionParams):
    kind: Literal["current_sheet", "current_view", "all_sheets"]
    sheet_id: int | None = Field(default=None, ge=1, strict=True)
    query: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _source(self) -> "GoogleSheetsSource":
        if (self.kind == "all_sheets") != (self.sheet_id is None):
            raise ValueError("invalid_sheet_ref")
        if (self.kind == "current_view") != (self.query is not None):
            raise ValueError("invalid_query_spec")
        return self


class GoogleSheetsDestination(ActionParams):
    kind: Literal["google_sheets"]
    mode: Literal["new_spreadsheet", "update_existing"]
    spreadsheet_id: str | None = Field(default=None, min_length=1)
    spreadsheet_title: str | None = Field(default=None, min_length=1)

    @field_validator("spreadsheet_id", "spreadsheet_title")
    @classmethod
    def _nonblank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("Google Sheets destination values must be non-empty")
        return value

    @model_validator(mode="after")
    def _destination(self) -> "GoogleSheetsDestination":
        if (self.mode == "update_existing") != bool(self.spreadsheet_id):
            raise ValueError("invalid_export_destination")
        return self


class GoogleSheetsExportRequest(ActionParams):
    connection_id: str = Field(min_length=1)
    source: GoogleSheetsSource
    destination: GoogleSheetsDestination
    write_policy: Literal["replace_managed_tabs"] = "replace_managed_tabs"

    @field_validator("connection_id")
    @classmethod
    def _connection(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("connection_id must be non-empty")
        return value


class GoogleSheetsExportOutput(BaseModel):
    connection_id: str
    spreadsheet_id: str
    spreadsheet_url: str | None = None
    updated_tabs: list[dict[str, Any]] = Field(default_factory=list)
    receipt_id: str | None = None
