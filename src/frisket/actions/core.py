from __future__ import annotations

import inspect
import json
import re
import types
import typing
from dataclasses import dataclass, field, replace
from collections.abc import Mapping as MappingABC
from enum import Enum, StrEnum
from types import MappingProxyType
from typing import Any, Callable, Generic, Mapping, TypeVar, get_type_hints

from pydantic import BaseModel, JsonValue, TypeAdapter
from pydantic.fields import FieldInfo
from pydantic_core import SchemaSerializer, SchemaValidator
from frisket.actions.url_import_types import UrlImporter
from frisket.actions.entity_package_types import (
    FollowTheMoneyImporter,
    FollowTheMoneyExporter,
    ImportedEntityDataset,
    ExportedEntityPackage,
)

from frisket.actions.google_sheets_types import (
    GoogleSheetsExportOutput,
    GoogleSheetsExportRequest,
)

from frisket.actions.geospatial_types import CensusDemographics, Geocoder
from frisket.actions.classify_types import Classifier
from frisket.actions.group_summary_types import (
    GROUP_SUMMARY_COLUMNS,
    GroupSummarizer,
    PreparedGroupSummary,
    GroupSummaryOutput,
)
from frisket.actions.find_types import FindScanner, PreparedFind, FindOutput
from frisket.actions.ner_types import NerExtractor
from frisket.actions.research_types import Researcher, WebSearcher
from frisket.actions.mcp_types import McpExtractor
from frisket.actions.translate_types import Translator, TranslationOptions
from frisket.actions.cluster_types import (
    ClusteredValues,
    PreparedClustering,
    ValueClusterer,
)
from frisket.actions.page_capture_types import (
    CapturedPages,
    PageCapturer,
    PreparedPageCapture,
)
from frisket.actions.document_types import DocumentConverter
from frisket.actions.enclosure_types import (
    EnclosureMaterializer,
    MaterializedEnclosures,
)
from frisket.actions.media_options import OcrOptions, TranscriptionOptions
from frisket.actions.media_types import (
    DetectedLanguage,
    OcrReader,
    OcrText,
    Transcriber,
    TranscriptText,
)
from frisket.execution.targets import (
    CAPABILITY_OCR,
    CAPABILITY_TO_MARKDOWN,
    CAPABILITY_TRANSCRIBE,
)
from frisket.actions.temporal_types import (
    TopicSectionsReader,
    VisualCutsReader,
    topic_sidecar_names,
)
from frisket.actions.python_types import (
    NamedResultTarget,
    PythonEvaluator,
    RoutedOutput,
)
from frisket.actions.http_types import HttpRequester
from frisket.actions.opencorporates_types import OpenCorporates
from frisket.actions.file_types import FileFetcher
from frisket.actions.media_download_types import MediaDownloader
from frisket.actions.screenshot_types import Screenshotter
from frisket.actions.row_media_types import FrameExtractor, FaceExtractor
from frisket.actions.semantic_match_types import SemanticMatchReader
from frisket.actions.semantic_join_types import SemanticJoinMatch, SemanticMatcher
from frisket.actions.join_types import JoinedTablesReader
from frisket.actions.temporal_types import TemporalMediaReader
from frisket.actions.transcript_types import (
    TranscriptReader,
    TranscriptSelection,
    TemporalSelectionColumn,
    validate_transcript_selection_scope,
)
from frisket.actions.entity_types import ClusterReceiptReader
from frisket.actions.pdf_table_types import PdfTablesReader
from frisket.actions.temporal_extract_types import (
    TemporalExtractor,
    TemporalExtractSelection,
    PreparedTemporalExtract,
    ExtractedRanges,
)
from frisket.actions.types import (
    InvocationContext,
    PluginSecrets,
    ActionParams,
    EngineRef,
    ActionRequest,
    AcceptedReplayColumn,
    AcceptedReplayValue,
    ColumnRef,
    ColumnTablesExport,
    ColumnTablesExporter,
    DynamicOutput,
    DynamicTableResult,
    EmbeddingIndexReader,
    EmailSourceReader,
    RuntimeImporter,
    ListTableReader,
    SheetRowsReader,
    CollectionReader,
    LocalFileReader,
    ImportBlobStager,
    PdfPageRenderer,
    StagedFile,
    StagedImage,
    StagedAudio,
    StagedVideo,
    DeletedSource,
    GeoPoint,
    GeneratedColumnRef,
    ColumnTransformContext,
    CheckedSource,
    ColumnAddActionOutput,
    ColumnCreator,
    ColumnPatchActionOutput,
    ColumnPatcher,
    ColumnSetTypeActionOutput,
    ColumnTyper,
    CreatedColumn,
    CreatedRow,
    CellEditActionOutput,
    CellEditQueryActionOutput,
    CellEditor,
    DismissedReplayValue,
    DeletedRows,
    EditedCells,
    ExportedCsvSheetFile,
    ExportedJsonlSheetFile,
    ExportedParquetSheetFile,
    ExportedWorkLog,
    ExportSheetCsvOutput,
    ExportSheetJsonlOutput,
    ExportSheetParquetOutput,
    ExportWorkLogOutput,
    ModelPrompt,
    MediaMetadataReader,
    ModelRef,
    OperationRedoer,
    OperationStepActionOutput,
    OperationTransition,
    OperationUndoer,
    Outcome,
    PatchedColumn,
    ProjectScope,
    Row,
    RowResult,
    RowError,
    RowAddActionOutput,
    RowCreator,
    RowsAppender,
    RowsUpdater,
    RowsAppendActionOutput,
    AppendedRows,
    UpdatedImportedRows,
    RowDeleteActionOutput,
    RowDeleter,
    SheetCsvExporter,
    SheetJsonlExporter,
    SheetParquetExporter,
    QueryCellEditor,
    QueryEditedCells,
    QueryPreviewResult,
    QueryPreviewer,
    ReplayAcceptActionOutput,
    ReplayAcceptColumnActionOutput,
    ReplayColumnAcceptor,
    ReplayDismissActionOutput,
    ReplayValueAcceptor,
    ReplayValueDismissor,
    ReviewDecider,
    ReviewDecision,
    ReviewDecisionActionOutput,
    RetypedColumn,
    Rows,
    SheetRows,
    TableColumn,
    TableResult,
    SourceCreator,
    SourceActionOutput,
    SourceCheckActionOutput,
    SourceChecker,
    SourcePoller,
    SheetRefresher,
    RunBackfiller,
    BackfilledRun,
    PreparedBackfill,
    RefreshedSheet,
    PolledSource,
    SourcePollOutput,
    SourceDeleteActionOutput,
    SourceDeleter,
    SourceRecord,
    SourceUpdateActionOutput,
    SourceUpdater,
    UpdatedSource,
    WorkLogExporter,
    Template,
    _COLUMN_TYPE_BY_VALUE,
    _without_none,
    _semantic_annotation,
    column_ref_types,
    source_kind,
    template_ref_types,
    discover_references,
)
from frisket.features.temporal_values import TimelinePointsValue, TimelineRangesValue
from frisket.actions.types import (
    EmbeddingIndexPolicy,
    DeletedEmbeddingIndex,
    IndexDeleter,
    IndexCreator,
    IndexRefresher,
    CreatedEmbeddingIndex,
    RefreshedEmbeddingIndex,
    PluginManifestLoader,
    PluginManifestRecord,
    EmbeddingIndexPolicyUpdater,
    IndexExport,
    IndexExporter,
)


P = TypeVar("P", bound=ActionParams)
OutputT = TypeVar("OutputT", bound=BaseModel)
_LOCAL_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
_REQUEST_FIELDS = frozenset(
    "action_id scope sheet_id row_ids output_name output_names replace_existing sheet_name "
    "idempotency_key confirmation preview".split()
)
_PROJECT_REQUEST_FIELDS = _REQUEST_FIELDS - {"sheet_id", "row_ids"}


@dataclass(frozen=True)
class ProjectCapabilitySpec:
    """Closed portable contract for one host-supplied project capability."""

    capability: type[Any]
    return_type: type[Any]
    output_model: type[BaseModel]
    errors: tuple[tuple[str, str], ...]
    side_effects: tuple[str, ...]
    required_capabilities: tuple[str, ...]
    writes_project: bool
    cost_kind: str = "none"
    requires_confirmation: bool = False
    callable_host: bool = False

    def __post_init__(self) -> None:
        if any(
            isinstance(value, (str, bytes))
            for value in (self.errors, self.side_effects, self.required_capabilities)
        ):
            raise ValueError("project capability metadata must contain sequences")
        try:
            errors = tuple(
                (error,) if isinstance(error, str) else tuple(error)
                for error in self.errors
            )
            effects = tuple(self.side_effects)
            capabilities = tuple(self.required_capabilities)
        except TypeError as error:
            raise ValueError("project capability metadata must be iterable") from error
        values = (
            *[value for pair in errors for value in pair],
            *effects,
            *capabilities,
        )
        if (
            not isinstance(self.capability, type)
            or not isinstance(self.return_type, type)
            or not isinstance(self.output_model, type)
            or not issubclass(self.output_model, BaseModel)
            or not errors
            or any(len(error) != 2 for error in errors)
            or any(
                not isinstance(value, str) or not value or value != value.strip()
                for value in values
            )
            or any(len(group) != len(set(group)) for group in (effects, capabilities))
            or len({code for code, _ in errors}) != len(errors)
            or not effects
            or not capabilities
            or type(self.writes_project) is not bool
            or self.cost_kind not in {"none", "external_metered", "model_metered"}
            or type(self.requires_confirmation) is not bool
            or type(self.callable_host) is not bool
            or self.callable_host
            and (self.cost_kind != "none" or self.requires_confirmation)
            or self.writes_project
            and "project:write" not in capabilities
        ):
            raise ValueError("invalid project capability metadata")
        for metadata_field, value in (
            ("errors", errors),
            ("side_effects", effects),
            ("required_capabilities", capabilities),
        ):
            object.__setattr__(self, metadata_field, value)


def _project_capability_registry(
    *specs: ProjectCapabilitySpec,
) -> Mapping[type[Any], ProjectCapabilitySpec]:
    registry = {spec.capability: spec for spec in specs}
    if len(registry) != len(specs):
        raise ValueError("project capabilities must be unique")
    return MappingProxyType(registry)


def _project_capability(
    capability: type[Any],
    return_type: type[Any],
    output_model: type[BaseModel],
    errors: tuple[tuple[str, str], ...],
    side_effects: tuple[str, ...],
    *,
    required_capabilities: tuple[str, ...] = ("project:write",),
    writes_project: bool = True,
    cost_kind: str = "none",
    requires_confirmation: bool = False,
    callable_host: bool = False,
) -> ProjectCapabilitySpec:
    return ProjectCapabilitySpec(
        capability,
        return_type,
        output_model,
        (*errors, *_CALLABLE_ERRORS) if callable_host else errors,
        (*side_effects, "write_receipt"),
        required_capabilities,
        writes_project,
        cost_kind,
        requires_confirmation,
        callable_host,
    )


_INVALID_PROJECT_REQUEST = (
    "invalid_action_request",
    "The typed action request is malformed.",
)
_PROJECT_IDEMPOTENCY_CONFLICT = (
    "idempotency_conflict",
    "The idempotency key was used for different work.",
)

# Host lifecycle facts, independent of the author's capability combinations.
_CALLABLE_ERRORS = (
    (
        "idempotency_in_progress",
        "The callable may have effected and cannot be repeated.",
    ),
    (
        "invalid_action_result",
        "The callable must return its declared finite JSON value.",
    ),
    ("action_failed", "The callable could not complete."),
    ("action_cancelled", "The callable was cancelled."),
)


def _project_errors(
    *errors: tuple[str, str], write_failure: str
) -> tuple[tuple[str, str], ...]:
    return (
        _INVALID_PROJECT_REQUEST,
        *errors,
        _PROJECT_IDEMPOTENCY_CONFLICT,
        ("project_write_failed", write_failure),
    )


_RUNNING_RESERVATION_ERRORS = (
    ("idempotency_in_progress", "The idempotent action is already running."),
    ("idempotency_stale_running", "A stale running reservation was cleared."),
)


