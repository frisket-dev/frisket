"""Typed producers for structured rows and declared-schema local files."""

from __future__ import annotations

import csv
import io
import json
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from frisket.actions.core import ActionCategory, RowScope, action, create_sheet
from frisket.actions.types import (
    ActionParams,
    DynamicOutput,
    LocalFileReader,
    TableColumn,
    TableError,
    TableResult,
    TableRow,
    RowsAppender,
    AppendedRows,
    RowsUpdater,
    UpdatedImportedRows,
)
from frisket.contracts.action import MAX_IMPORT_ROWS_COLUMNS
from frisket.contracts.actions.schemas.imports import (
    ImportRowsSource,
    ValidatedImportColumns,
    normalize_import_rows,
    normalize_import_value,
)
from frisket.csv_values import parse_csv_value


class ImportRowsParams(ActionParams):
    columns: ValidatedImportColumns = Field(
        min_length=1, max_length=MAX_IMPORT_ROWS_COLUMNS
    )
    rows: list[dict[str, Any]]
    source: ImportRowsSource | None = None

    @model_validator(mode="after")
    def _validate_rows(self) -> ImportRowsParams:
        self.rows = normalize_import_rows(self.columns, self.rows)
        return self


class UpdateRowsParams(ImportRowsParams):
    key_columns: list[str] = Field(min_length=1)
    keep_existing_on_blank: bool = False

    @model_validator(mode="after")
    def _validate_update_mapping(self) -> UpdateRowsParams:
        names = {column.name for column in self.columns}
        if len(self.key_columns) != len(set(self.key_columns)):
            raise ValueError("key_columns must be unique")
        if not set(self.key_columns).issubset(names):
            raise ValueError("key_columns must name imported columns")
        if names == set(self.key_columns):
            raise ValueError("choose at least one non-key update column")
        return self


def append_rows(params: ImportRowsParams, appender: RowsAppender) -> AppendedRows:
    return appender.append(
        columns=tuple(params.columns),
        rows=tuple(params.rows),
        source=params.source.model_dump(mode="json") if params.source else {},
    )


def update_rows(params: UpdateRowsParams, updater: RowsUpdater) -> UpdatedImportedRows:
    return updater.update(
        columns=tuple(params.columns),
        key_columns=tuple(params.key_columns),
        rows=tuple(params.rows),
        source=params.source.model_dump(mode="json") if params.source else {},
        keep_existing_on_blank=params.keep_existing_on_blank,
    )


def append_csv(params: ImportCsvParams, appender: RowsAppender) -> AppendedRows:
    return appender.append_csv(params)


class FileSource(ActionParams):
    kind: Literal["file"]
    path: str = Field(min_length=1)
    label: str | None = None


class ImportNdjsonParams(ActionParams):
    source: FileSource
    columns: ValidatedImportColumns = Field(
        min_length=1, max_length=MAX_IMPORT_ROWS_COLUMNS
    )
    encoding: str = Field(default="utf-8", min_length=1)


class CsvSource(ActionParams):
    path: str = Field(min_length=1)
    label: str | None = None
    encoding: str = Field(default="utf-8", min_length=1)
    delimiter: str = Field(default=",", min_length=1, max_length=1)
    decimal_separator: Literal[".", ","] = "."
    headers: list[str] | None = Field(default=None, min_length=1)

    @field_validator("headers")
    @classmethod
    def _unique_headers(cls, value: list[str] | None) -> list[str] | None:
        if value is not None and (
            any(not name for name in value) or len(value) != len(set(value))
        ):
            raise ValueError("CSV headers must be nonempty and unique")
        return value


class ImportCsvParams(ActionParams):
    sources: list[CsvSource] = Field(min_length=1)
    columns: ValidatedImportColumns = Field(
        min_length=1, max_length=MAX_IMPORT_ROWS_COLUMNS
    )
    source_label_column: str | None = None

    @model_validator(mode="after")
    def _source_columns(self) -> ImportCsvParams:
        columns = {column.name: column for column in self.columns}
        if self.source_label_column is not None:
            column = columns.get(self.source_label_column)
            if column is None or column.type != "text":
                raise ValueError("source_label_column must name a declared text column")
        data_columns = [
            column for column in self.columns if column.name != self.source_label_column
        ]
        source_names = {column.source_name or column.name for column in data_columns}
        if not source_names:
            raise ValueError("CSV requires at least one data column")
        for source in self.sources:
            if source.headers is not None and not source_names.issubset(source.headers):
                raise ValueError("CSV source headers must match declared data columns")
        return self


