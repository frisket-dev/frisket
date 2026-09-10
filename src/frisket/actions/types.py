from __future__ import annotations

import datetime as dt
import types
import typing
from collections.abc import AsyncIterable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import (
    Any,
    Annotated,
    BinaryIO,
    ContextManager,
    Generic,
    Literal,
    Protocol,
    TextIO,
    TypeVar,
)

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    StrictInt,
    StrictStr,
    field_validator,
    model_serializer,
    model_validator,
)
from pydantic_core import core_schema

from frisket.authoring.templates import column_template_names, render_column_template
from frisket.contracts.actions.schemas._base import MAX_IMPORT_ROWS_COLUMNS
from frisket.ai.model_defaults import DEFAULT_MAX_OUTPUT_TOKENS
from frisket.actions.grounding_types import EvidenceClaim


T = TypeVar("T")
QUERY_PREVIEW_SCHEMA_VERSION = "frisket.query_preview.v1"


class InvocationContext(Protocol):
    def check_cancelled(self) -> None:
        """Raise asyncio.CancelledError when the host has cancelled this invocation."""
        ...


class PluginSecrets(Protocol):
    """Project-configured secrets declared by an installed plugin package."""

    def require(self, name: str) -> str:
        """Return one declared secret or raise when it is unavailable."""
        ...


class Link(str):
    """A string materialized as a clickable link column."""

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type: Any, handler: Any) -> Any:
        return core_schema.no_info_after_validator_function(
            cls, core_schema.str_schema()
        )


_COLUMN_TYPE_BY_VALUE = {
    str: "text",
    Link: "link",
    int: "integer",
    float: "number",
    bool: "boolean",
    dt.date: "date",
    dt.datetime: "date",
}


def _without_none(annotation: Any) -> tuple[Any, ...]:
    origin = typing.get_origin(annotation)
    if origin in (typing.Union, types.UnionType):
        return tuple(
            item for item in typing.get_args(annotation) if item is not type(None)
        )
    return (annotation,)


def _semantic_annotation(annotation: Any) -> Any:
    """Unwrap metadata and single-member optional semantic annotations."""
    while True:
        if typing.get_origin(annotation) is Annotated:
            annotation = typing.get_args(annotation)[0]
            continue
        optional = _without_none(annotation)
        if len(optional) == 1 and optional[0] is not annotation:
            annotation = optional[0]
            continue
        return annotation


def source_kind(
    annotation: Any,
) -> (
    Literal[
        "column", "columns", "template", "column_or_template", "columns_or_template"
    ]
    | None
):
    """Classify supported row-source shapes independently of their presentation."""
    annotation = _semantic_annotation(annotation)
    if isinstance(annotation, type):
        if issubclass(annotation, ColumnRef):
            return "column"
        if issubclass(annotation, Template):
            return "template"
    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)
    if (
        origin in (list, tuple)
        and len(args) == 1
        and isinstance(args[0], type)
        and issubclass(args[0], ColumnRef)
    ):
        return "columns"
    if origin in (typing.Union, types.UnionType):
        kinds = {source_kind(item) for item in args}
        if kinds == {"columns", "template"}:
            return "columns_or_template"
        if kinds == {"column", "template"}:
            return "column_or_template"
    return None


class ActionParams(BaseModel):
    """Closed base model for an action's author-owned parameters."""

    model_config = ConfigDict(extra="forbid", defer_build=True)


class SheetRef(ActionParams):
    """A referenced sheet, not a second row-selection scope."""

    sheet_id: int = Field(gt=0, strict=True)