_MAP_ROWS_ERRORS = (
    ("invalid_action_request", "The typed action request is malformed."),
    ("invalid_params", "The action parameters are invalid."),
    ("invalid_input_ref", "A source sheet, row, or column reference is invalid."),
    ("output_column_exists", "A requested output column already exists."),
    ("output_column_busy", "A requested output column is claimed by another run."),
    ("network_disabled", "The action attempted an unadmitted network request."),
    ("map_rows_failed", "The row program failed to produce usable output."),
    ("project_write_failed", "The project write or receipt update failed."),
    ("idempotency_conflict", "The idempotency key was used for different work."),
    *_RUNNING_RESERVATION_ERRORS,
    ("stale_replay", "Stored outputs no longer match the committed receipt."),
)
_MODEL_ROWS_ERRORS = (
    *(item for item in _MAP_ROWS_ERRORS if item[0] != "map_rows_failed"),
    ("empty_input_column", "Every selected source cell is empty."),
    (
        "column_not_ai_generated",
        "The answer to grade must be an AI-generated column.",
    ),
    ("missing_provider_key", "The selected model provider is not configured."),
    ("model_cost_requires_confirmation", "The model cost requires confirmation."),
    ("model_run_failed", "The model failed for every selected row."),
    ("stale_input", "Queued source values changed before execution."),
    ("provider_spend_cap_exceeded", "The provider spend cap is exhausted."),
    (
        "provider_spend_cap_unenforceable",
        "The provider spend cap cannot be enforced for an unknown price.",
    ),
)
_EXTERNAL_ROWS_ERRORS = (
    *(item for item in _MAP_ROWS_ERRORS if item[0] != "map_rows_failed"),
    ("stale_input", "The queued source or output target changed before execution."),
    (
        "external_cost_requires_confirmation",
        "External provider use requires confirmation.",
    ),
    ("external_rows_failed", "The external provider failed for every selected row."),
)
_COLUMN_TRANSFORM_ERRORS = (
    ("invalid_action_request", "The typed action request is malformed."),
    ("invalid_params", "The transform parameters or result are invalid."),
    ("invalid_input_ref", "The source sheet or column is invalid."),
    ("output_column_exists", "The requested output column already exists."),
    ("output_column_busy", "The requested output column is claimed by another run."),
    (
        "project_write_failed",
        "The atomic transform failed without changing the project.",
    ),
    ("idempotency_conflict", "The idempotency key was used for different work."),
    ("stale_replay", "The source snapshot or committed output is no longer current."),
)
_TEMPORAL_PROJECT_ERRORS = (
    ("invalid_temporal_value", "A temporal value is malformed."),
    ("timeline_not_found", "A temporal timeline is missing."),
    ("timeline_stale", "A temporal timeline anchor is stale."),
    ("timeline_duration_required", "A timeline duration is not finalized."),
    ("ambiguous_time_mapping", "A temporal mapping is ambiguous."),
    ("range_out_of_bounds", "A temporal value exceeds its timeline duration."),
)
_CREATE_SHEET_ERRORS = (
    ("invalid_input_ref", "The admitted table source is invalid or unavailable."),
    ("invalid_action_request", "The typed action request is malformed."),
    ("invalid_params", "The table parameters or produced rows are invalid."),
    ("invalid_column_type", "A declared column type is unavailable."),
    ("duplicate_column_name", "Created sheet columns must have unique names."),
    ("duplicate_sheet_name", "A sheet with the requested name already exists."),
    ("stale_replay", "The published table or lineage has changed."),
    ("row_shape_mismatch", "Every row must match the declared columns."),
    (
        "import_workload_limit_exceeded",
        "The import exceeds the deployment row limit.",
    ),
    *_TEMPORAL_PROJECT_ERRORS,
    ("idempotency_conflict", "The idempotency key was used for different work."),
    (
        "project_write_failed",
        "The sheet, rows, operation, or receipt could not be written.",
    ),
)
_EXPORT_REQUEST_ERRORS = (
    ("invalid_action_request", "The typed action request is malformed."),
    ("invalid_export_destination", "The export destination is invalid."),
)
_EXPORT_REPLAY_ERRORS = (
    ("idempotency_conflict", "The idempotency key was used for different work."),
    ("export_artifact_missing", "The replayed export artifact is missing."),
    ("export_artifact_mismatch", "The replayed export artifact has changed."),
    ("project_write_failed", "The export receipt or artifact could not be written."),
)
_LOCAL_EXPORT_ERRORS = (*_EXPORT_REQUEST_ERRORS, *_EXPORT_REPLAY_ERRORS)
_SHEET_EXPORT_INPUT_ERRORS = (
    ("invalid_sheet_ref", "The sheet id must identify a visible sheet."),
    ("invalid_query_spec", "The optional query spec is invalid."),
    ("invalid_query_filter", "The optional query filter could not be evaluated."),
    ("export_rowset_too_large", "The export row set exceeds the hosted limit."),
)
_SHEET_EXPORT_ERRORS = (
    *_EXPORT_REQUEST_ERRORS,
    *_SHEET_EXPORT_INPUT_ERRORS,
    *_EXPORT_REPLAY_ERRORS,
)
_PROJECT_CAPABILITY_SPECS = _project_capability_registry(
    _project_capability(
        GroupSummarizer,
        PreparedGroupSummary,
        GroupSummaryOutput,
        _project_errors(
            *_RUNNING_RESERVATION_ERRORS,
            ("invalid_input_ref", "The grouped summary source is invalid."),
            ("model_cost_requires_confirmation", "Confirm the grouped model cost."),
            ("stale_input", "The source rows changed before publication."),
            write_failure="The grouped summary could not be published.",
        ),
        (
            "read_input_rows",
            "call_model_router",
            "create_sheet",
            "write_model_calls",
            "write_trace",
        ),
        required_capabilities=("project:write", "model:complete"),
        cost_kind="model_metered",
        requires_confirmation=True,
    ),
    _project_capability(
        FindScanner,
        PreparedFind,
        FindOutput,
        _project_errors(
            ("invalid_input_ref", "The finding source is invalid."),
            ("model_cost_requires_confirmation", "Confirm the finding scan cost."),
            ("action_job_required", "Find must run through the project queue."),
            ("stale_input", "The source or findings target changed."),
            write_failure="The findings could not be published.",
        ),
        (
            "read_input_rows",
            "call_model_router",
            "create_or_refresh_findings_sheet",
            "write_evidence_links",
            "write_model_calls",
        ),
        required_capabilities=("project:write", "model:complete"),
        cost_kind="model_metered",
        requires_confirmation=True,
    ),
    _project_capability(
        ValueClusterer,
        PreparedClustering,
        ClusteredValues,
        _project_errors(
            ("invalid_input_ref", "The cluster source column is invalid."),
            ("stale_input", "The reviewed source values changed."),
            ("network_disabled", "Project policy does not allow remote embeddings."),
            ("embedding_backend_unavailable", "No embedding backend is configured."),
            ("model_cost_requires_confirmation", "Confirm the embedding cost."),
            ("cluster_failed", "Value clustering could not complete."),
            ("stale_replay", "The source, canonical output or groups changed."),
            write_failure="Canonical values and reviewed groups could not be published.",
        ),
        (
            "read_input_rows",
            "embed_distinct_values",
            "create_column",
            "write_cluster_groups",
        ),
        cost_kind="model_metered",
        requires_confirmation=True,
    ),
    _project_capability(
        PageCapturer,
        PreparedPageCapture,
        CapturedPages,
        _project_errors(
            ("invalid_input_ref", "The source sheet, rows or URL column are invalid."),
            ("invalid_params", "The capture options or output names are invalid."),
            ("network_disabled", "Project policy does not allow page capture."),
            ("duplicate_sheet_name", "The links output sheet already exists."),
            ("url_capture_failed", "Every selected URL failed to produce a capture."),
            ("stale_input", "The source URLs changed before publication."),
            ("stale_replay", "The captured artifacts, outputs or lineage changed."),
            ("idempotency_in_progress", "This page capture is already running."),
            ("idempotency_stale_running", "A stale capture reservation was cleared."),
            write_failure="Page captures and their evidence could not be published.",
        ),
        (
            "read_input_rows",
            "call_static_http_capture_or_playwright",
            "stage_page_blobs",
            "create_primary_artifact_column",
            "or_create_links_child_sheet",
            "write_edit_op",
            "write_blob_metadata",
            "write_evidence_links",
        ),
        required_capabilities=("project:write", "external:url_capture"),
    ),
    _project_capability(
        EnclosureMaterializer,
        MaterializedEnclosures,
        MaterializedEnclosures,
        _project_errors(
            ("invalid_input_ref", "The selected enclosure rows are invalid."),
            ("media_download_failed", "An enclosure download failed."),
            ("stale_replay", "The recorded media cells or blobs changed."),
            ("idempotency_in_progress", "The materialization is already running."),
            ("idempotency_stale_running", "A stale reservation was cleared; retry."),
            write_failure="The materialization receipt could not be written.",
        ),
        (
            "fetch_external_media_url",
            "write_project_blob",
            "write_media_cell",
            "write_media_status",
        ),
        required_capabilities=("project:write", "external:media_download"),
        cost_kind="external_metered",
    ),
    *(
        _project_capability(
            capability,
            output,
            output,
            _project_errors(
                ("invalid_params", "The embedding operation arguments are invalid."),
                ("invalid_input_ref", "The source sheet or columns are invalid."),
                ("embedding_index_not_found", "The embedding index does not exist."),
                ("embedding_index_busy", "Another refresh holds the index lease."),
                (
                    "embedding_space_mismatch",
                    "Provider vectors do not match the index space.",
                ),
                (
                    "embedding_backend_unavailable",
                    "The selected embedder is unavailable.",
                ),
                (
                    "embedding_model_unsupported",
                    "The selected embedding model is unsupported.",
                ),
                (
                    "embedding_model_too_large",
                    "The model exceeds the deployment size limit.",
                ),
                (
                    "embedding_source_unsupported",
                    "The source modality cannot be embedded.",
                ),
                ("embedding_provider_error", "The embedding provider failed."),
                ("network_disabled", "Project network policy blocks remote embedding."),
                (
                    "embedding_remote_confirmation_required",
                    "Remote use requires provider policy consent.",
                ),
                (
                    "embedding_cost_requires_confirmation",
                    "Automatic refresh requires a finite cost ceiling.",
                ),
                (
                    "external_effect_reconciliation_required",
                    "A prior provider call requires reconciliation.",
                ),
                ("stale_replay", "The recorded embedding state has changed."),
                write_failure="Embedding metadata or accounting could not be written.",
            ),
            effects,
            required_capabilities=("project:write", "model:embed"),
            cost_kind="model_metered",
        )
        for capability, output, effects in (
            (
                IndexCreator,
                CreatedEmbeddingIndex,
                ("probe_embedding_dimension", "create_embedding_index"),
            ),
            (
                IndexRefresher,
                RefreshedEmbeddingIndex,
                ("embed_index_sources", "write_sidecar_vectors"),
            ),
        )
    ),
    _project_capability(
        PluginManifestLoader,
        PluginManifestRecord,
        PluginManifestRecord,
        _project_errors(
            (
                "invalid_plugin_manifest_source",
                "The manifest must be a readable local file.",
            ),
            (
                "invalid_plugin_manifest",
                "The manifest failed schema or contribution validation.",
            ),
            (
                "plugin_manifest_paid_write_plan_unsupported",
                "Paid queued plugin actions cannot declare project writes.",
            ),
            (
                "invalid_workbench_descriptor_package_source",
                "The descriptor package must be a regular local file.",
            ),
            (
                "invalid_workbench_descriptor_package",
                "The descriptor package failed validation.",
            ),
            ("invalid_plugin_package_source", "The plugin package failed validation."),
            (
                "plugin_frontend_module_path_escape",
                "A frontend module escapes the package.",
            ),
            (
                "plugin_frontend_module_type_invalid",
                "A frontend module must be JavaScript.",
            ),
            ("plugin_frontend_module_file_missing", "A frontend module is missing."),
            (
                "plugin_frontend_module_too_large",
                "A frontend module exceeds the size limit.",
            ),
            (
                "plugin_frontend_module_encoding_invalid",
                "A frontend module must be UTF-8.",
            ),
            write_failure="The plugin manifest receipt could not be written.",
        ),
        (
            "read_plugin_manifest",
            "validate_plugin_contributions",
            "write_plugin_manifest_receipt",
        ),
        required_capabilities=("project:write", "plugin:load"),
    ),
    _project_capability(
        IndexDeleter,
        DeletedEmbeddingIndex,
        DeletedEmbeddingIndex,
        _project_errors(
            ("invalid_params", "The deletion arguments are invalid."),
            ("embedding_index_not_found", "The embedding index does not exist."),
            ("stale_replay", "Deleted index data or export artifacts exist again."),
            write_failure="The index metadata, vectors, or receipt could not be updated.",
        ),
        (
            "delete_embedding_index",
            "delete_orphan_embedding_space",
            "delete_sidecar_vectors",
            "delete_export_artifacts",
        ),
    ),
    _project_capability(
        IndexExporter,
        IndexExport,
        IndexExport,
        _project_errors(
            ("invalid_params", "The export arguments are invalid."),
            ("embedding_index_not_found", "The embedding index does not exist."),
            ("embedding_export_sidecar_missing", "Refresh the index before exporting."),
            ("embedding_export_no_ready_vectors", "The index has no ready vectors."),
            ("embedding_source_stale", "The index sources changed; refresh first."),
            ("embedding_index_incomplete", "Refresh the incomplete index first."),
            ("embedding_index_scope_invalid", "The index source scope is invalid."),
            (
                "invalid_export_destination",
                "The destination must be an existing directory.",
            ),
            (
                "embedding_export_artifact_missing",
                "A prior export artifact is missing or changed.",
            ),
            ("stale_replay", "The exported index evidence no longer matches."),
            write_failure="The export artifacts or receipt could not be written.",
        ),
        ("read_sidecar_vectors", "write_export_artifact"),
    ),
    _project_capability(
        EmbeddingIndexPolicyUpdater,
        EmbeddingIndexPolicy,
        EmbeddingIndexPolicy,
        _project_errors(
            ("embedding_index_not_found", "The embedding index does not exist."),
            ("invalid_params", "The replacement policy is invalid."),
            (
                "stale_replay",
                "The stored index or policies no longer match the receipt.",
            ),
            write_failure="The index policy and receipt could not be updated.",
        ),
        ("update_embedding_index_policy",),
    ),
    _project_capability(
        SourceCreator,
        SourceRecord,
        SourceActionOutput,
        _project_errors(
            ("invalid_params", "The source metadata is invalid."),
            write_failure="The source and receipt could not be written.",
        ),
        ("create_source",),
    ),
    _project_capability(
        SourceUpdater,
        UpdatedSource,
        SourceUpdateActionOutput,
        _project_errors(
            ("invalid_params", "The source update is invalid."),
            ("invalid_input_ref", "The referenced source does not exist."),
            write_failure="The source and receipt could not be updated.",
        ),
        ("update_source",),
    ),
    _project_capability(
        SourceDeleter,
        DeletedSource,
        SourceDeleteActionOutput,
        _project_errors(
            ("invalid_params", "The source selector is invalid."),
            ("invalid_input_ref", "The referenced source does not exist."),
            write_failure="The source and receipt could not be deleted.",
        ),
        ("delete_source", "cascade_source_runs"),
    ),
    _project_capability(
        SourceChecker,
        CheckedSource,
        SourceCheckActionOutput,
        _project_errors(
            ("invalid_params", "The source check report is invalid."),
            ("invalid_input_ref", "The referenced source does not exist."),
            ("unsupported_source_kind", "RSS sources must be polled with source.poll."),
            write_failure="The source check and receipt could not be written.",
        ),
        ("write_source_run", "advance_source_cursor"),
    ),
    _project_capability(
        SheetRefresher,
        RefreshedSheet,
        RefreshedSheet,
        _project_errors(
            ("sheet_not_found", "The target sheet does not exist."),
            ("refresh_root_sheet_unsupported", "Root sheets cannot be refreshed."),
            ("refresh_unsupported", "This derived sheet cannot be refreshed in place."),
            ("sheet_refresh_busy", "Another refresh holds a live lease."),
            (
                "join_fanout_requires_confirmation",
                "Current join fan-out needs confirmation.",
            ),
            write_failure="The refresh and receipt could not be written.",
        ),
        ("replace_materialized_rows", "move_op_cursor", "advance_staleness_watermark"),
        cost_kind="none",
        requires_confirmation=True,
    ),
    _project_capability(
        RunBackfiller,
        PreparedBackfill,
        BackfilledRun,
        _project_errors(
            ("invalid_params", "The backfill preparation is invalid."),
            ("invalid_input_ref", "The target column is invalid."),
            ("invalid_row_ref", "Selected rows must be visible target-sheet rows."),
            ("column_not_found", "The requested column does not exist."),
            ("column_not_ai_generated", "The requested column is not AI-generated."),
            ("run_not_found", "The source generation run is missing."),
            ("run_required", "The column has no published source generation."),
            ("invalid_run_params", "The stored source action is invalid."),
            (
                "backfill_source_unavailable",
                "Selected rows lack published source heads.",
            ),
            (
                "mixed_origin_column_unsupported",
                "Choose rows from one source generation.",
            ),
            (
                "model_cost_requires_confirmation",
                "The source run requires fresh confirmation.",
            ),
            (
                "external_cost_requires_confirmation",
                "The external source requires fresh consent.",
            ),
            ("code_action_disabled", "The source action is disabled on this server."),
            ("output_column_busy", "A source output is claimed by another run."),
            ("local_engine_busy", "Another local transcription is still finishing."),
            (
                "local_artifact_unavailable",
                "The local model artifacts are unavailable.",
            ),
            (
                "local_session_failed",
                "The local session stopped; retry remaining rows.",
            ),
            ("idempotency_in_progress", "The backfill is already running."),
            ("idempotency_stale_running", "A stale backfill reservation was cleared."),
            write_failure="The backfill failed.",
        ),
        ("create_run", "write_run_results"),
        cost_kind="model_metered",
        requires_confirmation=True,
    ),
    _project_capability(
        TemporalExtractor,
        PreparedTemporalExtract,
        ExtractedRanges,
        _project_errors(
            *_RUNNING_RESERVATION_ERRORS,
            *_TEMPORAL_PROJECT_ERRORS,
            ("invalid_input_ref", "The media or selection reference is invalid."),
            ("invalid_range", "Select exactly one valid range per source row."),
            (
                "selection_unmappable",
                "The selection cannot map to the source timeline.",
            ),
            ("stale_input", "A source changed before clip publication."),
            ("cut_alignment_failed", "The renderer could not align the exact range."),
            (
                "temporal_materializer_unavailable",
                "The exact media renderer is unavailable.",
            ),
            (
                "literal_selection_requires_confirmation",
                "Acknowledge applying one literal range to multiple rows.",
            ),
            write_failure="Temporal clips and their evidence could not be published.",
        ),
        ("create_column", "write_media_blob", "write_temporal_lineage"),
    ),
    _project_capability(
        SourcePoller,
        PolledSource,
        SourcePollOutput,
        _project_errors(
            ("invalid_input_ref", "The source does not exist."),
            ("unsupported_source_kind", "No registered poller supports this source."),
            (
                "unsupported_source_config",
                "The poller rejected the source configuration.",
            ),
            ("source_poll_failed", "The poll failed and recorded an error source run."),
            write_failure="Source poll publication and receipt could not be written.",
        ),
        (
            "create_or_read_source",
            "fetch_external_source",
            "dedupe_source_items",
            "create_source_rows",
            "write_source_run",
            "advance_source_cursor",
        ),
        required_capabilities=("project:write", "external:source_poll"),
        cost_kind="external_metered",
    ),
    _project_capability(
        ColumnPatcher,
        PatchedColumn,
        ColumnPatchActionOutput,
        _project_errors(
            ("invalid_params", "The column patch is invalid."),
            ("invalid_column_ref", "The target must identify a visible column."),
            ("invalid_column_format", "The display format is not supported."),
            ("output_column_busy", "The target column is claimed by a running action."),
            write_failure="The column patch and receipt could not be written.",
        ),
        (
            "read_column_metadata",
            "update_column_format",
            "write_column_patch_op",
        ),
    ),
    _project_capability(
        ColumnCreator,
        CreatedColumn,
        ColumnAddActionOutput,
        _project_errors(
            ("invalid_params", "The column definition is invalid."),
            ("invalid_column_name", "The column name is invalid."),
            ("invalid_column_type", "The column type is unavailable."),
            ("column_exists", "A visible column with this name already exists."),
            ("sheet_not_found", "The target sheet was not found or is hidden."),
            write_failure="The column and receipt could not be written.",
        ),
        (
            "read_sheet_columns",
            "write_source_column",
            "write_add_column_op",
        ),
    ),
    _project_capability(
        RowCreator,
        CreatedRow,
        RowAddActionOutput,
        _project_errors(
            ("invalid_params", "The row definition is invalid."),
            ("invalid_row_ref", "The target sheet or cell column names are invalid."),
            ("sheet_not_found", "The target sheet was not found or is hidden."),
            ("invalid_temporal_value", "The temporal value is malformed."),
            ("timeline_not_found", "The temporal value refers to a missing timeline."),
            ("timeline_stale", "The temporal value timeline anchor is stale."),
            ("timeline_duration_required", "The timeline duration is not finalized."),
            ("ambiguous_time_mapping", "The temporal mapping is ambiguous."),
            (
                "range_out_of_bounds",
                "The temporal value exceeds its timeline duration.",
            ),
            write_failure="The row and receipt could not be written.",
        ),
        ("read_sheet_columns", "write_source_row", "write_add_row_op"),
    ),
    _project_capability(
        RowsAppender,
        AppendedRows,
        RowsAppendActionOutput,
        _project_errors(
            ("invalid_params", "The imported rows are invalid."),
            (
                "incompatible_append_mapping",
                "The import mapping is incompatible with the destination sheet.",
            ),
            (
                "derived_append_unsupported",
                "Derived sheets are owned by their source operation and cannot receive imported rows.",
            ),
            ("stale_replay", "The appended rows were undone or reclaimed."),
            ("sheet_not_found", "The destination sheet was not found or is hidden."),
            ("csv_parse_failed", "The CSV source could not be parsed."),
            ("invalid_csv_value", "A CSV value does not match the destination type."),
            ("xlsx_parse_failed", "The XLSX source could not be parsed."),
            (
                "xlsx_header_mismatch",
                "The XLSX source header does not match its mapping.",
            ),
            (
                "invalid_xlsx_value",
                "An XLSX value does not match the destination type.",
            ),
            (
                "import_workload_limit_exceeded",
                "The import exceeds the deployment row limit.",
            ),
            *_TEMPORAL_PROJECT_ERRORS,
            write_failure="The imported rows and receipt could not be written.",
        ),
        ("read_sheet_columns", "write_source_rows", "write_append_rows_op"),
    ),
    _project_capability(
        RowsUpdater,
        UpdatedImportedRows,
        RowsAppendActionOutput,
        _project_errors(
            ("invalid_params", "The imported rows are invalid."),
            (
                "incompatible_update_mapping",
                "The update mapping is incompatible with the destination sheet.",
            ),
            (
                "ambiguous_import_keys",
                "Duplicate exact keys make this update ambiguous.",
            ),
            (
                "stale_import_preview",
                "The reviewed import preview is no longer current.",
            ),
            ("sheet_not_found", "The destination sheet was not found or is hidden."),
            (
                "derived_update_unsupported",
                "Derived sheets cannot receive imported updates.",
            ),
            ("output_column_busy", "An update column is in use by another action."),
            *_TEMPORAL_PROJECT_ERRORS,
            write_failure="The imported updates and receipt could not be written.",
        ),
        (
            "read_sheet_columns",
            "read_sheet_rows",
            "write_edit_overlay",
            "write_edit_op",
        ),
    ),
    _project_capability(
        RowDeleter,
        DeletedRows,
        RowDeleteActionOutput,
        _project_errors(
            ("invalid_params", "The row selector is invalid."),
            (
                "invalid_row_ref",
                "A target row is missing, hidden, or on another sheet.",
            ),
            ("sheet_not_found", "The target sheet was not found or is hidden."),
            write_failure="The row deletion and receipt could not be written.",
        ),
        ("read_sheet_rows", "hide_source_rows", "write_delete_rows_op"),
    ),
    _project_capability(
        ColumnTyper,
        RetypedColumn,
        ColumnSetTypeActionOutput,
        _project_errors(
            ("invalid_column_ref", "The target must identify a visible column."),
            ("invalid_column_type", "The destination type must be registered."),
            ("column_value_validation_failed", "A current value is incompatible."),
            *_TEMPORAL_PROJECT_ERRORS,
            ("output_column_busy", "The target column is claimed by a running action."),
            write_failure="The column type and receipt could not be written.",
        ),
        (
            "read_column_values",
            "update_column_type",
            "write_column_type_op",
        ),
    ),
    _project_capability(
        CellEditor,
        EditedCells,
        CellEditActionOutput,
        _project_errors(
            ("cell_target_not_found", "A target cell is not visible."),
            ("column_value_validation_failed", "An edit value is incompatible."),
            *_TEMPORAL_PROJECT_ERRORS,
            ("output_column_busy", "A target column is claimed by a running action."),
            write_failure="The edits and receipt could not be written.",
        ),
        ("read_source_cell", "write_edit_overlay", "write_edit_op"),
    ),
    _project_capability(
        QueryCellEditor,
        QueryEditedCells,
        CellEditQueryActionOutput,
        _project_errors(
            ("invalid_query_spec", "The query is malformed or unsupported."),
            ("unsupported_query_kind", "Only sheet.filter queries are supported."),
            ("invalid_query_filter", "The filter or sort could not be evaluated."),
            ("query_rowset_empty", "The query resolved no rows."),
            ("query_rowset_too_large", "The query exceeds the deployment row limit."),
            (
                "invalid_column_ref",
                "The target column is not visible on the query sheet.",
            ),
            ("column_value_validation_failed", "The edit value is incompatible."),
            *_TEMPORAL_PROJECT_ERRORS,
            ("output_column_busy", "The target column is claimed by a running action."),
            write_failure="The edits and receipt could not be written.",
        ),
        (
            "read_query_rowset",
            "read_source_cell",
            "write_edit_overlay",
            "write_edit_op",
        ),
    ),
    _project_capability(
        ReviewDecider,
        ReviewDecision,
        ReviewDecisionActionOutput,
        _project_errors(
            ("review_value_required", "Edit decisions require a replacement value."),
            ("review_target_not_found", "The current result cell was not found."),
            ("output_column_busy", "The result column is claimed by a running action."),
            write_failure="The review decision and receipt could not be written.",
        ),
        (
            "read_result_cell",
            "write_review_state",
            "write_edit_overlay",
            "write_review_op",
        ),
    ),
    _project_capability(
        ReplayValueAcceptor,
        AcceptedReplayValue,
        ReplayAcceptActionOutput,
        _project_errors(
            ("invalid_replay_target", "The replay target does not exist."),
            ("mixed_origin_column_unsupported", "Replay requires one current origin."),
            ("stale_replay", "The regenerated run or value identity changed."),
            (
                "output_column_busy",
                "The generated column is claimed by a running action.",
            ),
            write_failure="The replay acceptance and receipt could not be written.",
        ),
        ("read_pending_value", "write_edit_overlay", "write_edit_op"),
    ),
    _project_capability(
        ReplayColumnAcceptor,
        AcceptedReplayColumn,
        ReplayAcceptColumnActionOutput,
        _project_errors(
            ("invalid_replay_target", "The replay column does not exist."),
            ("mixed_origin_column_unsupported", "Replay requires one current origin."),
            (
                "output_column_busy",
                "The generated column is claimed by a running action.",
            ),
            write_failure="The column replay acceptance and receipt could not be written.",
        ),
        ("read_pending_values", "write_edit_overlay", "write_edit_op"),
    ),
    _project_capability(
        ReplayValueDismissor,
        DismissedReplayValue,
        ReplayDismissActionOutput,
        _project_errors(
            ("invalid_replay_target", "The replay target does not exist."),
            ("mixed_origin_column_unsupported", "Replay requires one current origin."),
            ("stale_replay", "The regenerated run or value identity changed."),
            write_failure="The replay dismissal and receipt could not be written.",
        ),
        ("read_pending_value", "write_replay_dismissal"),
    ),
    _project_capability(
        OperationUndoer,
        OperationTransition,
        OperationStepActionOutput,
        _project_errors(
            ("operation_unavailable", "No operation is available to undo."),
            (
                "operation_mismatch",
                "The available operation does not match the request.",
            ),
            ("irreversible_barrier", "The target operation cannot be undone."),
            ("output_column_busy", "The target operation touches a claimed column."),
            ("project_state_corrupt", "The target operation state is invalid."),
            write_failure="The undo and receipt could not be written.",
        ),
        (
            "read_operation_cursor",
            "transition_operation_status",
            "move_op_cursor",
            "apply_undo_info",
        ),
    ),
    _project_capability(
        OperationRedoer,
        OperationTransition,
        OperationStepActionOutput,
        _project_errors(
            ("operation_unavailable", "No operation is available to redo."),
            (
                "operation_mismatch",
                "The available operation does not match the request.",
            ),
            ("irreversible_barrier", "The target operation cannot be redone."),
            ("output_column_busy", "The target operation touches a claimed column."),
            ("project_state_corrupt", "The target operation state is invalid."),
            write_failure="The redo and receipt could not be written.",
        ),
        (
            "read_operation_cursor",
            "transition_operation_status",
            "move_op_cursor",
            "apply_undo_info",
        ),
    ),
    _project_capability(
        QueryPreviewer,
        QueryPreviewResult,
        QueryPreviewResult,
        _project_errors(
            ("invalid_query_params", "The query operation arguments are invalid."),
            ("invalid_query_spec", "The query is malformed or unsupported."),
            ("unsupported_query_kind", "The query kind cannot be previewed."),
            ("invalid_query_filter", "The sheet filter could not be evaluated."),
            ("invalid_sheet_ref", "The query sheet does not exist or is hidden."),
            ("embedding_index_not_found", "The embedding index does not exist."),
            ("embedding_index_incomplete", "The embedding index is incomplete."),
            ("embedding_index_scope_invalid", "The embedding index scope is invalid."),
            ("embedding_anchor_not_found", "The similarity anchor was not found."),
            ("embedding_source_stale", "The stored similarity anchor is stale."),
            ("stale_input", "The query inputs changed while the preview was running."),
            ("embedding_source_unsupported", "The embedding source is unsupported."),
            ("embedding_space_mismatch", "The query and index spaces do not match."),
            (
                "embedding_composed_query_degenerate",
                "The composed query has no direction.",
            ),
            (
                "embedding_backend_unavailable",
                "The local embedding backend is unavailable.",
            ),
            ("embedding_provider_error", "The local embedding provider failed."),
            (
                "embedding_remote_preview_unsupported",
                "This action does not send manual or hybrid queries to remote providers.",
            ),
            write_failure="The query receipt could not be written.",
        ),
        ("read_query_rowset", "evaluate_local_embedding_query"),
        required_capabilities=("project:read",),
        writes_project=False,
        callable_host=True,
    ),
    _project_capability(
        FollowTheMoneyImporter,
        ImportedEntityDataset,
        ImportedEntityDataset,
        _project_errors(
            (
                "followthemoney_unavailable",
                "Install the entities extra to use FollowTheMoney.",
            ),
            ("invalid_file_source", "The admitted local source could not be read."),
            ("invalid_ftm_import", "The entity dataset cannot be imported."),
            ("stale_replay", "The imported entity sheets are no longer active."),
            write_failure="The entity dataset and receipt could not be published.",
        ),
        ("read_local_file", "create_sheets", "append_rows"),
        required_capabilities=("project:read", "project:write"),
        callable_host=True,
    ),
    _project_capability(
        FollowTheMoneyExporter,
        ExportedEntityPackage,
        ExportedEntityPackage,
        _project_errors(
            (
                "followthemoney_unavailable",
                "Install the entities extra to use FollowTheMoney.",
            ),
            ("invalid_ftm_export", "The entity package arguments are invalid."),
            ("invalid_rowset_spec", "The selected rowset is invalid."),
            ("invalid_sheet_ref", "The selected sheet must be visible."),
            ("invalid_materialized_sheet", "The selected sheet is not materialized."),
            ("export_artifact_missing", "The exported package is unavailable."),
            ("export_artifact_mismatch", "The exported package bytes changed."),
            write_failure="The entity package and receipt could not be published.",
        ),
        ("read_investigative_rowsets", "write_blob_store"),
        required_capabilities=("project:read", "project:write"),
        callable_host=True,
    ),
    _project_capability(
        WorkLogExporter,
        ExportedWorkLog,
        ExportWorkLogOutput,
        (
            *_LOCAL_EXPORT_ERRORS,
            ("invalid_params", "The work-log export arguments are invalid."),
        ),
        ("read_project_state", "write_export_artifact"),
        required_capabilities=("project:read", "project:write"),
        callable_host=True,
    ),
    _project_capability(
        ColumnTablesExporter,
        ColumnTablesExport,
        ColumnTablesExport,
        (
            *_EXPORT_REQUEST_ERRORS,
            ("invalid_params", "The export arguments or domain result are invalid."),
            ("invalid_sheet_ref", "The source sheet must be visible."),
            ("invalid_column_ref", "The source must be a JSON column on the sheet."),
            ("invalid_group_by", "The grouping field is not available."),
            ("invalid_exclude_columns", "Excluded fields are not available."),
            ("invalid_name_template", "The artifact name template is invalid."),
            ("source_not_list", "A source cell is not a list."),
            ("empty_source_column", "The source has no list items."),
            ("column_shape_mismatch", "List items have incompatible shapes."),
            ("empty_output_columns", "Every output field was excluded."),
            *_EXPORT_REPLAY_ERRORS,
        ),
        ("read_sheet_column_values", "write_export_artifact"),
        required_capabilities=("project:read", "project:write"),
    ),
    _project_capability(
        SheetCsvExporter,
        ExportedCsvSheetFile,
        ExportSheetCsvOutput,
        (
            *_SHEET_EXPORT_ERRORS,
            ("invalid_params", "The CSV export arguments are invalid."),
        ),
        ("read_sheet_values", "write_export_artifact"),
        required_capabilities=("project:read", "project:write"),
        callable_host=True,
    ),
    _project_capability(
        SheetJsonlExporter,
        ExportedJsonlSheetFile,
        ExportSheetJsonlOutput,
        (
            *_EXPORT_REQUEST_ERRORS,
            *_SHEET_EXPORT_INPUT_ERRORS,
            ("invalid_params", "The JSONL export arguments are invalid."),
            ("export_column_collision", "Flattened export column names collided."),
            *_EXPORT_REPLAY_ERRORS,
        ),
        ("read_sheet_values", "write_export_artifact"),
        required_capabilities=("project:read", "project:write"),
        callable_host=True,
    ),
    _project_capability(
        SheetParquetExporter,
        ExportedParquetSheetFile,
        ExportSheetParquetOutput,
        (
            *_EXPORT_REQUEST_ERRORS,
            *_SHEET_EXPORT_INPUT_ERRORS,
            ("invalid_params", "The Parquet export arguments are invalid."),
            ("export_column_collision", "Flattened export column names collided."),
            ("parquet_writer_unavailable", "The Parquet writer is unavailable."),
            ("parquet_render_failed", "The sheet could not be rendered as Parquet."),
            *_EXPORT_REPLAY_ERRORS,
        ),
        ("read_sheet_values", "write_export_artifact"),
        required_capabilities=("project:read", "project:write"),
        callable_host=True,
    ),
)


