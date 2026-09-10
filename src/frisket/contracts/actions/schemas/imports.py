"""Imports action schemas."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    Field,
    model_validator,
)

from frisket.authoring import column_types

from frisket.contracts.actions.schemas._base import (
    ContractModel,
    MAX_IMPORT_ROWS_COLUMNS,
    _is_json_value,
    _is_v1_column_type,
    canonical_column_type,
)


class ImportRowsSource(ContractModel):
    kind: Literal["inline", "file"]
    label: str | None = None
    fingerprint: str | None = None
    path: str | None = None
    importer: str | None = None
    line_count: int | None = Field(default=None, ge=0)
    request_hash: str | None = None
    geometry_column: str | None = None
    property_columns: list[str] = Field(default_factory=list)
    skipped_features: list[int] = Field(default_factory=list)
    blob_refs: list[dict[str, Any]] = Field(default_factory=list)


class ImportRowsColumn(ContractModel):
    name: str = Field(min_length=1)
    source_name: str | None = Field(default=None, min_length=1)
    type: str = Field(min_length=1)
    format: str | None = None
    hidden: bool = False


def _validate_import_columns(
    columns: list[ImportRowsColumn],
) -> list[ImportRowsColumn]:
    names = [column.name for column in columns]
    if len(names) != len(set(names)):
        raise ValueError("duplicate_column_name")
    for column in columns:
        if not _is_v1_column_type(column.type):
            raise ValueError("invalid_column_type")
    return columns


ValidatedImportColumns = Annotated[
    list[ImportRowsColumn],
    AfterValidator(_validate_import_columns),
]


class ImportRowsParams(ContractModel):
    sheet_name: str = Field(min_length=1)
    mode: Literal["create_sheet"]
    columns: ValidatedImportColumns = Field(
        min_length=1, max_length=MAX_IMPORT_ROWS_COLUMNS
    )
    rows: list[dict[str, Any]]
    rows_ref: dict[str, Any] | None = None
    source: ImportRowsSource | None = None

    @model_validator(mode="after")
    def _validate_row_contract(self) -> "ImportRowsParams":
        if self.rows_ref is not None:
            raise ValueError("invalid_rows_ref")
        self.rows = normalize_import_rows(self.columns, self.rows)
        return self


def normalize_import_rows(
    columns: list[ImportRowsColumn], rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    expected = {column.name for column in columns}
    types = {column.name: column.type for column in columns}
    normalized_rows: list[dict[str, Any]] = []
    for row in rows:
        if set(row) != expected:
            raise ValueError("row_shape_mismatch")
        normalized_row: dict[str, Any] = {}
        for name, value in row.items():
            normalized_row[name] = normalize_import_value(types[name], value)
        normalized_rows.append(normalized_row)
    return normalized_rows


def normalize_import_value(type_name: str, value: Any) -> Any:
    """Convert one supplied value once, then validate its stored representation."""
    if not _is_json_value(value):
        raise ValueError("invalid_params")
    try:
        parsed = column_types.parse_value(canonical_column_type(type_name), value)
    except Exception as exc:
        raise ValueError("invalid_params") from exc
    validate_import_value(type_name, parsed)
    return parsed


def validate_import_value(type_name: str, value: Any) -> None:
    """Validate an already-normalized value without rerunning its parser.

    Temporal anchors still require the executor's project-specific preflight.
    """
    if not _is_json_value(value) or not column_types.validate_value(
        canonical_column_type(type_name), value
    ):
        raise ValueError("invalid_params")


class ImportRowsOutput(ContractModel):
    sheet_id: int
    row_count: int
    column_ids: dict[str, int]
    op_ids: list[int] = Field(default_factory=list)
    evidence_refs: list[dict[str, Any]] = Field(default_factory=list)
    receipt_id: str | None = None