class UpdateCsvParams(ImportCsvParams):
    key_columns: list[str] = Field(min_length=1)
    keep_existing_on_blank: bool = False

    @model_validator(mode="after")
    def _validate_update_mapping(self) -> UpdateCsvParams:
        names = {column.name for column in self.columns}
        if len(self.key_columns) != len(set(self.key_columns)) or not set(
            self.key_columns
        ).issubset(names):
            raise ValueError("key_columns must uniquely name imported columns")
        if names == set(self.key_columns):
            raise ValueError("choose at least one non-key update column")
        return self


def _columns(
    params: ImportRowsParams | ImportNdjsonParams | ImportCsvParams,
) -> tuple[TableColumn, ...]:
    return tuple(
        TableColumn(
            key=column.name,
            type=column.type,
            format=column.format,
            hidden=column.hidden,
        )
        for column in params.columns
    )


def import_rows(params: ImportRowsParams) -> TableResult[DynamicOutput]:
    return TableResult(
        rows=[TableRow(output=DynamicOutput(row)) for row in params.rows],
        source=(
            params.source.model_dump(mode="json") if params.source is not None else None
        ),
    )


ROWS = action(
    examples=(
        ImportRowsParams(
            columns=[
                {"name": "title", "type": "text"},
                {"name": "count", "type": "integer"},
            ],
            rows=[{"title": "Example document", "count": 3}],
        ),
    ),
    name="rows",
    title="Import rows",
    description="Normalize already-structured rows into one new typed sheet.",
    category=ActionCategory.CONVERT,
    run=create_sheet(import_rows, columns_from=_columns),
)


APPEND_ROWS = action(
    examples=(
        ImportRowsParams(
            columns=[{"name": "title", "type": "text"}],
            rows=[{"title": "Example document"}],
        ),
    ),
    name="append_rows",
    title="Append imported rows",
    description="Append normalized structured rows to one explicitly scoped sheet.",
    category=ActionCategory.CONVERT,
    run=append_rows,
    row_scope=RowScope.ALL_ROWS,
)


UPDATE_ROWS = action(
    examples=(
        UpdateRowsParams(
            columns=[
                {"name": "id", "type": "text"},
                {"name": "title", "type": "text"},
            ],
            key_columns=["id"],
            rows=[{"id": "example-1", "title": "Updated title"}],
        ),
    ),
    name="update_rows",
    title="Update imported rows",
    description="Update selected cells on exact-key matches in one explicitly scoped sheet.",
    category=ActionCategory.CONVERT,
    run=update_rows,
    row_scope=RowScope.ALL_ROWS,
)


APPEND_CSV = action(
    examples=(
        ImportCsvParams(
            sources=[{"path": "examples/data.csv", "headers": ["title"]}],
            columns=[{"name": "title", "type": "text"}],
        ),
    ),
    name="append_csv",
    title="Append CSV rows",
    description="Stream one admitted CSV mapping into an explicitly scoped sheet.",
    category=ActionCategory.CONVERT,
    run=append_csv,
    row_scope=RowScope.ALL_ROWS,
)


def update_csv(params: UpdateCsvParams, updater: RowsUpdater) -> UpdatedImportedRows:
    return updater.update_csv(params)


UPDATE_CSV = action(
    examples=(
        UpdateCsvParams(
            sources=[{"path": "examples/data.csv", "headers": ["id", "title"]}],
            columns=[{"name": "id", "type": "text"}, {"name": "title", "type": "text"}],
            key_columns=["id"],
        ),
    ),
    name="update_csv",
    title="Update rows from CSV",
    description="Stream CSV rows into exact-key updates on an explicitly scoped sheet.",
    category=ActionCategory.CONVERT,
    run=update_csv,
    row_scope=RowScope.ALL_ROWS,
)