class ActionCategory(StrEnum):
    TEXT = "text"
    CLEANUP = "cleanup"
    CONVERT = "convert"
    EXTRACT = "extract"
    SOURCES = "sources"


class RowScope(StrEnum):
    """The row selectors an action accepts."""

    SELECTABLE_ROWS = "selectable_rows"
    ALL_ROWS = "all_rows"


@dataclass(frozen=True)
class ExportTarget:
    """Presentation metadata for an action exposed on the project export surface."""

    label: str
    destination_kind: str
    source_modes: tuple[str, ...] = ("current_sheet", "current_view")
    destination_modes: tuple[str, ...] = ("download",)
    connection_provider: str | None = None
    connection_scopes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not all(value.strip() for value in (self.label, self.destination_kind)):
            raise ValueError("export target values must be non-empty")

    def ui_hint(self, *, form: str) -> dict[str, Any]:
        return {
            "surface": "project.export",
            "label": self.label,
            "destination_kind": self.destination_kind,
            "form": form,
            "source_modes": list(self.source_modes),
            "destination_modes": list(self.destination_modes),
            **(
                {
                    "requires_connection": {
                        "provider": self.connection_provider,
                        "scopes": list(self.connection_scopes),
                    }
                }
                if self.connection_provider is not None
                else {}
            ),
        }


@dataclass(frozen=True)
class OutputField:
    key: str
    column_type: str
    schema: Mapping[str, Any]
    annotation: Any = Any
    format: str | None = None
    hidden: bool = False
    route: RoutedOutput | None = None
    default_hidden: bool = False
    named_result: NamedResultTarget | None = None
    semantic_type: str | None = None

    def materialized_name(self, names: Mapping[str, str]) -> str:
        if self.hidden:
            return (
                self.route.materialized_name(self.key)
                if self.route is not None
                else self.key
            )
        return names.get(self.key, self.key)


@dataclass(frozen=True)
class MapRows(Generic[P, OutputT]):
    handler: Callable[..., RowResult[OutputT]]
    params_model: type[P]
    output_model: type[OutputT]
    output_fields: tuple[OutputField, ...]
    dynamic_outputs: Callable[[P], Mapping[str, Any]] | None = None
    active_outputs: Callable[[P], typing.Iterable[str]] | None = None
    injections: tuple[type, ...] = ()
    engine_param: str | None = None

    engine_options: Callable[[P], OcrOptions | TranscriptionOptions] | None = None

    @property
    def capabilities(self) -> tuple[type, ...]:
        return tuple(
            kind
            for kind in self.injections
            if kind not in (InvocationContext, PluginSecrets)
        )

    def resolve_engine_options(self, params: P, engine: str) -> dict[str, Any]:
        options_type = _ENGINE_OPTIONS.get(routed_capability(self))
        if options_type is None:
            return {}
        options = self.engine_options(params) if self.engine_options else options_type()
        if type(options) is not options_type:
            raise TypeError(f"engine_options must return {options_type.__name__}")
        return options.normalize(engine)

    def resolve_output_fields(self, params: P) -> tuple[OutputField, ...]:
        if self.active_outputs is not None:
            selected = self.active_outputs(params)
            if isinstance(selected, (str, bytes, MappingABC)):
                raise TypeError(
                    "active_outputs must return an iterable of logical keys"
                )
            keys = tuple(selected)
            if not keys or any(
                not isinstance(key, str) or not key or key != key.strip()
                for key in keys
            ):
                raise TypeError(
                    "active_outputs must return non-empty, trimmed logical keys"
                )
            if len(set(keys)) != len(keys):
                raise TypeError("active_outputs must not repeat logical keys")
            unknown = set(keys) - {field.key for field in self.output_fields}
            if unknown:
                raise TypeError(f"unknown active outputs: {', '.join(sorted(unknown))}")
            fields = tuple(field for field in self.output_fields if field.key in keys)
        else:
            fields = _resolve_output_fields(
                self.output_fields, self.dynamic_outputs, params
            )
        if TopicSectionsReader in self.capabilities:
            sidecars = topic_sidecar_names(
                field.key
                for field in fields
                if field.column_type == "timeline_ranges" and not field.hidden
            )
            if set(sidecars.values()) & {field.key for field in fields}:
                raise TypeError(
                    "topic analysis output collides with a host-owned sidecar"
                )
            fields = (
                *fields,
                *(
                    OutputField(
                        name, "json", {"type": "object"}, JsonValue, hidden=True
                    )
                    for name in sidecars.values()
                ),
            )
        return fields


@dataclass(frozen=True)
class SemanticJoin(MapRows[P, OutputT]):
    """Row matching whose source outputs and linked table publish together."""

    def child_output_keys(self, params: P) -> tuple[str, ...]:
        return tuple(self.child_source_columns(params))

    def child_source_columns(self, params: P) -> dict[str, ColumnRef[Any]]:
        sources = [
            value for value in vars(params).values() if isinstance(value, ColumnRef)
        ]
        if len(sources) != 1:
            raise ValueError("semantic_join requires exactly one source ColumnRef")
        carry = [
            column
            for value in vars(params).values()
            if isinstance(value, list)
            for column in value
            if isinstance(column, ColumnRef)
        ]
        names = [sources[0].name, *(column.name for column in carry)]
        if len(set(names)) != len(names):
            raise ValueError(
                "semantic_join carry columns must be distinct and exclude the source"
            )
        return {
            "source": sources[0],
            **{f"carry.{column.name}": column for column in carry},
        }