class SheetColumnRef(SheetRef):
    """A column on a referenced sheet, resolved by its admitted capability."""

    column: StrictStr = Field(min_length=1)

    @field_validator("column")
    @classmethod
    def trimmed_column(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("column must be non-empty and trimmed")
        return value


class RefreshedSheet(BaseModel):
    """Descriptive refresh result; committed identities remain host facts."""

    sheet_id: int
    refreshed_row_count: int


class SheetRefresher(Protocol):
    def refresh(self, sheet_id: int) -> RefreshedSheet: ...


class BackfilledRun(BaseModel):
    """Descriptive backfill result; generation and receipt identity stay host-owned."""

    filled: int


@dataclass(frozen=True, eq=False)
class PreparedBackfill:
    """Invocation-owned preparation; only its issuing host can execute it."""

    selected_row_count: int


class RunBackfiller(Protocol):
    def prepare(self, column: ColumnRef[Any]) -> PreparedBackfill: ...


class SourceCreateIntent(ActionParams):
    name: str = Field(min_length=1)
    kind: str = Field(default="url", min_length=1)
    url: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    sheet_id: int | None = Field(default=None, gt=0, strict=True)
    schedule: str | None = None
    enabled: bool = Field(default=True, strict=True)

    @field_validator("name", "kind")
    @classmethod
    def _required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("value must be non-empty")
        return value

    @field_validator("url", "schedule")
    @classmethod
    def _optional_text(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None


SourcePollSelector = Annotated[int, Field(gt=0, strict=True)] | SourceCreateIntent


class PolledSource(BaseModel):
    """Descriptive result of a fetch; publication facts remain host-owned."""

    source_id: int
    status: Literal["ok", "error"]
    item_count: int
    cost_micro: int
    error: str | None = None


class SourcePollOutput(BaseModel):
    source_id: int
    source_run_id: int
    sheet_id: int | None = None
    new_rows: int
    revisions: int
    materialized_rows: int
    row_ids: list[int] = Field(default_factory=list)
    enclosure_row_ids: list[int] = Field(default_factory=list)
    op_ids: list[int] = Field(default_factory=list)
    receipt_id: str | None = None
    skipped_rows: int = 0
    changed_rows: int = 0
    warning_count: int = 0
    cursor_before: str | None = None
    cursor_after: str | None = None


class SourcePoller(Protocol):
    def poll(self, source: SourcePollSelector) -> PolledSource: ...


class RowError(Exception):
    """An expected row failure whose diagnostic remains visible and retryable."""

    def __init__(self, code: str, message: str) -> None:
        if not isinstance(code, str) or not code.strip() or len(code) > 64:
            raise ValueError("row error code must contain 1–64 characters")
        if not isinstance(message, str) or not message.strip() or len(message) > 500:
            raise ValueError("row error message must contain 1–500 characters")
        super().__init__(message)
        self.code = code
        self.message = message


class MediaMetadataReader(Protocol):
    async def read(
        self, cell: Any, *, refresh: bool = False
    ) -> dict[str, Any] | None: ...


class ColumnTablesLocalDirDestination(ActionParams):
    kind: Literal["local_dir"]
    path: str = Field(min_length=1)

    @field_validator("path")
    @classmethod
    def _nonblank_path(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("path must be non-empty")
        return value


class ColumnTablesProjectFileDestination(ActionParams):
    kind: Literal["project_file"]
    prefix: str = Field(min_length=1)

    @field_validator("prefix")
    @classmethod
    def _nonblank_prefix(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("prefix must be non-empty")
        return value


ColumnTablesDestination = Annotated[
    ColumnTablesLocalDirDestination | ColumnTablesProjectFileDestination,
    Field(discriminator="kind"),
]


class ColumnTablesExport(BaseModel):
    """Rendered ZIP and manifest facts, without host receipt coordinates."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["export_artifact", "export_project_file"]
    export_kind: Literal["column_tables"] = "column_tables"
    format: Literal["zip"] = "zip"
    content_type: Literal["application/zip"] = "application/zip"
    sheet_id: int
    column_id: int
    column_name: str
    shape: Literal["dict", "list", "scalar"]
    group_by: str | None = None
    destination_kind: Literal["local_dir", "project_file"]
    artifact_count: int
    entry_count: int
    byte_count: int
    sha256: str
    manifest: dict[str, Any]
    path: str | None = None
    project_path: str | None = None
    blob_hash: str | None = None


class ColumnTablesExporter(Protocol):
    def write_column_tables(
        self,
        *,
        sheet_id: int,
        column_id: int,
        destination: ColumnTablesDestination,
        group_by: str | None = None,
        exclude_columns: list[str] | None = None,
        name_template: str = "{row:03d}_{table:03d}.csv",
        first_row_header: bool = False,
    ) -> ColumnTablesExport:
        """Render a list-valued column into grouped CSVs in one ZIP package."""
        ...


class PluginManifestRecord(BaseModel):
    """Validated manifest facts; package evidence and receipt identity are host-owned."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    plugin_id: str
    version: str
    manifest_sha256: str
    byte_count: int
    contributes: dict[str, list[str]]


class PluginManifestLoader(Protocol):
    def load(self, path: str) -> PluginManifestRecord:
        """Validate a local package and record evidence, without activating it."""
        ...


EmbeddingModality = Literal["text", "image", "audio", "video", "file", "row", "auto"]


class CreatedEmbeddingIndex(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    index_id: str
    space_id: str
    modality: str
    provider_id: str
    provider_kind: str
    actual_model_id: str
    dimension: int


class RefreshedEmbeddingIndex(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    index_id: str
    space_id: str
    mode: str
    total_items: int
    refreshed: int
    skipped_current: int
    ready_items: int
    truncated_items: int
    backend_id: str
    backend_version: str


class IndexCreator(Protocol):
    def create(
        self,
        *,
        source_columns: list[str],
        sheet_id: int | None = None,
        modality: EmbeddingModality = "text",
        provider: str | None = None,
        model: str | None = None,
        source_query: dict[str, Any] | None = None,
        source_policy: dict[str, Any] | None = None,
        maintenance_policy: dict[str, Any] | None = None,
        provider_policy: dict[str, Any] | None = None,
        name: str | None = None,
    ) -> CreatedEmbeddingIndex:
        """Create index metadata, checkpointing any paid dimension-discovery probe."""
        ...


class IndexRefresher(Protocol):
    def refresh(
        self,
        index_id: str,
        *,
        mode: Literal["incremental", "full"] = "incremental",
        trigger_ref: dict[str, Any] | None = None,
        row_scope: dict[str, Any] | None = None,
    ) -> RefreshedEmbeddingIndex:
        """Refresh an admitted index under its lease and provider checkpoints."""
        ...


class DeletedEmbeddingIndex(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    index_id: str
    space_id: str
    deleted_items: int
    space_deleted: bool
    deleted_export_artifacts: int


class IndexDeleter(Protocol):
    def delete(self, index_id: str) -> DeletedEmbeddingIndex:
        """Delete the index definition, cached vectors, and its unshared space.

        Source data and other indexes are preserved.
        Vector deletion and best-effort export cleanup cannot be rolled back with
        the project metadata transaction.
        """
        ...


class EmbeddingIndexPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    index_id: str
    space_id: str
    maintenance_policy: dict[str, Any]
    provider_policy: dict[str, Any]


class EmbeddingIndexPolicyUpdater(Protocol):
    def update_policy(
        self,
        index_id: str,
        *,
        maintenance_policy: dict[str, Any] | None = None,
        provider_policy: dict[str, Any] | None = None,
    ) -> EmbeddingIndexPolicy:
        """Atomically replace provided policies; omitted policies remain unchanged."""
        ...


IndexExportFormat = Literal["jsonl", "parquet", "arrow"]


class IndexExportArtifact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    format: IndexExportFormat
    path: str
    byte_count: int
    sha256: str
    row_count: int


class IndexExport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    index_id: str
    space_id: str
    total_items: int
    ready_items: int
    exported_rows: int
    include_vectors: bool
    artifacts: tuple[IndexExportArtifact, ...]


class IndexExporter(Protocol):
    def export(
        self,
        index_id: str,
        *,
        path: str,
        formats: Iterable[IndexExportFormat] = ("jsonl",),
        include_vectors: bool = True,
    ) -> IndexExport:
        """Export a fresh index's originals and optional vectors to local files."""
        ...


class GeoPoint(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    lat: float = Field(ge=-90, le=90, allow_inf_nan=False)
    lon: float = Field(ge=-180, le=180, allow_inf_nan=False)


_COLUMN_TYPE_BY_VALUE[GeoPoint] = "geo_point"


@dataclass(frozen=True)
class InputReference:
    column: str
    accepted_column_types: tuple[str, ...] | None = None
    ai_generated_only: bool = False


@dataclass(frozen=True)
class Row:
    values: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", MappingProxyType(dict(self.values)))

    def read(self, column: str) -> Any:
        if column not in self.values:
            raise KeyError(f"row does not contain admitted column {column!r}")
        return self.values[column]


@dataclass(frozen=True)
class Rows(Mapping[int, Row]):
    """Immutable row-id keyed input for a whole-scope ``map_batch`` handler."""

    values: Mapping[int, Row]

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", MappingProxyType(dict(self.values)))

    def __getitem__(self, row_id: int) -> Row:
        return self.values[row_id]

    def __iter__(self) -> Iterator[int]:
        return iter(self.values)

    def __len__(self) -> int:
        return len(self.values)


@dataclass(frozen=True)
class ColumnTransformContext:
    """The one live source fact admitted to an atomic column transform."""

    source_type: str


@dataclass(frozen=True)
class SourceRecord:
    source_id: int
    name: str
    kind: str
    url: str | None
    config: Mapping[str, Any]
    sheet_id: int | None
    schedule: str | None
    enabled: bool
    last_checked_at: str | None
    last_status: str | None
    new_rows_total: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "config", MappingProxyType(dict(self.config)))


@dataclass(frozen=True)
class UpdatedSource:
    source: SourceRecord
    updated_fields: tuple[str, ...]


@dataclass(frozen=True)
class DeletedSource:
    source: SourceRecord
    source_run_count: int


@dataclass(frozen=True)
class CheckedSource:
    source: SourceRecord
    source_run_id: int
    status: Literal["ok", "error"]
    new_rows: int
    error: str | None


@dataclass(frozen=True)
class PatchedColumn:
    sheet_id: int
    sheet_name: str
    column_id: int
    column_name: str
    column_type: str
    format_before: str | None
    format_after: str | None
    op_id: int


@dataclass(frozen=True)
class CreatedColumn:
    sheet_id: int
    sheet_name: str
    column_id: int
    name: str
    column_type: str
    position: int
    op_id: int


@dataclass(frozen=True)
class CreatedRow:
    sheet_id: int
    row_id: int
    total: int
    op_id: int
    cells: Mapping[str, Any]
    column_ids: Mapping[str, int]

    def __post_init__(self) -> None:
        object.__setattr__(self, "cells", MappingProxyType(dict(self.cells)))
        object.__setattr__(self, "column_ids", MappingProxyType(dict(self.column_ids)))


@dataclass(frozen=True)
class AppendedRows:
    sheet_id: int
    row_ids: tuple[int, ...]
    total: int
    op_id: int
    column_ids: Mapping[str, int]
    source: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "column_ids", MappingProxyType(dict(self.column_ids)))
        object.__setattr__(self, "source", MappingProxyType(dict(self.source)))


@dataclass(frozen=True)
class UpdatedImportedRows:
    sheet_id: int
    row_ids: tuple[int, ...]
    edit_count: int
    op_id: int
    column_ids: Mapping[str, int]
    source: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "column_ids", MappingProxyType(dict(self.column_ids)))
        object.__setattr__(self, "source", MappingProxyType(dict(self.source)))


@dataclass(frozen=True)
class DeletedRows:
    sheet_id: int
    row_ids: tuple[int, ...]
    total: int
    op_id: int


@dataclass(frozen=True)
class RetypedColumn:
    sheet_id: int
    sheet_name: str
    column_id: int
    column_name: str
    type_before: str
    type_after: str
    op_id: int


@dataclass(frozen=True)
class EditedCells:
    op_id: int
    targets: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class QueryEditedCells:
    sheet_id: int
    column_id: int
    column_name: str
    query_hash: str
    row_ids: tuple[int, ...]
    op_id: int


@dataclass(frozen=True)
class ReviewDecision:
    run_id: int
    row_id: int
    column_id: int
    decision: Literal["accept", "reject", "reject_clear", "edit"]
    review_state_before: Literal["unreviewed", "verified", "rejected"]
    review_state_after: Literal["verified", "rejected"]
    note: str | None
    op_id: int


@dataclass(frozen=True)
class AcceptedReplayValue:
    sheet_id: int
    row_id: int
    column_id: int
    run_id: int
    generated_value_hash: str
    accepted: bool
    pending_count: int
    op_id: int | None


@dataclass(frozen=True)
class AcceptedReplayColumn:
    sheet_id: int
    column_id: int
    accepted: int
    pending_count: int
    op_id: int | None


@dataclass(frozen=True)
class DismissedReplayValue:
    sheet_id: int
    row_id: int
    column_id: int
    run_id: int
    generated_value_hash: str
    pending_count: int


@dataclass(frozen=True)
class OperationTransition:
    op_id: int
    operation_kind: str
    direction: Literal["undo", "redo"]
    status_before: Literal["applied", "undone"]
    status_after: Literal["applied", "undone"]
    cursor_before: int
    cursor_after: int
    affected_refs: tuple[Mapping[str, Any], ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "affected_refs",
            tuple(MappingProxyType(dict(ref)) for ref in self.affected_refs),
        )


class QueryPreviewScore(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    distance: float | None = None
    score: float | None = None


class QueryPreviewEvaluator(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal[
        "frisket.querysets.sheet_filter",
        "embedding_similarity",
        "embedding_hybrid",
    ]
    version: Literal[
        "v1",
        "frisket.embedding_similarity.v1",
        "frisket.embedding_hybrid.v1",
    ]


class QueryPreviewResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["frisket.query_preview.v1"] = QUERY_PREVIEW_SCHEMA_VERSION
    sheet_id: int
    query: dict[str, Any]
    query_hash: str
    row_ids: list[int] = Field(default_factory=list)
    row_count: int
    total: int
    limit: int
    offset: int
    evaluator: QueryPreviewEvaluator
    scores: dict[int, QueryPreviewScore] = Field(default_factory=dict)


@dataclass(frozen=True)
class ExportedWorkLog:
    __pydantic_config__ = ConfigDict(extra="forbid")
    format: Literal["markdown"]
    path: str
    byte_count: int
    sha256: str


@dataclass(frozen=True)
class ExportedSheetFile:
    sheet_id: int
    format: Literal["csv", "jsonl", "parquet"]
    path: str
    byte_count: int
    sha256: str
    row_count: int
    row_ids: tuple[int, ...]
    query_hash: str | None


@dataclass(frozen=True)
class ExportedCsvSheetFile(ExportedSheetFile):
    """The CSV primitive's closed domain result, without host receipt metadata."""

    __pydantic_config__ = ConfigDict(extra="forbid")
    format: Literal["csv"]


@dataclass(frozen=True)
class ExportedJsonlSheetFile(ExportedSheetFile):
    __pydantic_config__ = ConfigDict(extra="forbid")
    format: Literal["jsonl"]


@dataclass(frozen=True)
class ExportedParquetSheetFile(ExportedSheetFile):
    __pydantic_config__ = ConfigDict(extra="forbid")
    format: Literal["parquet"]


class ColumnPatchActionOutput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    sheet_id: int
    column_id: int
    format_before: str | None = None
    format_after: str | None = None
    op_ids: list[int] = Field(default_factory=list)
    receipt_id: str | None = None


class ColumnAddActionOutput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    sheet_id: int
    column_id: int
    name: str
    type: str
    position: int
    op_ids: list[int] = Field(default_factory=list)
    receipt_id: str | None = None


class RowAddActionOutput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    sheet_id: int
    row_id: int
    total: int
    op_ids: list[int] = Field(default_factory=list)
    receipt_id: str | None = None


class RowsAppendActionOutput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    sheet_id: int
    row_ids: list[int] = Field(default_factory=list)
    row_count: int
    total: int
    op_ids: list[int] = Field(default_factory=list)
    receipt_id: str | None = None


class RowDeleteActionOutput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    sheet_id: int
    row_ids: list[int] = Field(default_factory=list)
    deleted: int
    total: int
    op_ids: list[int] = Field(default_factory=list)
    receipt_id: str | None = None


class ColumnSetTypeActionOutput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    sheet_id: int
    column_id: int
    type_before: str
    type_after: str
    op_ids: list[int] = Field(default_factory=list)
    receipt_id: str | None = None


class CellTargetActionOutput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["cell"] = "cell"
    row_id: int
    column_id: int


class CellEditActionOutput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    edit_count: int
    targets: list[CellTargetActionOutput]
    op_ids: list[int] = Field(default_factory=list)
    receipt_id: str | None = None


class CellEditQueryActionOutput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    sheet_id: int
    column_id: int
    query_hash: str
    row_ids: list[int] = Field(default_factory=list)
    edit_count: int
    op_ids: list[int] = Field(default_factory=list)
    receipt_id: str | None = None


class ReviewDecisionActionOutput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: int
    row_id: int
    column_id: int
    decision: Literal["accept", "reject", "reject_clear", "edit"]
    review_state_before: Literal["unreviewed", "verified", "rejected"]
    review_state_after: Literal["verified", "rejected"]
    note: str | None
    op_ids: list[int] = Field(default_factory=list)
    receipt_id: str | None = None


class ReplayAcceptActionOutput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    sheet_id: int
    row_id: int
    column_id: int
    run_id: int
    generated_value_hash: str
    accepted: bool
    pending_count: int
    op_ids: list[int] = Field(default_factory=list)
    receipt_id: str | None = None


class ReplayAcceptColumnActionOutput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    sheet_id: int
    column_id: int
    accepted: int
    pending_count: int
    op_ids: list[int] = Field(default_factory=list)
    receipt_id: str | None = None


class ReplayDismissActionOutput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    sheet_id: int
    row_id: int
    column_id: int
    run_id: int
    generated_value_hash: str
    pending_count: int
    receipt_id: str | None = None


class OperationStepActionOutput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    op_id: int
    operation_kind: str
    direction: Literal["undo", "redo"]
    status_before: Literal["applied", "undone"]
    status_after: Literal["applied", "undone"]
    cursor_before: int
    cursor_after: int
    affected_refs: list[dict[str, Any]] = Field(default_factory=list)
    receipt_id: str | None = None


class SourceActionOutput(BaseModel):
    """Host projection of a source record into the public action result."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_id: int
    name: str
    source_kind: str
    url: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    sheet_id: int | None = None
    schedule: str | None = None
    enabled: bool
    last_checked_at: str | None = None
    last_status: str | None = None
    new_rows_total: int = 0
    receipt_id: str | None = None


class SourceUpdateActionOutput(SourceActionOutput):
    updated_fields: list[str] = Field(default_factory=list)


class SourceDeleteActionOutput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source_id: int
    name: str
    source_kind: str
    sheet_id: int | None = None
    deleted: bool = True
    source_run_count: int = 0
    receipt_id: str | None = None


class SourceCheckActionOutput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source_id: int
    source_run_id: int
    status: Literal["ok", "error"]
    new_rows: int
    receipt_id: str | None = None


class ExportWorkLogOutput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    format: Literal["markdown"]
    path: str
    byte_count: int
    sha256: str
    receipt_id: str | None = None


class _ExportSheetOutput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    sheet_id: int
    format: str
    path: str
    byte_count: int
    sha256: str
    row_count: int
    row_ids: list[int] = Field(default_factory=list)
    query_hash: str | None = None
    receipt_id: str | None = None


class ExportSheetCsvOutput(_ExportSheetOutput):
    format: Literal["csv"]


class ExportSheetJsonlOutput(_ExportSheetOutput):
    format: Literal["jsonl"]


class ExportSheetParquetOutput(_ExportSheetOutput):
    format: Literal["parquet"]


class SourcePatch(ActionParams):
    """The exact explicitly supplied source fields for one update."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(default=None, min_length=1)  # type: ignore[assignment]
    kind: str = Field(default=None, min_length=1)  # type: ignore[assignment]
    url: str | None = None
    config: dict[str, Any] | None = None
    sheet_id: int | None = Field(default=None, gt=0, strict=True)
    schedule: str | None = None
    enabled: bool = Field(default=None, strict=True)  # type: ignore[assignment]

    @field_validator("name", "kind")
    @classmethod
    def _required_text_when_present(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("field must be non-empty")
        return value

    @field_validator("url", "schedule")
    @classmethod
    def _trim_optional_text(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None

    @model_validator(mode="after")
    def _nonempty_patch(self) -> SourcePatch:
        if not self.model_fields_set:
            raise ValueError("patch must contain at least one field")
        return self

    def values(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in sorted(self.model_fields_set)}

    @model_serializer
    def _serialize_explicit_fields(self) -> dict[str, Any]:
        return self.values()


class SourceCreator(Protocol):
    def create(
        self,
        *,
        name: str,
        kind: str,
        url: str | None,
        config: dict[str, Any],
        sheet_id: int | None,
        schedule: str | None,
        enabled: bool,
    ) -> SourceRecord: ...


class SourceUpdater(Protocol):
    def update(self, source_id: int, *, patch: SourcePatch) -> UpdatedSource: ...


class SourceDeleter(Protocol):
    def delete(self, source_id: int) -> DeletedSource: ...


class SourceChecker(Protocol):
    def check(
        self,
        source_id: int,
        *,
        new_rows: int,
        status: Literal["ok", "error"],
        error: str | None,
        cursor: str | None,
    ) -> CheckedSource: ...


class ColumnPatcher(Protocol):
    def patch(
        self,
        column_id: int,
        *,
        sheet_id: int | None,
        format: str | None,
    ) -> PatchedColumn: ...


class ColumnCreator(Protocol):
    def create(
        self,
        sheet_id: int,
        *,
        name: str,
        column_type: str,
        position: int | None,
    ) -> CreatedColumn: ...


class RowCreator(Protocol):
    def create(self, sheet_id: int, *, cells: Mapping[str, Any]) -> CreatedRow: ...


class RowsAppender(Protocol):
    def append(
        self,
        *,
        columns: tuple[Any, ...],
        rows: tuple[Mapping[str, Any], ...],
        source: Mapping[str, Any],
    ) -> Any: ...

    def append_csv(self, params: Any) -> Any: ...

    def append_xlsx(self, params: Any) -> Any: ...


class RowsUpdater(Protocol):
    def update(
        self,
        *,
        columns: tuple[Any, ...],
        key_columns: tuple[str, ...],
        rows: tuple[Mapping[str, Any], ...],
        source: Mapping[str, Any],
        keep_existing_on_blank: bool,
    ) -> UpdatedImportedRows: ...

    def update_csv(self, params: Any) -> UpdatedImportedRows: ...

    def update_xlsx(self, params: Any) -> UpdatedImportedRows: ...


class RowDeleter(Protocol):
    def delete(self, sheet_id: int, *, row_ids: tuple[int, ...]) -> DeletedRows: ...


class ColumnTyper(Protocol):
    def set_type(
        self, column_id: int, *, sheet_id: int | None, column_type: str
    ) -> RetypedColumn: ...


class CellEditor(Protocol):
    def edit(self, edits: tuple[tuple[int, int, Any], ...]) -> EditedCells: ...


class QueryCellEditor(Protocol):
    def edit_query(
        self, *, query: Mapping[str, Any], column_id: int, value: Any
    ) -> QueryEditedCells: ...


class ReviewDecider(Protocol):
    def decide(
        self,
        *,
        run_id: int,
        row_id: int,
        column_id: int,
        decision: Literal["accept", "reject", "reject_clear", "edit"],
        value: Any,
        value_supplied: bool,
        note: str | None,
    ) -> ReviewDecision: ...


class ReplayValueAcceptor(Protocol):
    def accept(
        self,
        *,
        sheet_id: int,
        row_id: int,
        column_id: int,
        run_id: int,
        generated_value_hash: str,
    ) -> AcceptedReplayValue: ...


class ReplayColumnAcceptor(Protocol):
    def accept_column(
        self, *, sheet_id: int, column_id: int
    ) -> AcceptedReplayColumn: ...


class ReplayValueDismissor(Protocol):
    def dismiss(
        self,
        *,
        sheet_id: int,
        row_id: int,
        column_id: int,
        run_id: int,
        generated_value_hash: str,
    ) -> DismissedReplayValue: ...


class OperationUndoer(Protocol):
    def undo(self, *, expected_op_id: int | None) -> OperationTransition: ...


class OperationRedoer(Protocol):
    def redo(self, *, expected_op_id: int | None) -> OperationTransition: ...


class QueryPreviewer(Protocol):
    def preview(
        self, *, query: Mapping[str, Any], limit: int, offset: int
    ) -> QueryPreviewResult: ...


class WorkLogExporter(Protocol):
    def write_work_log(
        self,
        *,
        path: str,
        include_receipts: bool,
    ) -> ExportedWorkLog: ...


class SheetCsvExporter(Protocol):
    def write_sheet_csv(
        self,
        *,
        sheet_id: int,
        path: str,
        query: Mapping[str, Any] | None,
        formula_policy: Literal["escape", "raw"],
    ) -> ExportedCsvSheetFile: ...


class SheetJsonlExporter(Protocol):
    def write_sheet_jsonl(
        self,
        *,
        sheet_id: int,
        path: str,
        query: Mapping[str, Any] | None,
    ) -> ExportedJsonlSheetFile: ...


class SheetParquetExporter(Protocol):
    def write_sheet_parquet(
        self,
        *,
        sheet_id: int,
        path: str,
        query: Mapping[str, Any] | None,
    ) -> ExportedParquetSheetFile: ...


class _SemanticText(RootModel[str], Generic[T]):
    model_config = ConfigDict(frozen=True)

    @field_validator("root")
    @classmethod
    def _nonblank(cls, value: str) -> str:
        if not value or value != value.strip():
            raise ValueError("value must be non-empty and trimmed")
        return value


class TextLike(str):
    """Text or category values; templates may render scalar address parts."""


def _declared_ref_types(annotation: type) -> tuple[str, ...] | None:
    explicit = annotation.__dict__.get("accepted_column_types")
    if isinstance(explicit, tuple) and all(isinstance(item, str) for item in explicit):
        return explicit
    return None


def _exclude_ref_types(
    annotation: type, accepted: tuple[str, ...] | None
) -> tuple[str, ...] | None:
    excluded = annotation.__dict__.get("excluded_column_types", ())
    if not excluded:
        return accepted
    if accepted is None:
        from frisket.authoring.column_types import type_names

        available = type_names()
    else:
        available = accepted
    return tuple(sorted(set(available) - set(excluded)))


def template_ref_types(annotation: type[Template[Any]]) -> tuple[str, ...] | None:
    accepted = _declared_ref_types(annotation)
    if accepted is None:
        args = getattr(annotation, "__pydantic_generic_metadata__", {}).get("args", ())
        if args == (TextLike,):
            accepted = ("category", "date", "integer", "link", "number", "text")
    return _exclude_ref_types(annotation, accepted)


def column_ref_types(annotation: type[ColumnRef[Any]]) -> tuple[str, ...] | None:
    accepted = _declared_ref_types(annotation)
    if accepted is None:
        args = getattr(annotation, "__pydantic_generic_metadata__", {}).get("args", ())
        value_type = args[0] if len(args) == 1 else Any
        if value_type is Any:
            return _exclude_ref_types(annotation, None)
        if value_type is TextLike:
            return _exclude_ref_types(annotation, ("text", "category"))
        members = _without_none(value_type)

        def column_type(item: Any) -> str:
            origin = typing.get_origin(item)
            if origin in (dict, list):
                return "json"
            return _COLUMN_TYPE_BY_VALUE[item]

        try:
            accepted = tuple(dict.fromkeys(column_type(item) for item in members))
        except KeyError as error:
            raise TypeError(
                f"unsupported ColumnRef value type {value_type!r}"
            ) from error
    return _exclude_ref_types(annotation, accepted)


class ColumnRef(_SemanticText[T], Generic[T]):
    @property
    def name(self) -> str:
        return self.root

    def references(self) -> tuple[InputReference, ...]:
        return (InputReference(self.root, column_ref_types(type(self))),)

    def read(self, row: Row) -> T:
        return row.read(self.root)


class GeneratedColumnRef(ColumnRef[T], Generic[T]):
    """A column whose selected values must come from one generated run."""

    def references(self) -> tuple[InputReference, ...]:
        return (
            InputReference(
                self.root,
                column_ref_types(type(self)),
                ai_generated_only=True,
            ),
        )


class Template(BaseModel, Generic[T]):
    """A text template, distinct on the wire from a column name."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str

    @field_validator("text")
    @classmethod
    def _nonblank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("template must be non-empty")
        return value

    def references(self) -> tuple[InputReference, ...]:
        return tuple(
            InputReference(name, template_ref_types(type(self)))
            for name in column_template_names(self.text)
        )

    def render(self, row: Row) -> str:
        return render_column_template(self.text, dict(row.values))


class EngineRef(_SemanticText[T], Generic[T]):
    """Saved engine selection, associated with its injected capability by type."""


class ModelRef(_SemanticText[str]):
    """A model-router identifier selected by the action author."""

    @field_validator("root")
    @classmethod
    def _provider_and_model(cls, value: str) -> str:
        if "/" not in value:
            raise ValueError("model must use provider/model form")
        if value.startswith("ollama/"):
            from frisket.local_model_ids import parse_local_model_id

            try:
                parse_local_model_id(value)
            except ValueError as error:
                raise ValueError("invalid Ollama model id") from error
        return value


@dataclass(frozen=True)
class ModelPrompt(Generic[T]):
    """One pure model request; ``T`` owns the response schema."""

    messages: tuple[Mapping[str, Any], ...]
    response_schema: Mapping[str, Any] | None = None
    max_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS

    def __post_init__(self) -> None:
        if not self.messages:
            raise ValueError("model prompt must contain at least one message")
        object.__setattr__(
            self, "messages", tuple(dict(message) for message in self.messages)
        )
        if self.response_schema is not None:
            import copy

            object.__setattr__(
                self, "response_schema", copy.deepcopy(dict(self.response_schema))
            )

            def validate_refs(value: Any) -> None:
                if isinstance(value, Mapping):
                    for key, child in value.items():
                        if key in {"$ref", "$dynamicRef"} and (
                            not isinstance(child, str) or not child.startswith("#")
                        ):
                            raise ValueError(
                                "model response schema references must be local"
                            )
                        validate_refs(child)
                elif isinstance(value, list):
                    for child in value:
                        validate_refs(child)

            validate_refs(self.response_schema)
        if (
            isinstance(self.max_tokens, bool)
            or not isinstance(self.max_tokens, int)
            or self.max_tokens < 1
        ):
            raise ValueError("model prompt max_tokens must be a positive integer")


def discover_references(value: Any) -> tuple[InputReference, ...]:
    from frisket.actions.http_types import HttpRequest

    found: dict[str, InputReference] = {}

    def visit(item: Any) -> None:
        if isinstance(item, (ColumnRef, Template, HttpRequest)):
            for reference in item.references():
                current = found.get(reference.column)
                if current is None:
                    found[reference.column] = reference
                elif current.accepted_column_types is None:
                    found[reference.column] = InputReference(
                        reference.column,
                        reference.accepted_column_types,
                        current.ai_generated_only or reference.ai_generated_only,
                    )
                elif reference.accepted_column_types is not None:
                    accepted = tuple(
                        item
                        for item in current.accepted_column_types
                        if item in reference.accepted_column_types
                    )
                    found[reference.column] = InputReference(
                        reference.column,
                        accepted,
                        current.ai_generated_only or reference.ai_generated_only,
                    )
                elif reference.ai_generated_only and not current.ai_generated_only:
                    found[reference.column] = InputReference(
                        current.column,
                        current.accepted_column_types,
                        ai_generated_only=True,
                    )
        elif isinstance(item, BaseModel):
            for child in item.__class__.model_fields:
                visit(getattr(item, child))
        elif isinstance(item, Mapping):
            for child in item.values():
                visit(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                visit(child)

    visit(value)
    return tuple(found.values())


class Outcome(BaseModel, Generic[T]):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["ok", "failed"]
    value: T | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    justification: str | None = None
    evidence: tuple[EvidenceClaim, ...] = ()
    warnings: tuple[str, ...] = ()
    code: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,63}$")
    message: str | None = Field(default=None, min_length=1, max_length=500)

    @model_validator(mode="after")
    def _coherent(self) -> Outcome[T]:
        if self.status == "ok" and (self.code is not None or self.message is not None):
            raise ValueError("successful outcome cannot contain an error")
        if self.status == "failed" and (
            self.value is not None
            or self.confidence is not None
            or self.justification is not None
            or self.evidence
            or self.warnings
            or not self.code
            or not self.message
        ):
            raise ValueError("failed outcome must contain only a code and message")
        return self

    @classmethod
    def ok(
        cls,
        value: T,
        *,
        confidence: float | None = None,
        justification: str | None = None,
        evidence: tuple[EvidenceClaim, ...] = (),
        warnings: tuple[str, ...] = (),
    ) -> Outcome[T]:
        return cls(
            status="ok",
            value=value,
            confidence=confidence,
            justification=justification,
            evidence=evidence,
            warnings=warnings,
        )

    @classmethod
    def failed(cls, code: str, message: str) -> Outcome[T]:
        return cls(status="failed", code=code, message=message)


class RowResult(BaseModel, Generic[T]):
    model_config = ConfigDict(frozen=True, extra="forbid")

    output: T


@dataclass(frozen=True)
class TableColumn:
    """One logical column emitted by a ``create_sheet`` action."""

    key: str
    type: str
    format: str | None = None
    hidden: bool = False

    def __post_init__(self) -> None:
        if not self.key or self.key != self.key.strip():
            raise ValueError("table column key must be non-empty and trimmed")
        if not self.type or self.type != self.type.strip():
            raise ValueError("table column type must be non-empty and trimmed")


@dataclass(frozen=True, eq=False)
class RowSource:
    """A source row admitted by this invocation's host capability."""

    sheet_id: int
    row_id: int


@dataclass(frozen=True)
class SheetRowInput:
    """One request-scoped row and its host-issued lineage token."""

    row: Row
    source: RowSource


class SheetRowsReader(Protocol):
    def read(self) -> Iterable[SheetRowInput]:
        """Read the request-scoped rows and declared ordinary columns once."""
        ...


class ListColumnSource(ActionParams):
    """A live JSON list column, optionally projected onto ordered object keys."""

    kind: Literal["column"]
    sheet_id: int = Field(gt=0, strict=True)
    column_id: int = Field(gt=0, strict=True)
    include_columns: list[StrictStr] | None = Field(
        default=None, max_length=MAX_IMPORT_ROWS_COLUMNS
    )

    @field_validator("include_columns")
    @classmethod
    def _included_columns(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        names = [name.strip() for name in value]
        if not names or any(not name for name in names):
            raise ValueError("invalid_params")
        if len(names) != len(set(names)):
            raise ValueError("duplicate_column_name")
        return names


class NamedListSource(ActionParams):
    """A completed named result whose receipt permits list materialization."""

    model_config = ConfigDict(serialize_by_alias=True)

    kind: Literal["named_result"]
    sheet_id: int = Field(gt=0, strict=True)
    column_id: int = Field(gt=0, strict=True)
    run_id: int = Field(gt=0, strict=True)
    route: str = Field(min_length=1)
    schema_name: str = Field(alias="schema", min_length=1)


ListTableSource = typing.Annotated[
    ListColumnSource | NamedListSource, Field(discriminator="kind")
]


@dataclass(frozen=True)
class ListItem:
    """One admitted list value and its opaque, item-specific source token."""

    value: Any
    source: RowSource


class ListTableReader(Protocol):
    def read(
        self,
        source: ListTableSource,
        *,
        item_schema: dict[str, Any] | None = None,
    ) -> tuple[ListItem, ...]:
        """Admit and read list items; the host retains item evidence privately."""
        ...


@dataclass(frozen=True)
class CollectionItem:
    """External collection metadata with its admitted source-cell row."""

    value: dict[str, Any]
    source: RowSource


class CollectionReader(Protocol):
    def read(
        self, *, sheet_id: int, column_id: int, row_id: int
    ) -> tuple[CollectionItem, ...]:
        """Enumerate a URL cell's collection; the host owns consent and evidence."""
        ...


@dataclass(frozen=True)
class EmbeddingVector:
    source_key: str
    vector: tuple[float, ...]
    source: RowSource


class EmbeddingIndexReader(Protocol):
    def read(
        self, index_id: str, *, minimum_rows: int = 1
    ) -> tuple[EmbeddingVector, ...]: ...


class StagedFile:
    """An opaque file cell; only its issuing invocation can admit it for storage."""

    __slots__ = ("size",)

    def __init__(self, size: int) -> None:
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ValueError("staged file size must be a nonnegative integer")
        object.__setattr__(self, "size", size)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("StagedFile is immutable")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("StagedFile is immutable")

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type: Any, handler: Any) -> Any:
        # Never reconstruct an authoritative handle from a dict or serialized value.
        return core_schema.is_instance_schema(cls)

    @classmethod
    def __get_pydantic_json_schema__(cls, schema: Any, handler: Any) -> dict[str, Any]:
        # Catalogs describe the host-lowered cell, not the in-memory authority.
        return {
            "type": "object",
            "properties": {
                "blob": {"type": "string"},
                "mime": {"type": "string"},
                "filename": {"type": "string"},
            },
            "required": ["blob", "mime", "filename"],
            "additionalProperties": False,
        }


class StagedImage(StagedFile):
    """An invocation-owned image, lowered only by its admitted file publisher."""

    __slots__ = ()


class StagedAudio(StagedFile):
    """Invocation-owned audio, published through the admitted file writer."""

    __slots__ = ()


class StagedVideo(StagedFile):
    """Invocation-owned video, published through the admitted file writer."""

    __slots__ = ()


@dataclass(frozen=True, slots=True)
class PdfDocument:
    """Stage an original PDF that must remain reachable without a file cell."""


@dataclass(frozen=True, slots=True)
class PdfPage:
    """Associate staged page bytes with an admitted original PDF and page number."""

    document: StagedFile
    page: int

    def __post_init__(self) -> None:
        if not isinstance(self.document, StagedFile):
            raise ValueError("PDF page document must be a staged file")
        if (
            not isinstance(self.page, int)
            or isinstance(self.page, bool)
            or self.page < 1
        ):
            raise ValueError("PDF page must be a positive integer")


class ImportBlobStager(Protocol):
    def stage(
        self,
        stream: BinaryIO,
        *,
        filename: str,
        mime: str,
        role: PdfDocument | PdfPage | None = None,
    ) -> StagedFile:
        """Copy a stream to invocation-owned staging; None is an ordinary attachment."""
        ...

    def open_binary(self, file: StagedFile) -> ContextManager[BinaryIO]:
        """Borrow a read-only seekable stream for a file admitted by this invocation."""
        ...


class PdfPageRenderer(Protocol):
    def render(self, document: StagedFile, *, dpi: int) -> Mapping[int, StagedFile]:
        """Render admitted PDF pages to staged images within the host's read budget."""
        ...


class EmailSourceRef(ActionParams):
    source_ref: str = Field(min_length=1)
    logical_path: str = Field(min_length=1)
    format: Literal["eml", "mbox"]


@dataclass(frozen=True, slots=True)
class EmailInput:
    """An admitted email stream borrowed from its ingress owner."""

    logical_path: str
    format: Literal["eml", "mbox"]
    stream: BinaryIO = field(repr=False, compare=False)


class EmailSourceReader(Protocol):
    def open(
        self, sources: Sequence[EmailSourceRef]
    ) -> ContextManager[tuple[EmailInput, ...]]:
        """Admit the exact trusted source set without opening client-authored paths."""
        ...


class LocalFileReader(Protocol):
    def open_binary(self, path: str) -> ContextManager[BinaryIO]:
        """Borrow a seekable, read-only source for archive parsing.

        Successful context exit hashes the same source in bounded reads and
        verifies admitted bytes before recording facts. Failure or abandonment
        closes resources without hashing. Local descriptor checks detect ordinary
        edits, but do not guarantee an atomic snapshot during concurrent writes.
        """
        ...

    def open_text(
        self, path: str, *, encoding: str = "utf-8", newline: str | None = ""
    ) -> ContextManager[TextIO]:
        """Stream decoded text to EOF; the host records and verifies raw bytes.

        Normal completion requires a full read. Early failure closes resources
        without draining the source; prefix/preview reads are not this capability.
        """
        ...

    def read_bytes(self, path: str) -> bytes:
        """Read a regular local file; the host records the exact observed bytes."""
        ...


class TableError(ValueError):
    """An actionable producer refusal, before any table is published.

    Producer-specific codes may supplement the catalog's host error examples.
    """

    def __init__(
        self, code: str, message: str, *, details: dict[str, Any] | None = None
    ):
        super().__init__(message)
        self.code = code
        self.details = details


@dataclass(frozen=True)
class TableRow(Generic[T]):
    output: T
    sources: tuple[RowSource, ...] = ()
    parent: RowSource | None = None


@dataclass(frozen=True)
class TableResult(Generic[T]):
    """Rows produced for one host-materialized sheet, consumed once by the host.

    Materialized sequences (such as lists or tuples) publish in one transaction.
    Other source-free iterables stream in bounded, hidden batches before atomic
    publication. Parented rows use the host's buffered lineage writer.
    """

    rows: Iterable[TableRow[T]] | AsyncIterable[TableRow[T]]
    source: Mapping[str, Any] | None = None
    warnings: Iterable[str] = ()

    def __init__(
        self,
        *,
        rows: Iterable[TableRow[T]] | AsyncIterable[TableRow[T]],
        source: Mapping[str, Any] | None = None,
        warnings: Iterable[str] = (),
    ) -> None:
        object.__setattr__(self, "rows", rows)
        object.__setattr__(self, "warnings", warnings)
        object.__setattr__(
            self,
            "source",
            MappingProxyType(dict(source)) if source is not None else None,
        )


def _table_warnings(produced):
    """Freeze bounded final diagnostics after the producer's rows have closed."""
    supplied = getattr(produced, "warnings", ())
    if isinstance(supplied, (str, bytes)):
        raise TypeError("table warnings must be an iterable of strings")
    iterator = iter(supplied)
    warnings = []
    try:
        for index, warning in enumerate(iterator):
            if index == 100:
                warnings.append("Additional warnings omitted")
                break
            if not isinstance(warning, str):
                raise TypeError("table warnings must contain strings")
            warnings.append(warning if len(warning) <= 2000 else warning[:1999] + "…")
    finally:
        if callable(close := getattr(iterator, "close", None)):
            close()
    return tuple(warnings)


class DynamicOutput(RootModel[dict[str, Any]]):
    """A row result validated against the host's frozen logical fields."""

    model_config = ConfigDict(frozen=True)


@dataclass(frozen=True)
class DynamicTableResult:
    """An inspected table schema, frozen before the host consumes any rows."""

    schema: tuple[TableColumn, ...]
    rows: Iterable[TableRow[DynamicOutput]] | AsyncIterable[TableRow[DynamicOutput]]
    source: Mapping[str, Any] | None = None
    warnings: Iterable[str] = ()

    def __init__(
        self,
        *,
        schema: Iterable[TableColumn],
        rows: Iterable[TableRow[DynamicOutput]]
        | AsyncIterable[TableRow[DynamicOutput]],
        source: Mapping[str, Any] | None = None,
        warnings: Iterable[str] = (),
    ) -> None:
        object.__setattr__(self, "schema", tuple(schema))
        object.__setattr__(self, "warnings", warnings)
        object.__setattr__(self, "rows", rows)
        object.__setattr__(
            self,
            "source",
            MappingProxyType(dict(source)) if source is not None else None,
        )


class RuntimeImportSource(ActionParams):
    """Importer-supplied source description, not verified file evidence."""

    kind: Literal["runtime"]
    label: StrictStr | None = None
    fingerprint: StrictStr | None = None


class RuntimeImporter(Protocol):
    def read(
        self,
        importer_kind: str,
        *,
        source: RuntimeImportSource,
        handler_params: Mapping[str, Any],
    ) -> DynamicTableResult:
        """Read through this project's trusted importer; publication is separate."""
        ...


class ProjectScope(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    kind: Literal["project"] = "project"


class SheetRows(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    kind: Literal["sheet_rows"] = "sheet_rows"
    sheet_id: int = Field(gt=0, strict=True)
    row_ids: tuple[StrictInt, ...] | None = None

    @field_validator("row_ids")
    @classmethod
    def _valid_rows(cls, value: tuple[int, ...] | None) -> tuple[int, ...] | None:
        if value is None:
            return None
        if not value:
            raise ValueError("row ids must not be empty")
        if any(row_id <= 0 for row_id in value) or len(value) != len(set(value)):
            raise ValueError("row ids must be positive and unique")
        return tuple(sorted(value))


class ActionRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    action_id: str = Field(min_length=1)
    scope: ProjectScope | SheetRows = Field(discriminator="kind")
    params: dict[str, Any]
    output_names: dict[str, str] = Field(default_factory=dict)
    replace_existing: bool = Field(default=False, strict=True)
    sheet_name: str | None = None
    idempotency_key: str = Field(min_length=1)
    confirmation: str | None = Field(default=None, min_length=1)

    @field_validator("output_names")
    @classmethod
    def _valid_output_names(cls, value: dict[str, str]) -> dict[str, str]:
        if any(
            not key or not name or key != key.strip() or name != name.strip()
            for key, name in value.items()
        ):
            raise ValueError("output names must be non-empty and trimmed")
        if len(value.values()) != len(set(value.values())):
            raise ValueError("final output names must be unique")
        return value

    @field_validator("sheet_name")
    @classmethod
    def _valid_sheet_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("sheet_name must be non-empty")
        return value
