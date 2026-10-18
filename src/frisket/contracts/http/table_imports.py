"""Typed HTTP contract for one-source table import drafts."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, StringConstraints

from frisket.contracts.http.models import WireModel


TABLE_IMPORT_SCHEMA_VERSION = "frisket.table_import.v1"
TABLE_IMPORT_LARGE_PASTE_WARNING_BYTES = 20 * 1024 * 1024

TableImportFormat = Literal["delimited", "xlsx", "xls", "ods"]
TableImportTargetType = Literal[
    "text", "integer", "number", "date", "datetime", "boolean", "omit"
]
TableImportDateOrder = Literal["month_first", "day_first"]


class TableImportSheetInfo(WireModel):
    index: int = Field(ge=0)
    name: str
    hidden: bool
    empty: bool


class TableImportSource(WireModel):
    input_kind: Literal["file", "paste"]
    filename: str
    size: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    format: TableImportFormat | None
    sheets: list[TableImportSheetInfo]
    can_rerun_workbook: bool


class TableImportRequestedSettings(WireModel):
    delimiter: Annotated[str, StringConstraints(min_length=1, max_length=1)] | None
    encoding: str | None = Field(default=None, min_length=1, max_length=64)
    decimal_separator: Literal["auto", ".", ","] = "auto"
    thousands_separator: Literal["auto", ",", ".", " ", "none"] = "auto"
    worksheet: str | None = Field(default=None, max_length=512)
    skip_rows_start: int = Field(default=0, ge=0)
    first_row_is_header: bool = True
    skip_rows_end: int = Field(default=0, ge=0)


class TableImportResolvedSettings(WireModel):
    kind: TableImportFormat
    delimiter: str | None = Field(default=None, min_length=1, max_length=1)
    encoding: str | None = Field(default=None, min_length=1, max_length=64)
    decimal_separator: Literal[".", ","]
    thousands_separator: str | None = Field(default=None, min_length=1, max_length=1)
    worksheet: str | None = Field(default=None, max_length=512)
    skip_rows_start: int = Field(ge=0)
    first_row_is_header: bool
    skip_rows_end: int = Field(ge=0)


class TableImportSettings(WireModel):
    revision: int = Field(ge=0)
    requested: TableImportRequestedSettings
    resolved: TableImportResolvedSettings


class TableImportIssueCounts(WireModel):
    extra_values: int = Field(default=0, ge=0)
    missing_values: int = Field(default=0, ge=0)
    malformed_records: int = Field(default=0, ge=0)
    unreadable_cells: int = Field(default=0, ge=0)


class TableImportAnalysis(WireModel):
    state: Literal["idle", "running", "complete", "error"]
    job_id: int | None = Field(default=None, gt=0)
    scanned_rows: int = Field(ge=0)
    estimated_rows: int | None = Field(default=None, ge=0)
    exact_rows: int | None = Field(default=None, ge=0)
    complete: bool
    issue_counts: TableImportIssueCounts
    error: str | None


class TableImportSourceColumn(WireModel):
    index: int = Field(ge=0)
    name: str
    original_name: str | None
    name_status: Literal["original", "duplicate", "missing"]


class TableImportRowIssue(WireModel):
    code: Literal[
        "extra_values", "missing_values", "malformed_record", "unreadable_cell"
    ]
    message: str
    original_values: list[str]


class TableImportPreviewRow(WireModel):
    role: Literal["skipped_start", "header", "data", "blank", "skipped_end"]
    record_ordinal: int = Field(ge=1)
    physical_line_start: int | None = Field(default=None, ge=1)
    physical_line_end: int | None = Field(default=None, ge=1)
    cells: list[str]
    hidden_cell_count: int = Field(ge=0)
    issues: list[TableImportRowIssue]


class TableImportPreviewNotice(WireModel):
    kind: Literal[
        "extra_values",
        "unreadable_characters",
        "one_column",
        "mostly_wrong_width",
        "some_wrong_width",
        "consistent",
        "counting",
    ]
    count: int | None = Field(default=None, ge=0)
    common_values: int | None = Field(default=None, ge=0)
    expected_values: int | None = Field(default=None, ge=0)
    encoding: str | None


class TableImportPreview(WireModel):
    columns: list[TableImportSourceColumn]
    head: list[TableImportPreviewRow]
    tail: list[TableImportPreviewRow]
    rows_seen: int = Field(ge=0)
    omitted_rows: int | None = Field(default=None, ge=0)
    complete: bool
    tail_pending: bool
    expected_values: int = Field(ge=0)
    notice: TableImportPreviewNotice


class TableImportConversionSample(WireModel):
    original: str
    value: str | int | float | bool | None
    display: str
    outcome: Literal["unchanged", "changed", "blank", "invalid"]


class TableImportConversionCounts(WireModel):
    rows_checked: int = Field(ge=0)
    valid: int = Field(ge=0)
    blank: int = Field(ge=0)
    changed: int = Field(ge=0)
    invalid: int = Field(ge=0)
    leading_zero_loss: int = Field(ge=0)
    time_dropped: int = Field(ge=0)


class TableImportColumn(WireModel):
    source_index: int = Field(ge=0)
    name: str
    original_name: str | None
    name_status: Literal["original", "duplicate", "missing"]
    included: bool
    target_type: TableImportTargetType
    date_order: TableImportDateOrder | None
    suggested_type: TableImportTargetType
    ambiguous_date_order: bool
    needs_review: list[
        Literal[
            "leading_zeros",
            "long_integer",
            "ambiguous_date_order",
            "invalid_values",
            "time_dropped",
        ]
    ]
    samples: list[TableImportConversionSample] = Field(max_length=5)
    conversion_counts: TableImportConversionCounts
    stats_complete: bool


class TableImportNewDestination(WireModel):
    kind: Literal["new"]
    sheet_name: str = Field(min_length=1, max_length=80)


class TableImportExistingDestination(WireModel):
    kind: Literal["existing"]
    sheet_id: int = Field(gt=0)


TableImportDestinationConfig = Annotated[
    TableImportNewDestination | TableImportExistingDestination,
    Field(discriminator="kind"),
]


class TableImportExistingColumnTarget(WireModel):
    kind: Literal["existing"]
    column_id: int = Field(gt=0)


class TableImportNewColumnTarget(WireModel):
    kind: Literal["new"]
    name: str = Field(min_length=1, max_length=512)


TableImportColumnTarget = Annotated[
    TableImportExistingColumnTarget | TableImportNewColumnTarget,
    Field(discriminator="kind"),
]


class TableImportMappingRequest(WireModel):
    source_index: int = Field(ge=0)
    target: TableImportColumnTarget


class TableImportMapping(TableImportMappingRequest):
    source_name: str
    source_type: TableImportTargetType
    match: Literal["matched", "similar", "new", "type_clash"]
    detail: str | None


class TableImportDedup(WireModel):
    enabled: bool
    key_source_index: int | None = Field(default=None, ge=0)


class TableImportDestinationComparison(WireModel):
    state: Literal["idle", "running", "ready", "error"]
    matched_columns: int = Field(ge=0)
    new_columns: int = Field(ge=0)
    type_clashes: int = Field(ge=0)
    duplicate_rows: int | None = Field(default=None, ge=0)
    import_rows: int | None = Field(default=None, ge=0)
    complete: bool
    error: str | None


class TableImportDestination(WireModel):
    configuration: TableImportDestinationConfig
    mappings: list[TableImportMapping]
    dedup: TableImportDedup
    comparison: TableImportDestinationComparison


class TableImportCommitResponse(WireModel):
    schema_version: Literal[TABLE_IMPORT_SCHEMA_VERSION]
    draft_id: str
    status: Literal["accepted"]
    import_ref: str
    sheet_id: int = Field(gt=0)
    sheet_name: str
    selected_total: int | None = Field(default=None, ge=0)
    estimated_rows: int | None = Field(default=None, ge=0)


class TableImportDraft(WireModel):
    schema_version: Literal[TABLE_IMPORT_SCHEMA_VERSION]
    id: str
    draft_id: str
    state: Literal[
        "password_required", "analyzing", "ready", "committing", "committed", "error"
    ]
    revision: int = Field(ge=1)
    created_at: str
    updated_at: str
    source: TableImportSource
    settings: TableImportSettings | None
    analysis: TableImportAnalysis
    preview: TableImportPreview | None
    columns: list[TableImportColumn]
    destination: TableImportDestination | None
    can_commit: bool
    commit_blocked_reason: (
        Literal["password_required", "analyzing", "empty", "destination", "error"]
        | None
    )
    commit: TableImportCommitResponse | None


class TableImportUnlockRequest(WireModel):
    password: str = Field(min_length=1, max_length=4096)


class TableImportSettingsRequest(WireModel):
    expected_revision: int = Field(ge=1)
    settings: TableImportRequestedSettings


class TableImportColumnChoice(WireModel):
    source_index: int = Field(ge=0)
    included: bool
    target_type: TableImportTargetType
    date_order: TableImportDateOrder | None = None


class TableImportColumnsRequest(WireModel):
    expected_revision: int = Field(ge=1)
    columns: list[TableImportColumnChoice]


class TableImportDestinationRequest(WireModel):
    expected_revision: int = Field(ge=1)
    destination: TableImportDestinationConfig
    mappings: list[TableImportMappingRequest]
    dedup: TableImportDedup


class TableImportCommitRequest(WireModel):
    expected_revision: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=256)


class TableImportRerunRequest(WireModel):
    worksheet: str = Field(min_length=1, max_length=512)


__all__ = [
    name
    for name in globals()
    if name.startswith("TableImport") or name.startswith("TABLE_IMPORT_")
]