@dataclass(frozen=True)
class MapBatch(MapRows[P, OutputT]):
    handler: Callable[..., Mapping[int, RowResult[OutputT] | RowError]]


@dataclass(frozen=True)
class ModelRows(Generic[P, OutputT]):
    """Pure per-row prompt renderer executed by the host's existing MapRunner."""

    renderer: Callable[..., ModelPrompt[OutputT]]
    params_model: type[P]
    output_model: type[OutputT]
    output_fields: tuple[OutputField, ...]
    model_param: str
    source_param: str
    evaluation: ModelRowsEvaluation | None = None
    response_model: type[BaseModel] | None = None
    complete: Callable[..., RowResult[OutputT]] | None = None
    direct: MapRows[P, OutputT] | None = None
    dynamic_outputs: Callable[[P], Mapping[str, Any]] | None = None
    active_outputs: Callable[[P], typing.Iterable[str]] | None = None

    def uses_model(self, params: P) -> bool:
        selected = getattr(params, self.model_param)
        if selected is not None:
            if not isinstance(selected, ModelRef):
                raise TypeError("model selection must be a ModelRef")
            return True
        if self.direct is None:
            raise ValueError("a model must be selected")
        return False

    def resolve_output_fields(self, params: P) -> tuple[OutputField, ...]:
        return MapRows.resolve_output_fields(self, params)

    @property
    def capabilities(self) -> tuple[type, ...]:
        return ()

    def validate_source(self, params: P) -> None:
        _validate_model_rows_source(getattr(params, self.source_param))
        self.uses_model(params)


@dataclass(frozen=True)
class _DirectModelRows(MapRows[P, OutputT]):
    """The selected ordinary row path retains its admitted rich source."""

    source_param: str = ""


@dataclass(frozen=True)
class ModelRowsEvaluation:
    """Closed host profile for evaluating one generated column."""

    subject_param: str
    guidelines_param: str
    upstream_prompt_param: str


@dataclass(frozen=True)
class ModelRowsEvaluationContext:
    """Immutable provenance resolved by the host before a renderer is called."""

    subject_column: str
    subject_column_id: int
    source_run_id: int
    original_prompt: str | None = None
    subject_row_ids: tuple[int, ...] = ()
    subject_value_refs: tuple[Mapping[str, Any], ...] = ()
    source_receipt_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "subject_value_refs",
            tuple(MappingProxyType(dict(ref)) for ref in self.subject_value_refs),
        )

    def payload(self) -> dict[str, Any]:
        return {
            "subject_column": self.subject_column,
            "subject_column_id": self.subject_column_id,
            "source_run_id": self.source_run_id,
            "original_prompt": self.original_prompt,
            "subject_row_ids": list(self.subject_row_ids),
            "subject_value_refs": [dict(ref) for ref in self.subject_value_refs],
            "source_receipt_id": self.source_receipt_id,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> ModelRowsEvaluationContext:
        return cls(
            subject_column=str(payload["subject_column"]),
            subject_column_id=int(payload["subject_column_id"]),
            source_run_id=int(payload["source_run_id"]),
            original_prompt=(
                str(payload["original_prompt"])
                if payload.get("original_prompt") is not None
                else None
            ),
            subject_row_ids=tuple(
                int(row_id) for row_id in payload.get("subject_row_ids", [])
            ),
            subject_value_refs=tuple(
                dict(ref)
                for ref in payload.get("subject_value_refs", [])
                if isinstance(ref, Mapping)
            ),
            source_receipt_id=(
                str(payload["source_receipt_id"])
                if payload.get("source_receipt_id") is not None
                else None
            ),
        )


@dataclass(frozen=True)
class ColumnTransform(Generic[P, OutputT]):
    """One-source, one-output transform committed as a single transaction."""

    handler: Callable[..., Mapping[int, RowResult[OutputT]]]
    params_model: type[P]
    output_model: type[OutputT]
    output_fields: tuple[OutputField, ...]
    output_type: (
        Callable[[P, ColumnTransformContext, Mapping[int, RowResult[OutputT]]], str]
        | None
    ) = None
    preflight: Callable[[P, ColumnTransformContext], None] | None = None
    takes_context: bool = False

    def resolve_output_fields(self, params: P) -> tuple[OutputField, ...]:
        del params
        return self.output_fields

    def resolved_output_field(
        self,
        params: P,
        context: ColumnTransformContext,
        results: Mapping[int, RowResult[OutputT]],
    ) -> OutputField:
        [output_field] = self.output_fields
        if self.output_type is None:
            return output_field
        column_type = self.output_type(params, context, results)
        from frisket.authoring.column_types import is_registered

        if (
            not isinstance(column_type, str)
            or not column_type.strip()
            or not is_registered(column_type.strip())
        ):
            raise TypeError("column_transform output_type must return a column type")
        return OutputField(
            output_field.key,
            column_type.strip(),
            output_field.schema,
            output_field.annotation,
        )


@dataclass(frozen=True)
class CreateSheet(Generic[P, OutputT]):
    """One typed table published atomically as a new project sheet."""

    handler: Callable[..., TableResult[OutputT] | DynamicTableResult]
    params_model: type[P]
    output_model: type[OutputT]
    columns_from: Callable[[P], typing.Iterable[TableColumn] | None] | None
    output_fields: tuple[OutputField, ...] = ()
    injections: tuple[type, ...] = ()
    runtime_schema: bool = False

    @property
    def capabilities(self) -> tuple[type, ...]:
        return tuple(kind for kind in self.injections if kind is not PluginSecrets)

    def output_bindings(self) -> Mapping[str, str]:
        """Bind static logical keys to live model attributes before serialization."""
        if self.output_model is DynamicOutput:
            return MappingProxyType({})
        return MappingProxyType(
            {
                _table_output_key(name, info): name
                for name, info in self.output_model.model_fields.items()
            }
        )

    def resolve_columns(self, params: P) -> tuple[TableColumn, ...] | None:
        if self.columns_from is None:
            if self.runtime_schema:
                return None
            return tuple(
                TableColumn(field.key, field.column_type, field.format, field.hidden)
                for field in self.output_fields
            )
        columns = self.columns_from(params)
        if columns is None:
            if self.runtime_schema:
                return None
            raise TypeError("columns_from may return None only for a runtime table")
        return self.validate_columns(columns)

    @staticmethod
    def validate_columns(
        columns: typing.Iterable[TableColumn],
    ) -> tuple[TableColumn, ...]:
        columns = tuple(columns)
        if not columns:
            raise TypeError("create_sheet columns_from must return at least one column")
        if any(not isinstance(column, TableColumn) for column in columns):
            raise TypeError("create_sheet columns_from must return TableColumn values")
        keys = [column.key for column in columns]
        if len(keys) != len(set(keys)):
            raise TypeError("create_sheet columns must have unique keys")
        return columns

    @staticmethod
    def fields_from_columns(
        columns: typing.Iterable[TableColumn],
    ) -> tuple[OutputField, ...]:
        return tuple(
            OutputField(column.key, column.type, {}, Any, column.format, column.hidden)
            for column in CreateSheet.validate_columns(columns)
        )

    def resolve_output_fields(self, params: P) -> tuple[OutputField, ...] | None:
        if self.columns_from is None and not self.runtime_schema:
            return self.output_fields
        columns = self.resolve_columns(params)
        return None if columns is None else self.fields_from_columns(columns)


_ROW_SCOPED_PROJECT_CAPABILITIES = frozenset(
    {
        RunBackfiller,
        EnclosureMaterializer,
        TemporalExtractor,
        PageCapturer,
        ValueClusterer,
        GroupSummarizer,
        FindScanner,
        RowsAppender,
        RowsUpdater,
    }
)
_OUTPUT_NAMING_PROJECT_CAPABILITIES = frozenset(
    {
        TemporalExtractor,
        PageCapturer,
        ValueClusterer,
        GroupSummarizer,
        FindScanner,
    }
)
_SHEET_CREATING_PROJECT_CAPABILITIES = frozenset({GroupSummarizer, FindScanner})
_DYNAMIC_OUTPUT_PROJECT_CAPABILITIES = frozenset(
    {TemporalExtractor, PageCapturer, FindScanner}
)


@dataclass(frozen=True)
class _ProjectAction(Generic[P, OutputT]):
    handler: Callable[..., OutputT]
    params_model: type[P]
    injections: tuple[type[Any], ...]
    return_type: Any
    return_adapter: TypeAdapter[Any] = field(repr=False, compare=False)
    return_validator: SchemaValidator = field(repr=False, compare=False)
    return_serializer: SchemaSerializer = field(repr=False, compare=False)
    output_fields: tuple[OutputField, ...] = ()

    @property
    def capabilities(self) -> tuple[type[Any], ...]:
        return tuple(
            kind
            for kind in self.injections
            if kind not in (InvocationContext, PluginSecrets)
        )

    @property
    def specs(self) -> tuple[ProjectCapabilitySpec, ...]:
        return tuple(_PROJECT_CAPABILITY_SPECS[cap] for cap in self.capabilities)

    @property
    def callable_host(self) -> bool:
        return all(spec.callable_host for spec in self.specs)

    def single_capability(self) -> type[Any]:
        (capability,) = self.capabilities
        return capability

    def single_spec(self) -> ProjectCapabilitySpec:
        (spec,) = self.specs
        return spec

    @property
    def output_model(self) -> Any:
        # Specialized preparations still project their materialized result,
        # not an opaque preparation. Ordinary callables expose their return.
        return (
            self.return_type if self.callable_host else self.single_spec().output_model
        )

    def resolve_output_fields(self, params: P) -> tuple[OutputField, ...] | None:
        del params
        return (
            None
            if len(self.capabilities) == 1
            and self.capabilities[0] in _DYNAMIC_OUTPUT_PROJECT_CAPABILITIES
            else self.output_fields
        )


@dataclass(frozen=True)
class GoogleSheetsExport(Generic[P]):
    """One pure producer; the host admits and executes its actual export intent."""

    handler: Callable[[P], GoogleSheetsExportRequest]
    params_model: type[P]
    output_model: type[BaseModel] = GoogleSheetsExportOutput
    output_fields: tuple[OutputField, ...] = ()

    @property
    def spec(self) -> ProjectCapabilitySpec:
        return _GOOGLE_SHEETS_EXPORT_SPEC

    def resolve_output_fields(self, params: P) -> tuple[OutputField, ...]:
        return ()


_GOOGLE_SHEETS_EXPORT_SPEC = _project_capability(
    GoogleSheetsExportRequest,
    GoogleSheetsExportRequest,
    GoogleSheetsExportOutput,
    _project_errors(
        ("connected_account_not_found", "The Google connected account is unavailable."),
        ("google_sheets_client_unavailable", "No Google Sheets client is configured."),
        (
            "irreversible_external_requires_confirmation",
            "Confirm the exact external write before queueing.",
        ),
        ("invalid_export_destination", "The Google Sheets destination is invalid."),
        ("invalid_sheet_ref", "The export source sheet is invalid."),
        ("invalid_query_spec", "The export query is invalid."),
        ("invalid_query_filter", "The export query filter is invalid."),
        ("export_rowset_too_large", "The export row limit was exceeded."),
        (
            "external_effect_reconciliation_required",
            "The external write requires reconciliation before retry.",
        ),
        write_failure="The Google Sheets receipt could not be written.",
    ),
    (
        "read_sheet_values",
        "refresh_connected_account_token",
        "write_google_spreadsheet",
    ),
    required_capabilities=("project:read", "project:write", "external:google_sheets"),
    requires_confirmation=True,
)


def google_sheets_export(
    prepare: Callable[[P], GoogleSheetsExportRequest],
) -> GoogleSheetsExport[P]:
    if inspect.iscoroutinefunction(prepare):
        raise TypeError("Google Sheets export producer must be synchronous")
    parameters = tuple(inspect.signature(prepare).parameters.values())
    hints = get_type_hints(prepare)
    if (
        len(parameters) != 1
        or parameters[0].kind
        not in (parameters[0].POSITIONAL_ONLY, parameters[0].POSITIONAL_OR_KEYWORD)
        or not _is_subclass(hints.get(parameters[0].name), ActionParams)
        or hints.get("return") is not GoogleSheetsExportRequest
    ):
        raise TypeError(
            "Google Sheets export producer must accept Params and return GoogleSheetsExportRequest"
        )
    return GoogleSheetsExport(prepare, hints[parameters[0].name])


def _is_single_project_capability(
    terminal: Any, capabilities: frozenset[type[Any]]
) -> bool:
    return (
        isinstance(terminal, _ProjectAction)
        and len(terminal.capabilities) == 1
        and terminal.capabilities[0] in capabilities
    )


def _creates_sheet(terminal: Any) -> bool:
    return isinstance(terminal, (CreateSheet, SemanticJoin)) or (
        _is_single_project_capability(terminal, _SHEET_CREATING_PROJECT_CAPABILITIES)
    )


def _accepts_sheet_name(terminal: Any) -> bool:
    return _creates_sheet(terminal) or (
        isinstance(terminal, _ProjectAction)
        and terminal.capabilities == (PageCapturer,)
    )


def _accepts_output_names(terminal: Any) -> bool:
    return not isinstance(terminal, (_ProjectAction, GoogleSheetsExport)) or (
        _is_single_project_capability(terminal, _OUTPUT_NAMING_PROJECT_CAPABILITIES)
    )


def has_dynamic_outputs(terminal: Any) -> bool:
    """Whether logical output keys require Params or table inspection."""

    return (
        isinstance(terminal, SemanticJoin)
        or (
            isinstance(terminal, CreateSheet)
            and (terminal.columns_from is not None or terminal.runtime_schema)
        )
        or (
            getattr(terminal, "dynamic_outputs", None) is not None
            or getattr(terminal, "active_outputs", None) is not None
        )
        or _is_single_project_capability(terminal, _DYNAMIC_OUTPUT_PROJECT_CAPABILITIES)
    )


def compile_output_schema(output_model: Any) -> dict[str, Any]:
    """Compile the model-owned response schema using logical output keys."""

    return TypeAdapter(output_model).json_schema()


def _resolve_output_fields(
    output_fields: tuple[OutputField, ...],
    dynamic_outputs: Callable[[Any], Mapping[str, Any]] | None,
    params: BaseModel,
) -> tuple[OutputField, ...]:
    if dynamic_outputs is None:
        return output_fields
    declared = dynamic_outputs(params)
    if not isinstance(declared, Mapping) or not declared:
        raise TypeError("dynamic_outputs must return a non-empty mapping")
    fields: list[OutputField] = []
    for key, annotation in declared.items():
        if not isinstance(key, str) or not key or key != key.strip():
            raise TypeError(f"invalid dynamic output key {key!r}")
        try:
            if isinstance(annotation, OutputField):
                if annotation.key != key:
                    raise TypeError("dynamic output field must retain its declared key")
                if annotation.hidden or annotation.route is not None:
                    raise TypeError(
                        "dynamic output declarations cannot author hidden routes"
                    )
                TypeAdapter(annotation.annotation)
                fields.append(annotation)
                continue
            if isinstance(annotation, RoutedOutput):
                if annotation.route.name != key:
                    raise TypeError("routed output name must match its logical key")
                target = annotation.route.target
                fields.append(
                    OutputField(
                        key,
                        target.type if target.kind == "column" else "json",
                        annotation.schema,
                        JsonValue,
                        hidden=target.kind != "column",
                        route=annotation,
                    )
                )
                continue
            if _has_staged_file_annotation(annotation):
                raise TypeError("row outputs cannot publish staged files")
            fields.append(
                OutputField(
                    key,
                    _dynamic_column_type(annotation),
                    _value_schema(annotation),
                    annotation,
                )
            )
        except (TypeError, ValueError) as error:
            raise TypeError(
                f"invalid dynamic output type for {key!r}: {annotation!r}"
            ) from error
    return tuple(fields)


def _is_subclass(value: Any, parent: type[Any]) -> bool:
    return isinstance(value, type) and issubclass(value, parent)


def _result_model(annotation: Any) -> type[BaseModel]:
    if not _is_subclass(annotation, RowResult):
        raise TypeError("map_rows handler must return RowResult[OutputModel]")
    args = getattr(annotation, "__pydantic_generic_metadata__", {}).get("args", ())
    if len(args) != 1 or not _is_subclass(args[0], BaseModel):
        raise TypeError("RowResult must specify one Pydantic output model")
    return args[0]


def _batch_result_model(annotation: Any) -> type[BaseModel]:
    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)
    if origin not in (dict, MappingABC) or len(args) != 2 or args[0] is not int:
        raise TypeError(
            "map_batch handler must return dict[int, RowResult[OutputModel]]"
        )
    result_types = (args[1],)
    if typing.get_origin(args[1]) in (typing.Union, types.UnionType):
        result_types = tuple(
            item for item in typing.get_args(args[1]) if item is not RowError
        )
    if len(result_types) != 1:
        raise TypeError("batch values must be RowResult[Output] or RowError")
    return _result_model(result_types[0])


def _model_prompt_result_model(annotation: Any) -> type[BaseModel]:
    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)
    if (
        origin is not ModelPrompt
        or len(args) != 1
        or not _is_subclass(args[0], BaseModel)
    ):
        raise TypeError("model_rows renderer must return ModelPrompt[OutputModel]")
    return args[0]


ROUTED_CAPABILITIES = {
    Translator: "translate",
    Geocoder: "geocode",
    CensusDemographics: "census_demographics",
    DocumentConverter: CAPABILITY_TO_MARKDOWN,
    OcrReader: CAPABILITY_OCR,
    Transcriber: CAPABILITY_TRANSCRIBE,
}

_ENGINE_OPTIONS = {
    OcrReader: OcrOptions,
    Transcriber: TranscriptionOptions,
    Translator: TranslationOptions,
}


