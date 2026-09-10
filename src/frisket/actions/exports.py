"""Typed declarations for local artifact and Google Sheets exports."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from frisket.actions.core import (
    ActionCategory,
    ExportTarget,
    action,
    google_sheets_export,
)
from frisket.actions.google_sheets_types import GoogleSheetsExportRequest
from frisket.actions.types import (
    ActionParams,
    ColumnTablesDestination,
    ColumnTablesExport,
    ColumnTablesExporter,
    ExportedCsvSheetFile,
    ExportedJsonlSheetFile,
    ExportedParquetSheetFile,
    ExportedWorkLog,
    SheetCsvExporter,
    SheetJsonlExporter,
    SheetParquetExporter,
    WorkLogExporter,
)


class LocalFileDestination(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["local_file"]
    path: str = Field(min_length=1)

    @field_validator("path")
    @classmethod
    def _nonblank_path(cls, value: str) -> str:
        if not value.strip() or value != value.strip():
            raise ValueError("path must be non-empty and trimmed")
        return value


class ExportWorkLogParams(ActionParams):
    destination: LocalFileDestination
    include_receipts: bool = Field(default=True, strict=True)


class ExportColumnTablesParams(ActionParams):
    sheet_id: int = Field(ge=1, strict=True)
    column_id: int = Field(ge=1, strict=True)
    group_by: str | None = Field(default=None, min_length=1)
    exclude_columns: list[str] | None = Field(default=None, min_length=1)
    name_template: str = "{row:03d}_{table:03d}.csv"
    first_row_header: bool = Field(default=False, strict=True)
    destination: ColumnTablesDestination

    @field_validator("group_by", "name_template")
    @classmethod
    def _nonblank_name(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("name must be non-empty")
        return value

    @field_validator("exclude_columns")
    @classmethod
    def _nonblank_excludes(cls, value: list[str] | None) -> list[str] | None:
        if value is not None and any(not name.strip() for name in value):
            raise ValueError("exclude_columns must contain non-empty names")
        return value


class ExportSheetCsvParams(ActionParams):
    sheet_id: int = Field(ge=1, strict=True)
    destination: LocalFileDestination
    query: dict[str, Any] | None = None
    formula_policy: Literal["escape", "raw"] = "escape"


class ExportSheetJsonlParams(ActionParams):
    sheet_id: int = Field(ge=1, strict=True)
    destination: LocalFileDestination
    query: dict[str, Any] | None = None


class ExportSheetParquetParams(ActionParams):
    sheet_id: int = Field(ge=1, strict=True)
    destination: LocalFileDestination
    query: dict[str, Any] | None = None


def export_work_log(
    params: ExportWorkLogParams, exporter: WorkLogExporter
) -> ExportedWorkLog:
    return exporter.write_work_log(
        path=params.destination.path,
        include_receipts=params.include_receipts,
    )


def export_column_tables(
    params: ExportColumnTablesParams, exporter: ColumnTablesExporter
) -> ColumnTablesExport:
    return exporter.write_column_tables(
        sheet_id=params.sheet_id,
        column_id=params.column_id,
        destination=params.destination,
        group_by=params.group_by,
        exclude_columns=params.exclude_columns,
        name_template=params.name_template,
        first_row_header=params.first_row_header,
    )


def export_sheet_csv(
    params: ExportSheetCsvParams, exporter: SheetCsvExporter
) -> ExportedCsvSheetFile:
    return exporter.write_sheet_csv(
        sheet_id=params.sheet_id,
        path=params.destination.path,
        query=params.query,
        formula_policy=params.formula_policy,
    )


def export_sheet_jsonl(
    params: ExportSheetJsonlParams, exporter: SheetJsonlExporter
) -> ExportedJsonlSheetFile:
    return exporter.write_sheet_jsonl(
        sheet_id=params.sheet_id,
        path=params.destination.path,
        query=params.query,
    )


def export_sheet_parquet(
    params: ExportSheetParquetParams, exporter: SheetParquetExporter
) -> ExportedParquetSheetFile:
    return exporter.write_sheet_parquet(
        sheet_id=params.sheet_id,
        path=params.destination.path,
        query=params.query,
    )


WORK_LOG = action(
    examples=(
        ExportWorkLogParams(
            destination=LocalFileDestination(
                kind="local_file", path="exports/work-log.md"
            )
        ),
    ),
    name="work_log",
    title="Export work log",
    description=(
        "Render the current project state and v1 receipts into a Markdown "
        "work-log artifact, write it to a local file, and record the "
        "artifact hash/path in a receipt."
    ),
    category=ActionCategory.CONVERT,
    run=export_work_log,
    form="work_log_export",
)


COLUMN_TABLES = action(
    examples=(
        ExportColumnTablesParams(
            sheet_id=1,
            column_id=1,
            destination={"kind": "project_file", "prefix": "tables"},
        ),
    ),
    name="column_tables",
    title="Export column tables",
    description=(
        "Export a JSON list-valued sheet column as one CSV per row and optional "
        "group, packaged into a ZIP with a provenance manifest. Supports object, "
        "list, and scalar items, local directories, and project-file downloads."
    ),
    category=ActionCategory.CONVERT,
    run=export_column_tables,
    form="column_tables_export",
)

SHEET_CSV = action(
    examples=(
        ExportSheetCsvParams(
            sheet_id=1,
            destination=LocalFileDestination(
                kind="local_file", path="exports/data.csv"
            ),
        ),
    ),
    name="sheet_csv",
    title="Export sheet CSV",
    description=(
        "Render a visible sheet's current values into a CSV artifact, "
        "optionally scoped by a sheet.filter QuerySpec, write it to a "
        "local file, and record sheet/query refs plus artifact hash/path "
        "in a receipt."
    ),
    category=ActionCategory.CONVERT,
    run=export_sheet_csv,
    form="sheet_csv_export",
    export_target=ExportTarget(label="CSV", destination_kind="csv"),
)

SHEET_JSONL = action(
    examples=(
        ExportSheetJsonlParams(
            sheet_id=1,
            destination=LocalFileDestination(
                kind="local_file", path="exports/data.jsonl"
            ),
        ),
    ),
    name="sheet_jsonl",
    title="JSONL",
    description=(
        "Render a visible sheet's current values into a JSONL artifact (one "
        "typed JSON object per row, media columns flattened to reference "
        "fields), optionally scoped by a sheet.filter QuerySpec, and record "
        "sheet/query refs plus artifact hash/path in a receipt."
    ),
    category=ActionCategory.CONVERT,
    run=export_sheet_jsonl,
    form="sheet_jsonl_export",
    export_target=ExportTarget(label="JSONL", destination_kind="jsonl"),
)

SHEET_PARQUET = action(
    examples=(
        ExportSheetParquetParams(
            sheet_id=1,
            destination=LocalFileDestination(
                kind="local_file", path="exports/data.parquet"
            ),
        ),
    ),
    name="sheet_parquet",
    title="Parquet",
    description=(
        "Render a visible sheet's current values into a Parquet artifact "
        "with stable schema metadata (typed columns, media columns flattened "
        "to reference fields), optionally scoped by a sheet.filter QuerySpec, "
        "and record sheet/query refs plus artifact hash/path in a receipt."
    ),
    category=ActionCategory.CONVERT,
    run=export_sheet_parquet,
    form="sheet_parquet_export",
    export_target=ExportTarget(label="Parquet", destination_kind="parquet"),
)


def prepare_google_sheets(
    params: GoogleSheetsExportRequest,
) -> GoogleSheetsExportRequest:
    return params


GOOGLE_SHEETS = action(
    name="google_sheets",
    title="Export to Google Sheets",
    description="Export a sheet, view, or all visible sheets through a connected Google account. External writes cannot be undone.",
    category=ActionCategory.CONVERT,
    run=google_sheets_export(prepare_google_sheets),
    form="google_sheets_export",
    export_target=ExportTarget(
        label="Google Sheets",
        destination_kind="google_sheets",
        source_modes=("current_sheet", "current_view", "all_sheets"),
        destination_modes=("new_spreadsheet", "update_existing"),
        connection_provider="google",
        connection_scopes=(
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive.file",
        ),
    ),
    examples=(
        GoogleSheetsExportRequest(
            connection_id="conn_google_1",
            source={"kind": "current_sheet", "sheet_id": 1},
            destination={
                "kind": "google_sheets",
                "mode": "new_spreadsheet",
                "spreadsheet_title": "Frisket export",
            },
        ),
    ),
)