def import_ndjson(
    params: ImportNdjsonParams, files: LocalFileReader
) -> TableResult[DynamicOutput]:
    data = files.read_bytes(params.source.path)
    expected = {column.name for column in params.columns}
    rows = []
    try:
        with io.TextIOWrapper(
            io.BytesIO(data), encoding=params.encoding, newline=None
        ) as stream:
            for line_number, line in enumerate(stream, start=1):
                if not line.strip():
                    continue
                try:
                    parsed = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise TableError(
                        "ndjson_parse_failed",
                        "NDJSON line could not be parsed",
                        details={"line": line_number, "error": str(exc)},
                    ) from exc
                if not isinstance(parsed, dict):
                    raise TableError(
                        "ndjson_parse_failed",
                        "NDJSON lines must be JSON objects",
                        details={"line": line_number},
                    )
                if set(parsed) != expected:
                    raise TableError(
                        "row_shape_mismatch",
                        "NDJSON row keys must match declared column names",
                        details={
                            "line": line_number,
                            "missing": sorted(expected - set(parsed)),
                            "extra": sorted(set(parsed) - expected),
                        },
                    )
                row = {}
                for column in params.columns:
                    try:
                        row[column.name] = normalize_import_value(
                            column.type, parsed[column.name]
                        )
                    except ValueError as exc:
                        raise TableError(
                            "invalid_ndjson_value",
                            "NDJSON value does not match the declared column type",
                            details={
                                "line": line_number,
                                "column": column.name,
                                "type": column.type,
                            },
                        ) from exc
                rows.append(TableRow(output=DynamicOutput(row)))
    except (UnicodeError, LookupError) as exc:
        raise TableError(
            "ndjson_parse_failed", "NDJSON file could not be decoded"
        ) from exc
    return TableResult(
        rows=rows,
        source={"kind": "file", "label": params.source.label, "importer": "ndjson"},
    )


NDJSON = action(
    examples=(
        ImportNdjsonParams(
            source=FileSource(kind="file", path="examples/data.ndjson"),
            columns=[{"name": "title", "type": "text"}],
        ),
    ),
    name="ndjson",
    title="Import NDJSON",
    description="Import a local NDJSON file using its declared column schema.",
    category=ActionCategory.CONVERT,
    run=create_sheet(import_ndjson, columns_from=_columns),
)


def import_csv(
    params: ImportCsvParams, files: LocalFileReader
) -> TableResult[DynamicOutput]:
    def rows():
        columns = [
            column
            for column in params.columns
            if column.name != params.source_label_column
        ]
        source_names = [column.source_name or column.name for column in columns]
        for source in params.sources:
            expected = source.headers if source.headers is not None else source_names
            try:
                with files.open_text(
                    source.path, encoding=source.encoding, newline=""
                ) as stream:
                    reader = csv.reader(stream, delimiter=source.delimiter, strict=True)
                    header = next(reader, None)
                    # Upload scanners expose trimmed logical column names; direct
                    # declarations without scanned headers retain exact matching.
                    if header is not None and source.headers is not None:
                        header = [name.strip() for name in header]
                    if header != expected:
                        raise TableError(
                            "csv_header_mismatch",
                            "CSV header must match declared data columns",
                            details={"expected": expected, "actual": header or []},
                        )
                    positions = [header.index(name) for name in source_names]
                    for row_index, values in enumerate(reader, start=2):
                        if len(values) != len(header):
                            raise TableError(
                                "csv_parse_failed",
                                "CSV row length does not match the header",
                                details={
                                    "line": row_index,
                                    "expected_columns": len(header),
                                    "actual_columns": len(values),
                                },
                            )
                        row = {}
                        for column, position in zip(columns, positions, strict=True):
                            try:
                                row[column.name] = parse_csv_value(
                                    values[position],
                                    column.type,
                                    decimal_separator=source.decimal_separator,
                                )
                            except ValueError as exc:
                                raise TableError(
                                    "invalid_csv_value",
                                    "CSV value could not be coerced to declared column type",
                                    details={
                                        "line": row_index,
                                        "column": column.name,
                                        "type": column.type,
                                        "value": values[position],
                                    },
                                ) from exc
                        if params.source_label_column is not None:
                            row[params.source_label_column] = (
                                source.label
                                if source.label is not None
                                else source.path
                            )
                        yield TableRow(output=DynamicOutput(row))
            except (OSError, UnicodeError, LookupError, csv.Error) as exc:
                raise TableError(
                    "csv_parse_failed",
                    "CSV file could not be parsed",
                    details={"error": str(exc)},
                ) from exc

    return TableResult(
        rows=rows(),
        source={
            "kind": "file",
            "importer": "csv",
            "label": params.sources[0].label if len(params.sources) == 1 else None,
        },
    )


CSV = action(
    examples=(
        ImportCsvParams(
            sources=[CsvSource(path="examples/data.csv")],
            columns=[
                {"name": "title", "type": "text"},
                {"name": "count", "type": "integer"},
            ],
        ),
    ),
    name="csv",
    title="Import CSV",
    description="Import local CSV files using their declared column schema.",
    category=ActionCategory.CONVERT,
    run=create_sheet(import_csv, columns_from=_columns),
)