def resolved_engine_options(terminal, params: BaseModel) -> dict[str, Any] | None:
    """One semantic option projection for placement, execution and identity."""
    capability = routed_capability(terminal)
    if capability not in _ENGINE_OPTIONS:
        return None
    from frisket.execution.resolve_for_action import _default_engine

    engine = (
        getattr(params, terminal.engine_param).root
        if terminal.engine_param is not None
        else _default_engine(ROUTED_CAPABILITIES[capability])
    )
    return terminal.resolve_engine_options(params, engine)


def routed_capability(terminal: Any) -> type | None:
    return next(
        (
            cap
            for cap in getattr(terminal, "capabilities", ())
            if cap in ROUTED_CAPABILITIES
        ),
        None,
    )


def _resolved_callback_hints(
    callback: Callable[..., Any], *, name: str
) -> dict[str, Any]:
    try:
        return get_type_hints(callback)
    except Exception as error:
        raise TypeError(f"{name} annotations must resolve") from error


def _engine_parameter(
    params: type[BaseModel], capabilities: tuple[type, ...]
) -> str | None:
    routed = [cap for cap in capabilities if cap in ROUTED_CAPABILITIES]
    if len(routed) > 1:
        raise TypeError("one invocation may use only one routed capability")
    references = [
        (name, info.annotation)
        for name, info in params.model_fields.items()
        if _is_subclass(info.annotation, EngineRef)
    ]
    if len(references) > 1:
        raise TypeError("an injected capability has at most one EngineRef")
    for name, annotation in references:
        args = annotation.__pydantic_generic_metadata__.get("args", ())
        if len(args) != 1 or args[0] not in (
            *routed,
            *(
                cap
                for cap in capabilities
                if cap in (TopicSectionsReader, Classifier, NerExtractor)
            ),
        ):
            raise TypeError("EngineRef must name the injected routed capability")
        return name
    return None


def _handler_hints(
    handler: Callable[..., Any], *, kind: str, input_type: type[Any]
) -> tuple[type[BaseModel], Any, tuple[type, ...]]:
    parameters = tuple(inspect.signature(handler).parameters.values())
    if (
        len(parameters) < 2
        or (
            kind not in {"map_rows", "map_batch", "semantic_join"}
            and len(parameters) != 2
        )
        or any(
            param.kind not in (param.POSITIONAL_ONLY, param.POSITIONAL_OR_KEYWORD)
            for param in parameters
        )
    ):
        raise TypeError(
            f"{kind} handler must accept exactly (params, {input_type.__name__.lower()})"
        )
    hints = _resolved_callback_hints(handler, name=f"{kind} handler")
    params_model = hints.get(parameters[0].name)
    if not _is_subclass(params_model, ActionParams):
        raise TypeError(f"{kind} params must inherit ActionParams")
    if hints.get(parameters[1].name) is not input_type:
        raise TypeError(
            f"{kind} {input_type.__name__.lower()} parameter must be {input_type.__name__}"
        )
    injections = tuple(hints.get(parameter.name) for parameter in parameters[2:])
    capabilities = tuple(
        kind for kind in injections if kind not in (InvocationContext, PluginSecrets)
    )
    if InvocationContext in injections and kind not in {"map_rows", "map_batch"}:
        raise TypeError(f"{kind} does not support InvocationContext")
    if any(
        capability
        not in (
            MediaMetadataReader,
            VisualCutsReader,
            TopicSectionsReader,
            PythonEvaluator,
            HttpRequester,
            OpenCorporates,
            PdfTablesReader,
            FileFetcher,
            MediaDownloader,
            Screenshotter,
            FrameExtractor,
            FaceExtractor,
            Classifier,
            NerExtractor,
            Researcher,
            WebSearcher,
            McpExtractor,
            *ROUTED_CAPABILITIES,
            *((SemanticMatcher,) if kind == "semantic_join" else ()),
        )
        for capability in capabilities
    ):
        raise TypeError(f"{kind} supports only known SDK capability injection")
    if len(set(injections)) != len(injections):
        raise TypeError(f"{kind} injections must not be repeated")
    if kind == "map_rows" and CensusDemographics in capabilities:
        raise TypeError("CensusDemographics requires map_batch whole-scope binding")
    if kind == "map_batch" and any(
        cap
        in (
            Geocoder,
            DocumentConverter,
            OcrReader,
            Transcriber,
            VisualCutsReader,
            TopicSectionsReader,
            PythonEvaluator,
            HttpRequester,
            OpenCorporates,
            PdfTablesReader,
            FileFetcher,
            MediaDownloader,
            Screenshotter,
            FrameExtractor,
            FaceExtractor,
            WebSearcher,
        )
        for cap in capabilities
    ):
        raise TypeError("row-bound capabilities require map_rows")
    return params_model, hints.get("return"), injections


def _value_annotation(annotation: Any) -> Any:
    if _is_subclass(annotation, Outcome):
        return annotation.model_fields["value"].annotation
    return annotation


def _column_type(annotation: Any) -> str:
    choices = _without_none(_value_annotation(annotation))
    choices = tuple(
        typing.get_args(choice)[0]
        if typing.get_origin(choice) is typing.Annotated
        else choice
        for choice in choices
    )
    media_types = {
        OcrText: "text",
        TranscriptText: "timestamped_transcript",
        DetectedLanguage: "category",
    }
    if len(choices) == 1 and choices[0] in media_types:
        return media_types[choices[0]]
    if choices and all(choice is GeoPoint for choice in choices):
        return "geo_point"
    if choices and all(choice is TimelinePointsValue for choice in choices):
        return "timeline_points"
    if choices and all(choice is TimelineRangesValue for choice in choices):
        return "timeline_ranges"
    if choices and all(
        (
            typing.get_origin(choice) is typing.Literal
            and all(isinstance(value, str) for value in typing.get_args(choice))
        )
        or (
            _is_subclass(choice, Enum)
            and all(isinstance(value.value, str) for value in choice)
        )
        for choice in choices
    ):
        return "category"
    column_types = {_COLUMN_TYPE_BY_VALUE.get(choice) for choice in choices}
    if column_types == {"integer", "number"}:
        return "number"
    if len(column_types) == 1 and None not in column_types:
        return next(iter(column_types))
    raise TypeError(f"unsupported output field type {annotation!r}")


def _dynamic_column_type(annotation: Any) -> str:
    choices = _without_none(_value_annotation(annotation))
    if choices and all(
        choice is Any
        or choice in (dict, list, MappingABC)
        or typing.get_origin(choice) in (dict, list, MappingABC)
        or (
            _is_subclass(choice, BaseModel)
            and choice
            not in (
                GeoPoint,
                TimelinePointsValue,
                TimelineRangesValue,
                OcrText,
                TranscriptText,
                DetectedLanguage,
            )
        )
        for choice in choices
    ):
        return "json"
    return _column_type(annotation)


def _value_schema(annotation: Any) -> Mapping[str, Any]:
    choices = _without_none(_value_annotation(annotation))
    value_type = choices[0] if len(choices) == 1 else _value_annotation(annotation)
    if value_type is Any:
        return MappingProxyType({})
    return MappingProxyType(TypeAdapter(value_type).json_schema())


def _has_serialization_schema(value: Any) -> bool:
    if isinstance(value, dict):
        return "serialization" in value or any(
            _has_serialization_schema(item) for item in value.values()
        )
    if isinstance(value, (list, tuple)):
        return any(_has_serialization_schema(item) for item in value)
    return False


def _table_output_key(name: str, info: FieldInfo) -> str:
    validation_key = (
        info.validation_alias if info.validation_alias is not None else name
    )
    serialization_key = (
        info.serialization_alias if info.serialization_alias is not None else name
    )
    if not isinstance(validation_key, str) or validation_key != serialization_key:
        raise TypeError(
            "static table output aliases must agree for validation and serialization"
        )
    if not validation_key or validation_key != validation_key.strip():
        raise TypeError("static table output keys must be nonempty and trimmed")
    return validation_key


def _has_staged_file_annotation(
    annotation: Any, seen: frozenset[type] = frozenset()
) -> bool:
    """Plain JSON declarations cannot hide table-only publication handles."""
    if _is_subclass(annotation, StagedFile):
        return True
    if _is_subclass(annotation, BaseModel):
        if annotation in seen:
            return False
        return any(
            _has_staged_file_annotation(info.annotation, seen | {annotation})
            for info in annotation.model_fields.values()
        )
    return any(
        _has_staged_file_annotation(argument, seen)
        for argument in typing.get_args(annotation)
    )


def _validate_staged_row_shape(annotation: Any, seen=frozenset()) -> None:
    """Returned JSON must retain one unambiguous declared file-leaf shape."""
    if not _has_staged_file_annotation(annotation):
        return
    if _is_subclass(annotation, BaseModel):
        if annotation in seen:
            return
        keys = set()
        for name, info in annotation.model_fields.items():
            key = _table_output_key(name, info)
            if key in keys:
                raise TypeError("row file output aliases must be unique")
            keys.add(key)
            _validate_staged_row_shape(info.annotation, seen | {annotation})
        return
    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)
    if origin in (typing.Union, types.UnionType):
        if len([item for item in args if item is not type(None)]) > 1:
            raise TypeError("row file unions must have one non-null declared shape")
    for item in args:
        _validate_staged_row_shape(item, seen)


def _output_fields(
    model: type[BaseModel],
    *,
    allow_json: bool = False,
    table_fields: bool = False,
    allow_defaults: bool = False,
    row_files: bool = False,
) -> tuple[OutputField, ...]:
    if not model.model_fields:
        raise TypeError("map_rows output model must declare at least one field")
    fields: list[OutputField] = []
    seen: set[str] = set()
    for name, info in model.model_fields.items():
        key = _table_output_key(name, info) if table_fields else name
        if table_fields and key in seen:
            raise TypeError("static table output fields must have unique logical keys")
        seen.add(key)
        if not info.is_required() and not allow_defaults:
            raise TypeError(f"static output field {key!r} must be required")
        annotation = info.rebuild_annotation()
        if (
            not table_fields
            and not row_files
            and _has_staged_file_annotation(annotation)
        ):
            raise TypeError(f"unsupported output field type {annotation!r}")
        if (table_fields or row_files) and _without_none(
            _value_annotation(info.annotation)
        ) in ((StagedFile,), (StagedImage,), (StagedAudio,), (StagedVideo,)):
            column_type = (
                "image"
                if StagedImage in _without_none(_value_annotation(info.annotation))
                else "audio"
                if StagedAudio in _without_none(_value_annotation(info.annotation))
                else "video"
                if StagedVideo in _without_none(_value_annotation(info.annotation))
                else "file"
            )
        else:
            column_type = (
                _dynamic_column_type(info.annotation)
                if allow_json
                else _column_type(info.annotation)
            )
        column_format = None
        default_hidden = False
        named_result = None
        semantic_type = None
        if info.json_schema_extra is not None:
            if not isinstance(info.json_schema_extra, dict):
                raise TypeError("static output field metadata must be a mapping")
            column_format = info.json_schema_extra.get("format")
            semantic_type = info.json_schema_extra.get("semantic_type")
            if semantic_type is not None and (
                not isinstance(semantic_type, str)
                or not semantic_type.strip()
                or semantic_type != semantic_type.strip()
            ):
                raise TypeError("static output semantic_type must be nonempty text")
            if "named_result" in info.json_schema_extra:
                declaration = info.json_schema_extra["named_result"]
                if not isinstance(declaration, dict) or "kind" in declaration:
                    raise TypeError("named_result must declare schema and may_feed")
                named_result = NamedResultTarget.model_validate(
                    {"kind": "named_result", **declaration}
                )
                if table_fields or _value_schema(annotation).get("type") != "array":
                    raise TypeError("named_result requires a row output list field")
            default_hidden = info.json_schema_extra.get("default_hidden", False)
            if not isinstance(default_hidden, bool):
                raise TypeError("static output default_hidden must be a boolean")
            if column_format is not None and (
                not isinstance(column_format, str)
                or not column_format
                or column_format != column_format.strip()
            ):
                raise TypeError("static output column format must be nonempty text")
        fields.append(
            OutputField(
                key,
                column_type,
                _value_schema(annotation),
                annotation,
                column_format,
                default_hidden=default_hidden,
                named_result=named_result,
                semantic_type=semantic_type,
            )
        )
    return tuple(fields)


def _validate_params_callback(callback: Callable[..., Any] | None, name: str) -> None:
    if callback is not None:
        if not callable(callback):
            raise TypeError(f"{name} must be callable")
        if inspect.iscoroutinefunction(callback) or inspect.iscoroutinefunction(
            getattr(callback, "__call__", None)
        ):
            raise TypeError(f"{name} must be synchronous")
        parameters = tuple(inspect.signature(callback).parameters.values())
        if len(parameters) != 1 or parameters[0].kind not in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        ):
            raise TypeError(f"{name} must accept exactly (params)")


def map_rows(
    handler: Callable[..., Any],
    *,
    dynamic_outputs: Callable[[Any], Mapping[str, Any]] | None = None,
    active_outputs: Callable[[Any], typing.Iterable[str]] | None = None,
    engine_options: Callable[[Any], OcrOptions | TranscriptionOptions] | None = None,
) -> MapRows[Any, Any]:
    if dynamic_outputs is not None and active_outputs is not None:
        raise TypeError("dynamic_outputs and active_outputs are mutually exclusive")
    _validate_params_callback(active_outputs, "active_outputs")
    _validate_params_callback(engine_options, "engine_options")
    params_model, return_type, injections = _handler_hints(
        handler, kind="map_rows", input_type=Row
    )
    capabilities = tuple(
        kind for kind in injections if kind not in (InvocationContext, PluginSecrets)
    )
    if engine_options is not None and not any(
        cap in _ENGINE_OPTIONS for cap in capabilities
    ):
        raise TypeError("engine_options requires an options-bearing routed capability")
    output_model = _result_model(return_type)
    if active_outputs is not None and output_model is DynamicOutput:
        raise TypeError("active_outputs requires a statically declared output model")
    if dynamic_outputs is None:
        _validate_staged_row_shape(output_model)
        if _has_staged_file_annotation(output_model) and _has_serialization_schema(
            TypeAdapter(output_model).core_schema
        ):
            raise TypeError("row file outputs may not define serializers")
        output_fields = _output_fields(
            output_model,
            allow_json=True,
            allow_defaults=active_outputs is not None,
            row_files=True,
        )
    else:
        if output_model is not DynamicOutput:
            raise TypeError("dynamic_outputs requires RowResult[DynamicOutput]")
        output_fields = ()
    return MapRows(
        handler,
        params_model,
        output_model,
        output_fields,
        dynamic_outputs,
        active_outputs,
        injections,
        _engine_parameter(params_model, capabilities),
        engine_options,
    )


def semantic_join(handler: Callable[..., Any]) -> SemanticJoin[Any, Any]:
    """Match rows and atomically publish their outputs and linked child sheet."""

    params_model, return_type, capabilities = _handler_hints(
        handler, kind="semantic_join", input_type=Row
    )
    output_model = _result_model(return_type)
    if capabilities != (SemanticMatcher,) or output_model is not SemanticJoinMatch:
        raise TypeError(
            "semantic_join requires SemanticMatcher and RowResult[SemanticJoinMatch]"
        )
    return SemanticJoin(
        handler=handler,
        params_model=params_model,
        output_model=output_model,
        output_fields=_output_fields(output_model, allow_json=True),
        injections=capabilities,
    )


def map_batch(
    handler: Callable[..., Any],
    *,
    dynamic_outputs: Callable[[Any], Mapping[str, Any]] | None = None,
    active_outputs: Callable[[Any], typing.Iterable[str]] | None = None,
) -> MapBatch[Any, Any]:
    """Whole-scope rows with the same outputs and capabilities as map_rows."""
    _validate_params_callback(active_outputs, "active_outputs")
    params_model, return_type, injections = _handler_hints(
        handler, kind="map_batch", input_type=Rows
    )
    capabilities = tuple(
        kind for kind in injections if kind not in (InvocationContext, PluginSecrets)
    )
    if dynamic_outputs is not None and active_outputs is not None:
        raise TypeError("dynamic_outputs and active_outputs are mutually exclusive")
    output_model = _batch_result_model(return_type)
    if active_outputs is not None and output_model is DynamicOutput:
        raise TypeError("active_outputs requires a statically declared output model")
    if dynamic_outputs is None:
        output_fields = _output_fields(
            output_model, allow_json=True, allow_defaults=active_outputs is not None
        )
    else:
        if output_model is not DynamicOutput:
            raise TypeError("dynamic_outputs requires RowResult[DynamicOutput]")
        output_fields = ()
    return MapBatch(
        handler,
        params_model,
        output_model,
        output_fields,
        dynamic_outputs,
        active_outputs,
        injections,
        _engine_parameter(params_model, capabilities),
    )


def _validate_model_rows_source(value: Any) -> Any:
    """Keep the paid row source nonempty and unambiguous for every author."""

    if isinstance(value, list):
        names = [column.name for column in value]
        if not names:
            raise ValueError("source must contain at least one column")
        if len(names) != len(set(names)):
            raise ValueError("source columns must be unique")
    elif isinstance(value, Template) and not value.references():
        raise ValueError("source template must reference at least one column")
    return value


def model_rows(
    renderer: Callable[..., Any],
    *,
    source_param: str | None = None,
    evaluation: ModelRowsEvaluation | None = None,
    direct: Callable[..., Any] | None = None,
    complete: Callable[..., Any] | None = None,
    dynamic_outputs: Callable[[Any], Mapping[str, Any]] | None = None,
    active_outputs: Callable[[Any], typing.Iterable[str]] | None = None,
    engine_options: Callable[..., Any] | None = None,
) -> ModelRows[Any, Any]:
    """One inspectable model request, with optional pure completion and direct work.

    A non-null ModelRef selects the model request. Without one, the host runs
    ``direct`` as an ordinary typed row handler. Params own domain consistency
    rules; selection never depends on parameter names or engine labels.
    ``source_param`` selects the prompt source when Params also declare other
    typed references, such as grounding-only documents.
    """

    if inspect.iscoroutinefunction(renderer) or inspect.iscoroutinefunction(
        getattr(renderer, "__call__", None)
    ):
        raise TypeError("model_rows renderer must be synchronous")
    if evaluation is None:
        params_model, return_type, _ = _handler_hints(
            renderer, kind="model_rows", input_type=Row
        )
    else:
        parameters = tuple(inspect.signature(renderer).parameters.values())
        if len(parameters) != 3 or any(
            param.kind not in (param.POSITIONAL_ONLY, param.POSITIONAL_OR_KEYWORD)
            for param in parameters
        ):
            raise TypeError(
                "evaluation model_rows renderer must accept (params, row, context)"
            )
        try:
            hints = get_type_hints(renderer)
        except Exception as error:
            raise TypeError("model_rows renderer annotations must resolve") from error
        params_model = hints.get(parameters[0].name)
        if not _is_subclass(params_model, ActionParams):
            raise TypeError("model_rows params must inherit ActionParams")
        if hints.get(parameters[1].name) is not Row:
            raise TypeError("model_rows row parameter must be Row")
        if hints.get(parameters[2].name) is not ModelRowsEvaluationContext:
            raise TypeError(
                "evaluation model_rows context parameter must be ModelRowsEvaluationContext"
            )
        return_type = hints.get("return")
    response_model = _model_prompt_result_model(return_type)
    if response_model is not DynamicOutput:
        _validate_model_response(response_model)
    output_model = response_model
    if complete is not None:
        if inspect.iscoroutinefunction(complete):
            raise TypeError("model_rows completion must be synchronous and pure")
        parameters = tuple(inspect.signature(complete).parameters.values())
        try:
            hints = get_type_hints(complete)
        except Exception as error:
            raise TypeError("model_rows completion annotations must resolve") from error
        if (
            len(parameters) != 3
            or any(
                parameter.kind
                not in (parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD)
                for parameter in parameters
            )
            or tuple(hints.get(parameter.name) for parameter in parameters)
            != (params_model, Row, response_model)
        ):
            raise TypeError("model_rows completion must accept (params, row, response)")
        output_model = _result_model(hints.get("return"))
    if dynamic_outputs is not None and active_outputs is not None:
        raise TypeError("dynamic_outputs and active_outputs are mutually exclusive")
    _validate_params_callback(active_outputs, "active_outputs")
    if dynamic_outputs is not None:
        if output_model is not DynamicOutput:
            raise TypeError("dynamic_outputs requires DynamicOutput")
        output_fields = ()
    else:
        output_fields = _output_fields(
            output_model, allow_json=True, allow_defaults=active_outputs is not None
        )
    direct_terminal = None
    if direct is not None:
        direct_terminal = map_rows(
            direct,
            dynamic_outputs=dynamic_outputs,
            active_outputs=active_outputs,
            engine_options=engine_options,
        )
        if (
            direct_terminal.params_model is not params_model
            or direct_terminal.output_model is not output_model
        ):
            raise TypeError(
                "model_rows direct handler must share Params and final Output"
            )
    elif engine_options is not None:
        raise TypeError("model_rows engine_options requires a direct handler")
    model_fields = [
        name
        for name, info in params_model.model_fields.items()
        if any(_is_subclass(item, ModelRef) for item in _without_none(info.annotation))
    ]
    source_fields = [
        name
        for name, info in params_model.model_fields.items()
        if source_kind(info.annotation) is not None
        and not _is_subclass(info.annotation, GeneratedColumnRef)
    ]
    if len(model_fields) != 1:
        raise TypeError("model_rows Params must declare exactly one ModelRef")
    if source_param is None:
        if len(source_fields) != 1:
            raise TypeError("model_rows Params must declare exactly one typed source")
        source_param = source_fields[0]
    elif source_param not in source_fields:
        raise TypeError("model_rows source_param must name a typed source field")
    if evaluation is not None:
        fields = params_model.model_fields
        try:
            subject = fields[evaluation.subject_param]
            guidelines = fields[evaluation.guidelines_param]
            upstream_prompt = fields[evaluation.upstream_prompt_param]
        except KeyError as error:
            raise TypeError(
                "evaluation profile names an unknown Params field"
            ) from error
        if not _is_subclass(subject.annotation, GeneratedColumnRef):
            raise TypeError("evaluation subject must be a GeneratedColumnRef")
        if guidelines.annotation is not str:
            raise TypeError("evaluation guidelines must be a string")
        if upstream_prompt.annotation is not bool:
            raise TypeError("evaluation upstream-prompt opt-in must be a boolean")
        if tuple(output_model.model_fields) != ("verdict", "judge_note"):
            raise TypeError(
                "evaluation output must declare exactly verdict and judge_note"
            )
        if output_model.model_fields["verdict"].annotation is not bool:
            raise TypeError("evaluation verdict must be boolean")
        if output_model.model_fields["judge_note"].annotation is not str:
            raise TypeError("evaluation judge_note must be a string")
    return ModelRows(
        renderer,
        params_model,
        output_model,
        output_fields,
        model_fields[0],
        source_param,
        evaluation,
        response_model,
        complete,
        direct_terminal,
        dynamic_outputs,
        active_outputs,
    )


def _validate_model_response(output_model: type[BaseModel]) -> None:
    """Provider responses must express their validation in their JSON schema."""
    if output_model.__pydantic_root_model__:
        raise TypeError("model_rows output must be an object model")
    outcome_fields = [
        name
        for name, info in output_model.model_fields.items()
        if any(_is_subclass(item, Outcome) for item in _without_none(info.annotation))
    ]
    if outcome_fields:
        raise TypeError("model_rows output fields may not use Outcome")
    aliased = [
        name
        for name, info in output_model.model_fields.items()
        if info.alias is not None
        or info.validation_alias is not None
        or info.serialization_alias is not None
    ]
    if aliased:
        raise TypeError("model_rows output fields may not declare aliases")
    if output_model.model_config:
        raise TypeError("model_rows output model configuration is unsupported")
    if output_model.__pydantic_custom_init__ or output_model.__pydantic_post_init__:
        raise TypeError("model_rows output initialization hooks are unsupported")
    if any(
        _has_serialization_schema(TypeAdapter(info.rebuild_annotation()).core_schema)
        for info in output_model.model_fields.values()
    ):
        raise TypeError("model_rows output serializers are unsupported")
    decorators = output_model.__pydantic_decorators__
    if (
        decorators.validators
        or decorators.field_validators
        or decorators.root_validators
        or decorators.model_validators
    ):
        raise TypeError(
            "model_rows output validators are unsupported; use schema-expressible fields"
        )
    if (
        decorators.field_serializers
        or decorators.model_serializers
        or decorators.computed_fields
    ):
        raise TypeError(
            "model_rows output serializers and computed fields are unsupported"
        )


def column_transform(
    handler: Callable[..., Any],
    *,
    output_type: Callable[..., str] | None = None,
    preflight: Callable[..., None] | None = None,
) -> ColumnTransform[Any, Any]:
    """Declare a local full-column transform with atomic publication."""

    parameters = tuple(inspect.signature(handler).parameters.values())
    if len(parameters) not in {2, 3} or any(
        param.kind not in (param.POSITIONAL_ONLY, param.POSITIONAL_OR_KEYWORD)
        for param in parameters
    ):
        raise TypeError(
            "column_transform handler must accept (params, rows[, context])"
        )
    hints = _resolved_callback_hints(handler, name="column_transform handler")
    params_model = hints.get(parameters[0].name)
    if not _is_subclass(params_model, ActionParams):
        raise TypeError("column_transform params must inherit ActionParams")
    if hints.get(parameters[1].name) is not Rows:
        raise TypeError("column_transform rows parameter must be Rows")
    takes_context = len(parameters) == 3
    if takes_context and hints.get(parameters[2].name) is not ColumnTransformContext:
        raise TypeError(
            "column_transform context parameter must be ColumnTransformContext"
        )
    output_model = _batch_result_model(hints.get("return"))
    controls, requirements = _semantic_metadata(params_model)
    if len(requirements) != 1 or next(iter(controls.values()), None) != "column":
        raise TypeError("column_transform Params must declare exactly one ColumnRef")
    if len(output_model.model_fields) != 1:
        raise TypeError("column_transform output model must declare exactly one field")
    key, info = next(iter(output_model.model_fields.items()))
    if not info.is_required():
        raise TypeError("column_transform output field must be required")
    if output_type is None:
        field = OutputField(
            key,
            _column_type(info.annotation),
            _value_schema(info.annotation),
            info.annotation,
        )
    else:
        callback_parameters = tuple(inspect.signature(output_type).parameters.values())
        if len(callback_parameters) != 3:
            raise TypeError(
                "column_transform output_type must accept (params, context, results)"
            )
        field = OutputField(
            key,
            # The key is statically authoritative, while the runtime derives
            # the concrete registered type from this invocation's source and
            # values. Catalog consumers treat this marker as descriptive only.
            "runtime",
            _value_schema(info.annotation),
            info.annotation,
        )
    if preflight is not None:
        callback_parameters = tuple(inspect.signature(preflight).parameters.values())
        if len(callback_parameters) != 2:
            raise TypeError("column_transform preflight must accept (params, context)")
    return ColumnTransform(
        handler,
        params_model,
        output_model,
        (field,),
        output_type,
        preflight,
        takes_context,
    )


def _publication_return_schema(schema: Any) -> Any:
    """Derive strict publication behavior from the author's one type declaration."""
    if isinstance(schema, dict):
        result = {
            key: value
            if key in {"metadata", "default", "config", "serialization"}
            else _publication_return_schema(value)
            for key, value in schema.items()
        }
        if schema.get("type") in ("model", "dataclass") and isinstance(
            schema.get("cls"), type
        ):
            result["revalidate_instances"] = "always"
            # Keep non-finite floats observable until the strict JSON check,
            # including inferred Any fields; Pydantic otherwise substitutes null.
            result["config"] = {
                **schema.get("config", {}),
                "ser_json_inf_nan": "constants",
            }
        return result
    if isinstance(schema, (list, tuple)):
        return type(schema)(_publication_return_schema(value) for value in schema)
    return schema


def _project_action(handler: Callable[..., Any]) -> _ProjectAction[Any, Any]:
    if inspect.iscoroutinefunction(handler) or inspect.iscoroutinefunction(
        getattr(handler, "__call__", None)
    ):
        raise TypeError("project action handler must be synchronous")
    parameters = tuple(inspect.signature(handler).parameters.values())
    if not parameters or any(
        param.kind not in (param.POSITIONAL_ONLY, param.POSITIONAL_OR_KEYWORD)
        for param in parameters
    ):
        raise TypeError(
            "project action handler accepts params followed by admitted injections"
        )
    hints = _resolved_callback_hints(handler, name="project action handler")
    params_model = hints.get(parameters[0].name)
    if not _is_subclass(params_model, ActionParams):
        raise TypeError("project action params must inherit ActionParams")
    injections = tuple(hints.get(parameter.name) for parameter in parameters[1:])
    if any(
        kind not in (InvocationContext, PluginSecrets)
        and kind not in _PROJECT_CAPABILITY_SPECS
        for kind in injections
    ):
        raise TypeError(
            "project action injections must be admitted capabilities, "
            "InvocationContext, or PluginSecrets"
        )
    if injections.count(InvocationContext) > 1:
        raise TypeError("project action accepts at most one InvocationContext")
    if injections.count(PluginSecrets) > 1:
        raise TypeError("project action accepts at most one PluginSecrets")
    if "return" not in hints:
        raise TypeError("project action handler must declare its return type")
    return_type = hints["return"]
    capabilities = tuple(
        kind for kind in injections if kind not in (InvocationContext, PluginSecrets)
    )
    specs = tuple(_PROJECT_CAPABILITY_SPECS[cap] for cap in capabilities)
    ordinary = all(spec.callable_host for spec in specs)
    if not ordinary:
        if len(specs) != 1 or any(
            kind in injections for kind in (InvocationContext, PluginSecrets)
        ):
            raise TypeError(
                "these project capabilities do not support composed callable execution yet"
            )
        if return_type is not specs[0].return_type:
            raise TypeError(
                "specialized project capability requires its declared preparation/result return"
            )
    adapter = TypeAdapter(return_type)
    # Refuse unsupported return schemas at registration, never after effects.
    if ordinary:
        adapter.json_schema(mode="serialization", by_alias=True)
    fields = (
        (OutputField("canonical", "text", {"type": "string"}, str),)
        if capabilities == (ValueClusterer,)
        else CreateSheet.fields_from_columns(GROUP_SUMMARY_COLUMNS)
        if capabilities == (GroupSummarizer,)
        else ()
    )
    publication_schema = _publication_return_schema(adapter.core_schema)
    return _ProjectAction(
        handler,
        params_model,
        injections,
        return_type,
        adapter,
        # Otherwise nested schemas can silently reuse their class validator,
        # whose instance-trusting policy this publication boundary overrides.
        SchemaValidator(publication_schema, _use_prebuilt=False),
        SchemaSerializer(
            publication_schema, {"ser_json_inf_nan": "constants"}, _use_prebuilt=False
        ),
        output_fields=fields,
    )


def create_sheet(
    handler: Callable[..., Any],
    *,
    columns_from: Callable[[Any], typing.Iterable[TableColumn] | None] | None = None,
) -> CreateSheet[Any, Any]:
    """Bind a typed table producer to the host's atomic sheet materializer."""

    parameters = tuple(inspect.signature(handler).parameters.values())
    if not parameters or any(
        parameter.kind
        not in (parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD)
        for parameter in parameters
    ):
        raise TypeError(
            "create_sheet handler must accept params and known read capabilities"
        )
    hints = _resolved_callback_hints(handler, name="create_sheet handler")
    params_model = hints.get(parameters[0].name)
    if not _is_subclass(params_model, ActionParams):
        raise TypeError("create_sheet params must inherit ActionParams")
    injections = tuple(hints.get(parameter.name) for parameter in parameters[1:])
    capabilities = tuple(kind for kind in injections if kind is not PluginSecrets)
    if any(
        capability
        not in {
            EmbeddingIndexReader,
            EmailSourceReader,
            RuntimeImporter,
            UrlImporter,
            LocalFileReader,
            ListTableReader,
            SheetRowsReader,
            CollectionReader,
            SemanticMatchReader,
            JoinedTablesReader,
            TranscriptReader,
            TemporalMediaReader,
            ClusterReceiptReader,
            ImportBlobStager,
            PdfPageRenderer,
        }
        for capability in capabilities
    ):
        raise TypeError(
            "create_sheet supports only EmbeddingIndexReader, LocalFileReader, "
            "ListTableReader, SheetRowsReader, CollectionReader, SemanticMatchReader, JoinedTablesReader, TranscriptReader, ClusterReceiptReader, EmailSourceReader, RuntimeImporter, UrlImporter, ImportBlobStager and PdfPageRenderer injection"
        )
    if len(injections) != len(set(injections)):
        raise TypeError("create_sheet injections must not be repeated")
    return_type = hints.get("return")
    alternatives = (
        typing.get_args(return_type)
        if typing.get_origin(return_type) in (typing.Union, types.UnionType)
        else (return_type,)
    )
    runtime_schema = DynamicTableResult in alternatives
    known = tuple(item for item in alternatives if item is not DynamicTableResult)
    if runtime_schema and not known:
        if columns_from is not None:
            raise TypeError(
                "runtime-only tables discover their schema; omit columns_from"
            )
        return CreateSheet(
            handler, params_model, DynamicOutput, None, (), injections, True
        )
    if len(known) != 1 or typing.get_origin(known[0]) is not TableResult:
        raise TypeError("create_sheet handler must return TableResult[OutputModel]")
    [output_model] = typing.get_args(known[0])
    if not _is_subclass(output_model, BaseModel):
        raise TypeError("TableResult must specify one Pydantic output model")
    if output_model is DynamicOutput:
        if columns_from is None:
            raise TypeError("TableResult[DynamicOutput] requires columns_from")
        columns_parameters = tuple(inspect.signature(columns_from).parameters.values())
        if len(columns_parameters) != 1:
            raise TypeError("create_sheet columns_from must accept exactly (params)")
        output_fields = ()
    else:
        if runtime_schema:
            raise TypeError(
                "runtime table alternatives require TableResult[DynamicOutput]"
            )
        if columns_from is not None:
            raise TypeError(
                "static TableResult schema comes from its output model; omit columns_from"
            )
        output_fields = _output_fields(output_model, allow_json=True, table_fields=True)
    return CreateSheet(
        handler,
        params_model,
        output_model,
        columns_from,
        output_fields,
        injections,
        runtime_schema,
    )


@dataclass(frozen=True)
class Action(Generic[P, OutputT]):
    name: str
    title: str
    description: str
    category: ActionCategory
    run: (
        MapRows[P, OutputT]
        | MapBatch[P, OutputT]
        | ModelRows[P, OutputT]
        | ColumnTransform[P, OutputT]
        | CreateSheet[P, OutputT]
        | GoogleSheetsExport[P]
        | _ProjectAction[P, OutputT]
    )
    row_scope: RowScope
    export_target: ExportTarget | None
    form: str = "generated"
    _example_params: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (
            not isinstance(self.form, str)
            or not self.form
            or self.form != self.form.strip()
        ):
            raise ValueError("action form must be a non-empty trimmed string")


def action(
    *,
    name: str,
    title: str,
    description: str,
    category: ActionCategory,
    run: (
        MapRows[P, OutputT]
        | MapBatch[P, OutputT]
        | ModelRows[P, OutputT]
        | ColumnTransform[P, OutputT]
        | CreateSheet[P, OutputT]
        | GoogleSheetsExport[P]
        | Callable[..., OutputT]
    ),
    row_scope: RowScope = RowScope.SELECTABLE_ROWS,
    export_target: ExportTarget | None = None,
    form: str = "generated",
    examples: typing.Iterable[P] = (),
) -> Action[P, OutputT]:
    """Declare an action, its presentation, and illustrative Params instances.

    Registration validates examples through the normal request binder, with
    project scope or all rows of sheet 1, default output names, and an example
    sheet name for table creation. These static catalog requests are not
    presets or authorization: callers must supply their own project inputs,
    fresh idempotency key, and any required confirmation before execution.
    """
    if not _LOCAL_NAME.fullmatch(name):
        raise ValueError(f"invalid action name {name!r}")
    if not title.strip() or not description.strip():
        raise ValueError("action title and description must be non-empty")
    if not isinstance(category, ActionCategory):
        raise TypeError("action category must be an ActionCategory")
    if not isinstance(row_scope, RowScope):
        raise TypeError("action row_scope must be a RowScope")
    if callable(run) and not isinstance(
        run,
        (
            MapRows,
            MapBatch,
            ModelRows,
            ColumnTransform,
            CreateSheet,
            _ProjectAction,
            GoogleSheetsExport,
        ),
    ):
        run = _project_action(run)
    if (
        isinstance(run, (_ProjectAction, GoogleSheetsExport, CreateSheet))
        and row_scope is not RowScope.SELECTABLE_ROWS
        and not (
            isinstance(run, _ProjectAction)
            and run.capabilities in ((ValueClusterer,), (RowsAppender,), (RowsUpdater,))
        )
    ):
        raise TypeError("project actions do not accept row scope controls")
    if isinstance(run, _ProjectAction) and run.capabilities == (ValueClusterer,):
        row_scope = RowScope.ALL_ROWS
    if export_target is not None and not isinstance(
        run, (_ProjectAction, GoogleSheetsExport)
    ):
        raise TypeError("export targets require project actions")
    if isinstance(run, ColumnTransform) and row_scope is not RowScope.ALL_ROWS:
        raise TypeError("column_transform actions must use RowScope.ALL_ROWS")
    params = run.params_model
    reserved_fields = (
        _PROJECT_REQUEST_FIELDS
        if isinstance(run, (_ProjectAction, GoogleSheetsExport, CreateSheet))
        else _REQUEST_FIELDS
    )
    reserved = reserved_fields.intersection(params.model_fields)
    if reserved:
        raise TypeError(
            f"Params declares request-owned fields: {', '.join(sorted(reserved))}"
        )
    _semantic_metadata(params)
    example_params = []
    for example in examples:
        if type(example) is not params:
            raise TypeError(f"action examples must be {params.__name__} instances")
        # Preserve omitted-versus-explicit fields (notably patches and review
        # decisions), and own immutable snapshots rather than mutable models.
        example_params.append(
            json.dumps(
                example.model_dump(mode="json", exclude_unset=True, by_alias=True),
                allow_nan=False,
            )
        )
    return Action(
        name,
        title.strip(),
        description.strip(),
        category,
        run,
        row_scope,
        export_target,
        form=form,
        _example_params=tuple(example_params),
    )


def _request_scope_types(
    terminal: Any,
) -> tuple[type[ProjectScope] | type[SheetRows], ...]:
    """One private scope contract for binding, examples, and catalog projection."""

    if isinstance(terminal, _ProjectAction):
        return (
            (SheetRows,)
            if _is_single_project_capability(terminal, _ROW_SCOPED_PROJECT_CAPABILITIES)
            else (ProjectScope,)
        )
    if isinstance(terminal, GoogleSheetsExport):
        return (ProjectScope,)
    if isinstance(terminal, CreateSheet):
        # A second reader may narrow admission, never broaden the first reader's
        # scope. Ordinary table producers without a scoped reader are project-level.
        requirements = [
            scopes
            for capability, scopes in (
                (TranscriptReader, (SheetRows,)),
                (TemporalMediaReader, (SheetRows,)),
                (JoinedTablesReader, (SheetRows,)),
                (SheetRowsReader, (SheetRows,)),
                (SemanticMatchReader, (ProjectScope, SheetRows)),
            )
            if capability in terminal.capabilities
        ]
        if not requirements:
            return (ProjectScope,)
        return tuple(
            scope
            for scope in (ProjectScope, SheetRows)
            if all(scope in allowed for allowed in requirements)
        )
    return (SheetRows,)


@dataclass(frozen=True)
class RegisteredAction:
    action_id: str
    definition: Action[Any, Any]
    _example_requests: tuple[str, ...] = field(init=False, repr=False)

    def for_execution(self, params: BaseModel) -> RegisteredAction:
        terminal = self.definition.run
        if not isinstance(terminal, ModelRows) or terminal.uses_model(params):
            return self
        direct = terminal.direct
        assert direct is not None
        selected = _DirectModelRows(**vars(direct), source_param=terminal.source_param)
        return replace(self, definition=replace(self.definition, run=selected))

    def __post_init__(self) -> None:
        terminal = self.definition.run
        examples = []
        for index, params_json in enumerate(self.definition._example_params, start=1):
            request = ActionRequest(
                action_id=self.action_id,
                scope=(
                    ProjectScope()
                    if ProjectScope in _request_scope_types(terminal)
                    else SheetRows(
                        sheet_id=1,
                        row_ids=(1,)
                        if (
                            isinstance(terminal, CreateSheet)
                            and SemanticMatchReader in terminal.capabilities
                        )
                        or (
                            isinstance(terminal, _ProjectAction)
                            and terminal.capabilities == (EnclosureMaterializer,)
                        )
                        else None,
                    )
                ),
                params=json.loads(params_json),
                sheet_name="Example results" if _creates_sheet(terminal) else None,
                idempotency_key=f"example:{self.action_id}:{index}",
            )
            self.bind_request(request)
            examples.append(request.model_dump_json(exclude_none=True))
        object.__setattr__(self, "_example_requests", tuple(examples))

    def validate_output_names(
        self,
        output_fields: tuple[OutputField, ...] | None,
        output_names: Mapping[str, str],
        *,
        require_all_output_names: bool = False,
    ) -> None:
        """Validate known names, or just explicit renames before discovery."""

        ActionRequest._valid_output_names(dict(output_names))
        if output_fields is None:
            return
        # Table schema visibility is presentation, unlike row-host sidecars.
        table = isinstance(self.definition.run, CreateSheet)
        output_keys = {
            field.key for field in output_fields if table or not field.hidden
        }
        supplied_keys = output_names.keys()
        unknown = supplied_keys - output_keys
        if unknown:
            raise ValueError(f"unknown output names: {', '.join(sorted(unknown))}")
        if require_all_output_names and supplied_keys != output_keys:
            raise ValueError("output names must cover every logical output")
        final_names = [
            output_names.get(field.key, field.key)
            if table
            else field.materialized_name(output_names)
            for field in output_fields
        ]
        if len(final_names) != len(set(final_names)):
            raise ValueError("final output names must be unique")

    def bind_values(
        self,
        *,
        scope: Any,
        params: Mapping[str, Any],
        output_names: Mapping[str, str],
        require_all_output_names: bool = False,
    ) -> tuple[BaseModel, tuple[OutputField, ...] | None]:
        terminal = self.definition.run
        project_action = isinstance(
            terminal, (_ProjectAction, GoogleSheetsExport, CreateSheet)
        )
        scope_types = _request_scope_types(terminal)
        if not isinstance(scope, scope_types):
            allowed = " or ".join(
                model.model_fields["kind"].default for model in scope_types
            )
            raise ValueError(f"action requires {allowed} scope")
        if (
            isinstance(terminal, _ProjectAction)
            and terminal.capabilities == (EnclosureMaterializer,)
            and (scope.row_ids is None or not 1 <= len(scope.row_ids) <= 500)
        ):
            raise ValueError(
                "enclosure materialization requires 1 to 500 selected rows"
            )
        if (
            isinstance(terminal, CreateSheet)
            and SemanticMatchReader in terminal.capabilities
            and isinstance(scope, SheetRows)
            and scope.row_ids is None
        ):
            raise ValueError("semantic match tables require an explicit row selection")
        if (
            (
                not project_action
                or isinstance(terminal, _ProjectAction)
                and terminal.capabilities == (ValueClusterer,)
            )
            and self.definition.row_scope is RowScope.ALL_ROWS
            and scope.row_ids is not None
        ):
            raise ValueError("action requires all rows")
        validated, output_fields = self.bind_params(
            params=params,
            output_names=output_names,
            require_all_output_names=require_all_output_names,
        )
        if (
            (
                isinstance(terminal, CreateSheet)
                and any(
                    c in terminal.capabilities
                    for c in (TranscriptReader, TemporalMediaReader)
                )
            )
            or isinstance(terminal, _ProjectAction)
            and terminal.capabilities == (TemporalExtractor,)
        ):
            validate_transcript_selection_scope(validated, scope)
        return validated, output_fields

    def bind_params(
        self,
        *,
        params: Mapping[str, Any],
        output_names: Mapping[str, str],
        require_all_output_names: bool = False,
    ) -> tuple[BaseModel, tuple[OutputField, ...] | None]:
        """Bind authored Params and output names without an execution scope.

        Portable artifacts and scoped invocations share these semantic checks;
        project selection and invocation-envelope policy remain in bind_values
        and bind_request respectively.
        """

        terminal = self.definition.run
        if output_names and not _accepts_output_names(terminal):
            raise ValueError("project actions do not accept output_names")
        validated = terminal.params_model.model_validate(params)
        if isinstance(self.definition.run, ModelRows):
            self.definition.run.validate_source(validated)
        output_fields = self.definition.run.resolve_output_fields(validated)
        if isinstance(terminal, SemanticJoin):
            # The child has additional columns, not additional source outputs.
            # Validate the shared rename envelope against the entire child shape.
            keys = {
                *(field.key for field in output_fields),
                *terminal.child_output_keys(validated),
            }
            unknown = output_names.keys() - keys
            if unknown:
                raise ValueError(f"unknown output names: {', '.join(sorted(unknown))}")
            names = [output_names.get(key, key) for key in keys]
            if len(set(names)) != len(names):
                raise ValueError("final output names must be unique")
            ActionRequest._valid_output_names(dict(output_names))
            if require_all_output_names and output_names.keys() != keys:
                raise ValueError("output names must cover every logical output")
            return validated, output_fields
        self.validate_output_names(
            output_fields,
            output_names,
            require_all_output_names=require_all_output_names,
        )
        if output_fields is None:
            # Discovery will validate keys and collisions with unnamed columns.
            # Basic spelling and explicit duplicate targets are already decidable.
            return validated, None
        output_keys = {field.key for field in output_fields}
        final_names = [output_names.get(key, key) for key in output_keys]
        if isinstance(self.definition.run, ModelRows):
            if self.definition.run.evaluation is not None:
                input_names = {
                    reference.column for reference in discover_references(validated)
                }
                overlap = input_names.intersection(final_names)
                if overlap:
                    raise ValueError(
                        "output names cannot overlap input columns: "
                        + ", ".join(sorted(overlap))
                    )
        return validated, output_fields

    def bind_request(
        self, request: ActionRequest
    ) -> tuple[BaseModel, tuple[OutputField, ...] | None]:
        if request.action_id != self.action_id:
            raise ValueError("request action_id does not match registered action")
        create_sheet_action = _creates_sheet(self.definition.run)
        if create_sheet_action and request.sheet_name is None:
            raise ValueError("create_sheet actions require sheet_name")
        if (
            not _accepts_sheet_name(self.definition.run)
            and request.sheet_name is not None
        ):
            raise ValueError("sheet_name is accepted only by create_sheet actions")
        if request.replace_existing and isinstance(
            self.definition.run,
            (ColumnTransform, CreateSheet, _ProjectAction, GoogleSheetsExport),
        ):
            raise ValueError(
                f"{type(self.definition.run).__name__} does not support replace_existing"
            )
        return self.bind_values(
            scope=request.scope,
            params=request.params,
            output_names=request.output_names,
        )

    def validate_request(self, request: ActionRequest) -> BaseModel:
        return self.bind_request(request)[0]

    def catalog_entry(self) -> dict[str, Any]:
        definition = self.definition
        model_backed = isinstance(definition.run, ModelRows)
        agent_backed = any(
            capability in getattr(definition.run, "capabilities", ())
            for capability in (Researcher, McpExtractor)
        )
        semantic = isinstance(definition.run, SemanticJoin)
        external = WebSearcher in getattr(definition.run, "capabilities", ())
        http = HttpRequester in getattr(definition.run, "capabilities", ())
        opencorporates = OpenCorporates in getattr(definition.run, "capabilities", ())
        fetch = any(
            cap in getattr(definition.run, "capabilities", ())
            for cap in (FileFetcher, MediaDownloader)
        )
        screenshot = Screenshotter in getattr(definition.run, "capabilities", ())
        routed = routed_capability(definition.run)
        reads_media_metadata = (
            isinstance(definition.run, MapRows)
            and MediaMetadataReader in definition.run.capabilities
        )
        project_action = ProjectScope in _request_scope_types(definition.run)
        create_sheet_action = isinstance(definition.run, CreateSheet)
        project_specs = (
            definition.run.specs
            if isinstance(definition.run, _ProjectAction)
            else (definition.run.spec,)
            if isinstance(definition.run, GoogleSheetsExport)
            else ()
        )
        project_errors = tuple(
            dict(pair for spec in project_specs for pair in spec.errors).items()
        )
        project_effects = tuple(
            dict.fromkeys(
                effect for spec in project_specs for effect in spec.side_effects
            )
        )
        project_requirements = tuple(
            dict.fromkeys(
                cap for spec in project_specs for cap in spec.required_capabilities
            )
        )
        if isinstance(definition.run, _ProjectAction) and not project_specs:
            project_errors = _project_errors(
                *_CALLABLE_ERRORS,
                write_failure="The callable receipt could not be written.",
            )
            project_effects = ("write_receipt",)
            project_requirements = ("project:read",)
        project_costs = {spec.cost_kind for spec in project_specs} - {"none"}
        project_cost = (
            next(iter(project_costs))
            if len(project_costs) == 1
            else "unknown"
            if project_costs
            else "none"
        )
        form = definition.form
        controls, source_requirements = _semantic_metadata(definition.run.params_model)
        input_schema = definition.run.params_model.model_json_schema()
        outputs = [
            {"key": field.key, "column_type": field.column_type}
            for field in definition.run.output_fields
        ]
        local_topic_hints = {}
        if TopicSectionsReader in getattr(definition.run, "capabilities", ()):
            from frisket.features.topic_segmentation.engines import (
                static_engine_catalog,
            )

            local_topic_hints = {"engines": list(static_engine_catalog())}
        entry = {
            "kind": self.action_id,
            "examples": [json.loads(value) for value in self._example_requests],
            "title": definition.title,
            "description": definition.description,
            "input_schema": input_schema,
            "output_schema": (
                definition.run.return_adapter.json_schema(
                    mode="serialization", by_alias=True
                )
                if isinstance(definition.run, _ProjectAction)
                and definition.run.callable_host
                else compile_output_schema(definition.run.output_model)
            ),
            "required_credentials": ["CENSUS_API_KEY"]
            if routed is CensusDemographics
            else ["OPENCORPORATES_API_TOKEN"]
            if opencorporates
            else [],
            "errors": [
                {"code": code, "message": message}
                for code, message in (
                    (
                        *_CREATE_SHEET_ERRORS,
                        *(
                            (
                                (
                                    "join_fanout_requires_confirmation",
                                    "The join exceeds the output row limit; echo the current confirmation token.",
                                ),
                            )
                            if JoinedTablesReader in definition.run.capabilities
                            else ()
                        ),
                        *(
                            (
                                (
                                    "literal_selection_requires_confirmation",
                                    "Repeating one literal selection across rows requires acknowledgement.",
                                ),
                                (
                                    "timestamps_required",
                                    "No effective internal timestamps remain.",
                                ),
                                (
                                    "timeline_mismatch",
                                    "The selection belongs to another timeline.",
                                ),
                                (
                                    "selection_unmappable",
                                    "The selection cannot map to the source timeline.",
                                ),
                                (
                                    "invalid_range",
                                    "The selected range is empty or reversed.",
                                ),
                                (
                                    "stale_input",
                                    "Transcript inputs changed before publication.",
                                ),
                                *_RUNNING_RESERVATION_ERRORS,
                            )
                            if any(
                                c in definition.run.capabilities
                                for c in (TranscriptReader, TemporalMediaReader)
                            )
                            else ()
                        ),
                        *(
                            (
                                (
                                    "embedding_index_not_found",
                                    "The embedding index does not exist.",
                                ),
                                (
                                    "embedding_export_sidecar_missing",
                                    "Refresh the index to create its vector store.",
                                ),
                                (
                                    "embedding_export_no_ready_vectors",
                                    "The index has no ready vectors.",
                                ),
                                (
                                    "embedding_source_stale",
                                    "Refresh the changed source rows.",
                                ),
                                (
                                    "embedding_index_incomplete",
                                    "Refresh the incomplete index.",
                                ),
                                (
                                    "embedding_index_scope_invalid",
                                    "The index source scope is invalid.",
                                ),
                                (
                                    "embedding_space_mismatch",
                                    "The vectors do not match their space.",
                                ),
                                (
                                    "embedding_analysis_insufficient_rows",
                                    "More ready vectors are required.",
                                ),
                            )
                            if EmbeddingIndexReader in definition.run.capabilities
                            else ()
                        ),
                        *(
                            (
                                (
                                    "invalid_file_source",
                                    "Source must be a readable local file.",
                                ),
                            )
                            if LocalFileReader in definition.run.capabilities
                            else ()
                        ),
                        *(
                            (
                                (
                                    "collection_expand_requires_confirmation",
                                    "Collection count exceeds the default cap; echo the confirmation token.",
                                ),
                                (
                                    "collection_expand_exceeds_hard_cap",
                                    "Collection count exceeds the hard cap.",
                                ),
                                (
                                    "provider_error",
                                    "External collection enumeration failed.",
                                ),
                            )
                            if CollectionReader in definition.run.capabilities
                            else ()
                        ),
                        *(
                            (
                                (
                                    "email_sources_unavailable",
                                    "Trusted ingress could not resolve the email source references.",
                                ),
                            )
                            if EmailSourceReader in definition.run.capabilities
                            else ()
                        ),
                        *(
                            (
                                (
                                    "unsupported_runtime_importer",
                                    "The project has no admitted importer binding.",
                                ),
                                (
                                    "runtime_importer_handler_failed",
                                    "The trusted importer handler failed.",
                                ),
                                (
                                    "invalid_runtime_importer_plan",
                                    "The importer returned an invalid table plan.",
                                ),
                            )
                            if RuntimeImporter in definition.run.capabilities
                            else ()
                        ),
                        *(
                            (
                                (
                                    "url_limit_exceeded",
                                    "URL count exceeds the deployment limit.",
                                ),
                            )
                            if UrlImporter in definition.run.capabilities
                            else ()
                        ),
                    )
                    if create_sheet_action
                    else project_errors
                    if isinstance(definition.run, (_ProjectAction, GoogleSheetsExport))
                    else _COLUMN_TRANSFORM_ERRORS
                    if isinstance(definition.run, ColumnTransform)
                    else tuple(
                        item
                        for item in _MODEL_ROWS_ERRORS
                        if getattr(definition.run, "evaluation", None) is not None
                        or item[0] != "column_not_ai_generated"
                    )
                    if model_backed or agent_backed
                    else _EXTERNAL_ROWS_ERRORS
                    if external or routed is not None or http or fetch or opencorporates
                    else (
                        *_MAP_ROWS_ERRORS,
                        ("invalid_pdf_cell", "The source must be a blob-backed PDF."),
                        (
                            "invalid_pdf_table_cells",
                            "The extracted table cells are invalid.",
                        ),
                        (
                            "pdf_table_adapter_unavailable",
                            "The Natural PDF adapter is unavailable.",
                        ),
                        (
                            "pdf_table_extract_failed",
                            "No usable PDF table rows were extracted.",
                        ),
                        (
                            "pdf_table_shape_mismatch",
                            "Extracted table headers differ across sources.",
                        ),
                        (
                            "unsupported_pdf_table_mode",
                            "The PDF extraction mode is unsupported.",
                        ),
                        (
                            "stale_media_extract_pdf_tables_input",
                            "PDF sources changed after queue reservation.",
                        ),
                    )
                    if PdfTablesReader in getattr(definition.run, "capabilities", ())
                    else _MAP_ROWS_ERRORS
                )
            ],
            "side_effects": (
                [
                    "create_sheet",
                    "create_columns",
                    "create_rows",
                    "write_op",
                    *(
                        ["read_semantic_join_receipt", "read_current_join_outputs"]
                        if SemanticMatchReader in definition.run.capabilities
                        else []
                    ),
                    *(
                        ["fetch_external_media_url"]
                        if UrlImporter in definition.run.capabilities
                        else []
                    ),
                    *(
                        ["read_source_cell_url", "enumerate_collection_metadata"]
                        if CollectionReader in definition.run.capabilities
                        else []
                    ),
                    *(
                        ["read_sidecar_vectors"]
                        if EmbeddingIndexReader in definition.run.capabilities
                        else []
                    ),
                    *(
                        ["read_local_file"]
                        if LocalFileReader in definition.run.capabilities
                        else []
                    ),
                    *(
                        ["resolve_server_issued_email_sources"]
                        if EmailSourceReader in definition.run.capabilities
                        else []
                    ),
                    *(
                        ["write_blob_store", "write_evidence_links"]
                        if ImportBlobStager in definition.run.capabilities
                        or UrlImporter in definition.run.capabilities
                        else []
                    ),
                    *(
                        ["call_trusted_runtime_importer"]
                        if RuntimeImporter in definition.run.capabilities
                        else []
                    ),
                    "write_receipt",
                ]
                if create_sheet_action
                else list(project_effects)
                if isinstance(definition.run, (_ProjectAction, GoogleSheetsExport))
                else [
                    "read_source_column",
                    "create_generated_column",
                    "write_column_transform_op",
                    "write_receipt",
                ]
                if isinstance(definition.run, ColumnTransform)
                else [
                    "read_input_rows",
                    *(
                        ["read_pdf_blob_cells", "call_natural_pdf_adapter"]
                        if PdfTablesReader
                        in getattr(definition.run, "capabilities", ())
                        else []
                    ),
                    *(
                        ["execute_trusted_local_python"]
                        if PythonEvaluator
                        in getattr(definition.run, "capabilities", ())
                        else []
                    ),
                    *(
                        ["call_mcp_tools"]
                        if McpExtractor in getattr(definition.run, "capabilities", ())
                        else []
                    ),
                    *(
                        [
                            "read_media_blobs",
                            "call_local_metadata_adapters",
                            "write_media_metadata_cache",
                        ]
                        if reads_media_metadata
                        else []
                    ),
                    *(
                        ["call_model_router"]
                        if model_backed or agent_backed or semantic
                        else []
                    ),
                    *(
                        ["call_external_provider"]
                        if external
                        or routed is not None
                        or http
                        or opencorporates
                        or fetch
                        or Researcher in getattr(definition.run, "capabilities", ())
                        else []
                    ),
                    "create_generated_columns",
                    "write_run_results",
                    "write_map_op",
                    *(
                        ["write_model_calls", "write_trace"]
                        if model_backed
                        or agent_backed
                        or semantic
                        or routed is not None
                        else []
                    ),
                    "write_receipt",
                ]
            ),
            "required_capabilities": (
                list(project_requirements)
                if isinstance(definition.run, (_ProjectAction, GoogleSheetsExport))
                else [
                    "project:write",
                    *(
                        ["external:media_download"]
                        if UrlImporter in getattr(definition.run, "capabilities", ())
                        else []
                    ),
                    *(["project:read"] if reads_media_metadata else []),
                    *(
                        ["unsafe:local_code"]
                        if PythonEvaluator
                        in getattr(definition.run, "capabilities", ())
                        or McpExtractor in getattr(definition.run, "capabilities", ())
                        else []
                    ),
                    *(["model:complete"] if model_backed or agent_backed else []),
                    *(
                        ["external:web_search", "external:http_fetch"]
                        if Researcher in getattr(definition.run, "capabilities", ())
                        else []
                    ),
                    *(["model:embed"] if semantic else []),
                    *(["external:web_search"] if external else []),
                    *(["external:api_call"] if http else []),
                    *(["external:opencorporates"] if opencorporates else []),
                    *(["external:media_download"] if fetch else []),
                    *(["external:browser_render"] if screenshot else []),
                    *(
                        [
                            "external:us_census_acs"
                            if routed is CensusDemographics
                            else "model:complete"
                            if routed in (DocumentConverter, OcrReader)
                            else "model:transcribe"
                            if routed is Transcriber
                            else "model:complete"
                            if routed is Translator
                            else "external:geocode"
                        ]
                        if routed is not None
                        else []
                    ),
                ]
            ),
            "cost_policy": {
                "kind": (
                    project_cost
                    if isinstance(definition.run, (_ProjectAction, GoogleSheetsExport))
                    else "model_metered"
                    if model_backed or semantic
                    else "unknown"
                    if agent_backed or http or fetch
                    else "external_metered"
                    if external or routed is not None or opencorporates
                    else "none"
                ),
                "requires_confirmation": (
                    any(spec.requires_confirmation for spec in project_specs)
                    if isinstance(definition.run, (_ProjectAction, GoogleSheetsExport))
                    else model_backed
                    or agent_backed
                    or semantic
                    or external
                    or routed is not None
                    or http
                    or opencorporates
                    or fetch
                ),
            },
            "idempotency": {
                "supported": True,
                "scope": "project",
                "key_field": "idempotency_key",
                "behavior": "Same request keys replay committed results.",
            },
            "retry_policy": {
                "supported": True,
                "strategy": (
                    "provider_retry_then_idempotency_replay"
                    if external or opencorporates
                    else "idempotency_replay"
                ),
            },
            "execution_mode": (
                "cross_sheet"
                if semantic
                else "grouped"
                if isinstance(definition.run, _ProjectAction)
                and definition.run.capabilities == (GroupSummarizer,)
                else "whole_project"
                if project_action or create_sheet_action
                else "atomic_column_transform"
                if isinstance(definition.run, ColumnTransform)
                else "batch_deduped"
                if isinstance(definition.run, MapBatch)
                or (
                    isinstance(definition.run, _ProjectAction)
                    and definition.run.capabilities == (ValueClusterer,)
                )
                else "per_row"
            ),
            "async_mode": (
                "queued"
                if opencorporates
                or any(
                    c in getattr(definition.run, "capabilities", ())
                    for c in (TranscriptReader, TemporalMediaReader)
                )
                else "async"
                if isinstance(definition.run, GoogleSheetsExport)
                else "sync"
            ),
            "writes_project": (
                any(spec.writes_project for spec in project_specs)
                if isinstance(definition.run, (_ProjectAction, GoogleSheetsExport))
                else True
            ),
            "ui_hints": {
                **local_topic_hints,
                "form": form,
                "category": definition.category.value,
                "semantic_controls": controls,
                "source_requirements": source_requirements,
                "logical_outputs": outputs,
                **(
                    {"typed_action": {"creates_sheet": True}}
                    if _creates_sheet(definition.run)
                    else {}
                ),
                **(
                    {"export_target": definition.export_target.ui_hint(form=form)}
                    if definition.export_target is not None
                    else {}
                ),
                **(
                    {"dynamic_outputs": True}
                    if has_dynamic_outputs(definition.run)
                    else {}
                ),
            },
            "receipt_policy": "writes_receipt",
            "row_scope_policy": {
                "kind": "project" if project_action else "sheet_rows",
                "selectors": (
                    []
                    if project_action
                    else ["exact_membership"]
                    if isinstance(definition.run, _ProjectAction)
                    and definition.run.capabilities == (EnclosureMaterializer,)
                    else ["all_rows"]
                    if definition.row_scope is RowScope.ALL_ROWS
                    else ["all_rows", "exact_membership"]
                ),
            },
        }
        if semantic:
            entry["side_effects"].extend(
                ["read_target_sheet_rows", "create_link_child_sheet"]
            )
            entry["errors"].extend(
                {"code": code, "message": message}
                for code, message in (
                    ("duplicate_sheet_name", "The linked sheet name already exists."),
                    (
                        "embedding_backend_unavailable",
                        "No embedding backend is configured.",
                    ),
                    ("join_run_failed", "The semantic join could not complete."),
                    (
                        "model_cost_requires_confirmation",
                        "The embedding cost requires confirmation.",
                    ),
                )
                if code not in {error["code"] for error in entry["errors"]}
            )
        if entry["required_credentials"]:
            entry["errors"].append(
                {
                    "code": "missing_action_credential",
                    "message": "A required provider credential is unavailable.",
                }
            )
        if routed in (OcrReader, Transcriber):
            entry["errors"].extend(
                {"code": code, "message": message}
                for code, message in (
                    (
                        "local_engine_busy",
                        "Another local media read is still finishing.",
                    ),
                    (
                        "local_artifact_unavailable",
                        "The local model artifacts are unavailable.",
                    ),
                    (
                        "local_session_failed",
                        "The local session stopped; retry remaining rows.",
                    ),
                )
            )
        return entry


def _semantic_control(annotation: Any) -> str | None:
    annotation = _semantic_annotation(annotation)
    if annotation in (
        typing.get_args(TranscriptSelection)[0],
        typing.get_args(TemporalExtractSelection)[0],
    ):
        return "transcript_selection"
    if _is_subclass(annotation, ModelRef):
        return "model"
    if _is_subclass(annotation, EngineRef):
        return "engine"
    kind = source_kind(annotation)
    return "rich_source" if kind == "columns_or_template" else kind


def _contains_semantic(
    annotation: Any, seen: frozenset[type[BaseModel]] = frozenset()
) -> bool:
    if _semantic_control(annotation) is not None:
        return True
    if not _is_subclass(annotation, BaseModel):
        return any(
            _contains_semantic(item, seen) for item in typing.get_args(annotation)
        )
    return annotation not in seen and any(
        _contains_semantic(info.annotation, seen | {annotation})
        for info in annotation.model_fields.values()
    )


def _semantic_metadata(
    params: type[BaseModel],
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    controls: dict[str, str] = {}
    requirements: list[dict[str, Any]] = []
    for name, info in params.model_fields.items():
        annotation = info.annotation
        non_null = _without_none(annotation)
        if len(non_null) == 1:
            annotation = non_null[0]
        if (control := _semantic_control(annotation)) is None:
            if _contains_semantic(annotation):
                raise TypeError(f"nested semantic param {name!r} is unsupported")
            continue
        controls[name] = control
        if control in {"model", "engine"}:
            continue
        if control == "transcript_selection":
            details = {
                "max": 1,
                "accepted_column_types": list(
                    TemporalSelectionColumn.accepted_column_types
                ),
            }
        elif control == "column":
            accepted_types = column_ref_types(annotation)
            details = (
                {"accepted_column_types": list(accepted_types)}
                if accepted_types is not None
                else {}
            )
            if _is_subclass(annotation, GeneratedColumnRef):
                details["ai_generated_only"] = True
        elif control == "columns":
            while typing.get_origin(annotation) is typing.Annotated:
                annotation = typing.get_args(annotation)[0]
            item_annotation = typing.get_args(annotation)[0]
            accepted_types = column_ref_types(item_annotation)
            details = (
                {"accepted_column_types": list(accepted_types)}
                if accepted_types is not None
                else {}
            )
            field_schema = params.model_json_schema(by_alias=False)["properties"][name]
            details["min"] = field_schema.get("minItems", 0)
            if "maxItems" in field_schema:
                details["max"] = field_schema["maxItems"]
        elif control == "column_or_template":
            union_args = typing.get_args(annotation)
            column_annotation = next(
                item for item in union_args if _semantic_control(item) == "column"
            )
            template_annotation = next(
                item for item in union_args if _semantic_control(item) == "template"
            )
            accepted = column_ref_types(column_annotation)
            template_types = template_ref_types(template_annotation)
            details = {
                **(
                    {"accepted_column_types": list(accepted)}
                    if accepted is not None
                    else {}
                ),
                **(
                    {"template_accepted_column_types": list(template_types)}
                    if template_types is not None
                    else {}
                ),
                "template_columns": "exact",
            }
        elif control == "rich_source":
            union_args = typing.get_args(annotation)
            template_annotation = next(
                item for item in union_args if _semantic_control(item) == "template"
            )
            template_types = template_ref_types(template_annotation)
            columns_annotation = next(
                item for item in union_args if _semantic_control(item) == "columns"
            )
            while typing.get_origin(columns_annotation) is typing.Annotated:
                columns_annotation = typing.get_args(columns_annotation)[0]
            item_annotation = typing.get_args(columns_annotation)[0]
            details = {
                "accepted_column_types": list(column_ref_types(item_annotation)),
                "template_columns": "exact",
                "accepted_cell_kinds": [
                    "text",
                    *(
                        ["blob"]
                        if set(column_ref_types(item_annotation))
                        & {"image", "audio", "video", "file"}
                        else []
                    ),
                    "template",
                ],
                "unset_fallback_includes_ai_generated": True,
            }
            if (
                template_types is not None
                and list(template_types) != details["accepted_column_types"]
            ):
                details["template_accepted_column_types"] = list(template_types)
        else:
            details = {"template_columns": "union"}
        requirements.append(
            {
                "id": name,
                "param": name,
                "label": name.replace("_", " ").capitalize(),
                "mode": (
                    "columns_or_template" if control == "rich_source" else control
                ),
                "min": int(
                    info.is_required()
                    and type(None) not in typing.get_args(info.annotation)
                    and control
                    in {"column", "columns", "rich_source", "column_or_template"}
                ),
                **details,
            }
        )
    return controls, requirements


@dataclass(frozen=True)
class ActionNamespace:
    name: str
    actions: tuple[Action[Any, Any], ...]

    def __init__(
        self, name: str, *, actions: typing.Iterable[Action[Any, Any]]
    ) -> None:
        if not _LOCAL_NAME.fullmatch(name):
            raise ValueError(f"invalid namespace {name!r}")
        values = tuple(actions)
        if len({action.name for action in values}) != len(values):
            raise ValueError(f"duplicate action name in namespace {name!r}")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "actions", values)


class ActionRegistry:
    def __init__(self, namespaces: typing.Iterable[ActionNamespace]) -> None:
        bound: dict[str, RegisteredAction] = {}
        namespace_names: set[str] = set()
        action_objects: set[int] = set()
        for namespace in namespaces:
            if namespace.name in namespace_names:
                raise ValueError(f"duplicate namespace {namespace.name!r}")
            namespace_names.add(namespace.name)
            for definition in namespace.actions:
                if id(definition) in action_objects:
                    raise ValueError("one Action cannot belong to multiple namespaces")
                action_objects.add(id(definition))
                action_id = f"{namespace.name}.{definition.name}"
                bound[action_id] = RegisteredAction(action_id, definition)
        self._actions = MappingProxyType(bound)

    @property
    def action_ids(self) -> tuple[str, ...]:
        return tuple(self._actions)

    @property
    def actions(self) -> tuple[RegisteredAction, ...]:
        return tuple(self._actions.values())

    def get(self, action_id: str) -> RegisteredAction:
        return self._actions[action_id]
