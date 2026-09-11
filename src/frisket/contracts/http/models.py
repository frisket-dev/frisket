"""Strict wire models for the first generated HTTP contract surfaces."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, RootModel

from frisket.authoring.workbench.contracts import (
    FRONTEND_COMPONENT_BINDING_SCHEMA_VERSION,
    PLUGIN_INSTALL_STATE_SCHEMA_VERSION,
    RUNTIME_INDEX_SCHEMA_VERSION,
    RUNTIME_PLUGIN_SCHEMA_VERSION,
    WORKBENCH_DESCRIPTOR_PACKAGE_SCHEMA_VERSION,
)


ACTION_JOB_SCHEMA_VERSION = "frisket.job.v1"
ACTION_JOBS_PAGE_SCHEMA_VERSION = "frisket.job_list.v1"
PROJECT_SCHEMA_VERSION = "frisket.project.v1"


def _omit_query_defaults(schema: dict[str, object]) -> None:
    """Describe supplied query values; Python defaults are not wire values."""

    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return
    for value in properties.values():
        if isinstance(value, dict):
            value.pop("default", None)


def _omit_query_defaults_and_nulls(schema: dict[str, object]) -> None:
    """Strip defaults and null variants: ``None`` means an absent query value."""

    _omit_query_defaults(schema)
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return
    for value in properties.values():
        if not isinstance(value, dict):
            continue
        variants = value.get("anyOf")
        if not isinstance(variants, list):
            continue
        non_null = [item for item in variants if item != {"type": "null"}]
        if len(non_null) == 1 and len(non_null) < len(variants):
            replacement = non_null[0]
            assert isinstance(replacement, dict)
            del value["anyOf"]
            for key, child in replacement.items():
                value.setdefault(key, child)


class WireModel(BaseModel):
    """Base for closed JSON objects at the public HTTP boundary."""

    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        populate_by_name=True,
    )


class CoerciveRequest(BaseModel):
    """Request body retaining legacy coercion and ignored extensions."""

    model_config = ConfigDict(extra="ignore", strict=False)


class NamedCoerciveRequest(CoerciveRequest):
    """Coercive request also accepting field names for aliased fields."""

    model_config = ConfigDict(populate_by_name=True)


class QueryWireModel(WireModel):
    """Closed query object whose schema describes only supplied wire values."""

    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        populate_by_name=True,
        json_schema_extra=_omit_query_defaults,
    )


class NonNullQueryWireModel(QueryWireModel):
    """Query object whose optional fields are omitted, never null, on the wire."""

    model_config = ConfigDict(json_schema_extra=_omit_query_defaults_and_nulls)


class EmptyQuery(WireModel):
    pass


class ActionJobsQuery(QueryWireModel):
    status: str | None = None
    limit: int = Field(default=100, ge=1, le=500)


class SheetDataQuery(NonNullQueryWireModel):
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=200, ge=0, le=1000)
    parent_row_id: int | None = None
    filter_: str | None = Field(default=None, alias="filter")
    sort: str | None = None
    row_ids: str | None = None


class ActionRunRowsQuery(NonNullQueryWireModel):
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=50, ge=1, le=500)
    status: str | None = None


class HttpError(WireModel):
    """Existing FastAPI error envelope with a closed root object."""

    detail: JsonValue


class CreateProjectRequest(WireModel):
    name: str
    sensitive: bool = False


class ProjectDeleteRequest(WireModel):
    # The project's display name, typed back by the caller to arm an
    # irreversible deletion. Verified server-side against the stored name.
    confirm_name: str
    # Hosted deletion may bind the confirmation to one control-plane project
    # lifetime. Omission preserves the local/team deletion contract.
    project_row_id: int = Field(default=None, gt=0)


class UpdateProjectSensitivityRequest(WireModel):
    sensitive: bool


class UpdateProjectRequest(WireModel):
    name: str | None = None
    description: str | None = None
    starred: bool | None = None
    archived: bool | None = None


class UpdateSheetRequest(WireModel):
    title_column_id: int | None = None


class SheetDeleteResponse(WireModel):
    ok: bool
    deleted_sheet_id: int
    deleted_sheet_name: str


class Project(WireModel):
    id: str
    name: str
    description: str
    sensitive: bool
    starred: bool = False
    archived: bool = False


class ProjectDelete(WireModel):
    ok: bool
    deleted: str


class ProjectListItem(WireModel):
    id: str
    name: str
    description: str
    sensitive: bool
    updated_at: str | None
    pending_review_count: int = Field(ge=0)
    starred: bool
    archived: bool
    role: Literal["viewer", "reviewer", "editor", "owner"] | None


class ProjectList(RootModel[list[ProjectListItem]]):
    """Exact project-picker rows shared by local and hosted producers."""


class SheetListItem(WireModel):
    id: int
    name: str
    parent_sheet_id: int | None
    parent_op_id: int | None
    rows: int = Field(ge=0)
    title_column_id: int | None
    op_kind: str | None = None
    op_label: str | None = None
    materialized_kind: str | None = None
    cited_column_ids: list[int]
    # The narrow annotated-text availability signal:
    # SOURCE text columns reachable through a valid annotation surface. Distinct
    # from cited_column_ids, which names NER's OUTPUT column and every other
    # evidence kind — gating the reader on that would light the Document view up
    # on every sheet that has any evidence at all.
    annotated_text_column_ids: list[int]
    dependent_sheet_ids: list[int]
    columns: list[SheetDataColumn]
    sync_state: str | None = Field(default=None, alias="syncState")
    stale_reason: str | None = None


class SheetList(RootModel[list[SheetListItem]]):
    """Project sheet-picker rows."""


class Sheet(WireModel):
    id: int
    title_column_id: int | None


class SheetDataColumn(WireModel):
    id: int
    name: str
    type: str
    ai_generated: bool
    format: str | None
    # Explicit semantic-contract marker (currently only 'entity_mentions',
    # written by map.ner) — never inferred from cell contents. None means
    # the column carries no declared contract.
    semantic_type: str | None = None
    # Legacy-only value pointer. Generation-managed columns always expose null;
    # their per-cell current_value_ref is authoritative.
    current_run_id: int | None
    # Newest applicable output family for grouping/in-flight presentation only.
    latest_run_id: int | None
    # Exact store predicate used to keep legacy scalar consumers from treating
    # a managed column as single-origin or replacement-compatible.
    generation_managed: bool
    # Derived on demand from distinct exact cell-head origins (limit two).
    mixed_origins: bool
    transcript_status: (
        Literal["missing", "partial", "complete_visible", "complete_hidden"] | None
    )
    media_download_candidate: Literal["media.ytdlp_download", "media.fetch_url"] | None
    # Full-column count of edited generated cells with a differing fresh
    # value, feeding the header chip. 0 when nothing is pending or the
    # column is not ai_generated.
    replay_pending_count: int = Field(default=0, ge=0)
    default_hidden: bool = False


class SheetDataValueRef(WireModel):
    kind: str
    op_id: int | None
    row_id: int
    column_id: int
    run_id: int | None


class SheetDataPendingValue(WireModel):
    # The fresh current-run generated value sitting beneath a human edit,
    # surfaced for the per-cell accept popover. Present only on pending cells.
    fresh_value: JsonValue
    run_id: int
    generated_value_hash: str


class SheetDataCellMeta(WireModel):
    current_value_ref: SheetDataValueRef
    confidence: float | None = None
    justification: str | None = None
    error: str | None = None
    # Result-outcome taxonomy bucket (store/runs.py), present only on failed
    # cells — 'empty_output' marks the terminal-failure bucket behind the
    # row-level "Retry anyway" affordance.
    outcome: str | None = None
    review_state: str | None = None
    state: str | None = None
    pending_value: SheetDataPendingValue | None = None
    invalid: bool = False


class SheetDataRow(WireModel):
    id: int
    cells: dict[str, JsonValue]
    meta: dict[str, SheetDataCellMeta]
    parent_row_id: int | None
    child_count: int = Field(ge=0)


class SheetData(WireModel):
    columns: list[SheetDataColumn]
    rows: list[SheetDataRow]
    total: int = Field(ge=0)


class ColumnStatsColumn(WireModel):
    id: int
    name: str
    type: str
    format: str | None


class ColumnStatsTopValue(WireModel):
    value: str
    count: int = Field(ge=0)


class ColumnStatsHistogramBin(WireModel):
    min: int | float
    max: int | float
    count: int = Field(ge=0)


class ColumnStatsNumeric(WireModel):
    count: int = Field(ge=0)
    mean: int | float
    median: int | float
    min: int | float
    max: int | float
    histogram: list[ColumnStatsHistogramBin]


class ColumnStatsText(WireModel):
    count: int = Field(ge=0)
    shortest: str
    shortest_length: int = Field(ge=0)
    longest: str
    longest_length: int = Field(ge=0)
    mean_length: int | float
    median_length: int | float
    length_histogram: list[ColumnStatsHistogramBin]


class ColumnStatsDate(WireModel):
    count: int = Field(ge=0)
    min: str
    median: str
    max: str


class ColumnStatsFile(WireModel):
    count: int = Field(ge=0)
    min_size: int = Field(ge=0)
    max_size: int = Field(ge=0)


class ColumnStatsJsonType(WireModel):
    type: str
    count: int = Field(ge=0)


class ColumnStats(WireModel):
    schema_version: Literal["frisket.column_stats.v1"]
    sheet_id: int
    column: ColumnStatsColumn
    row_count: int = Field(ge=0)
    threshold: int = Field(ge=0)
    computed: bool
    requires_manual_analyze: bool
    missing: int = Field(default=0, ge=0)
    invalid: int = Field(default=0, ge=0)
    present: int = Field(default=0, ge=0)
    distinct: int = Field(default=0, ge=0)
    top_values: list[ColumnStatsTopValue] = Field(default_factory=list)
    numeric: ColumnStatsNumeric | None = None
    text: ColumnStatsText | None = None
    date: ColumnStatsDate | None = None
    json_types: list[ColumnStatsJsonType] = Field(default_factory=list)
    file: ColumnStatsFile | None = None


class SheetRowLocation(WireModel):
    schema_version: Literal["frisket.sheet_row_location.v1"]
    sheet_id: int
    row_id: int
    found: bool
    index: int | None
    page_offset: int | None
    page_size: int = Field(ge=1, le=1000)


class RunRowErrorGroup(WireModel):
    message: str
    count: int = Field(ge=0)
    code: str | None
    # Result-outcome taxonomy bucket for this group (store/runs.py:
    # model_error / invalid_output / empty_output) and whether that bucket is
    # terminal — the failure-triage UI's grouping and retry wording read
    # these, never the message text.
    outcome: str | None = None
    terminal: bool = False
    row_ids: list[int]
    # Retry/resume metadata for the typed resumable provider codes
    # (frisket.llm.remediation.RESUMABLE_PROVIDER_ERROR_DETAILS:
    # provider_rate_limited, provider_key_exhausted, invalid_provider_key);
    # absent for untyped failure messages.
    details: dict[str, bool] | None = None


class RunRowErrorSummary(WireModel):
    total_failed_rows: int = Field(ge=0)
    groups: list[RunRowErrorGroup]


class ActionRunTiming(WireModel):
    started_at: str | None
    finished_at: str | None
    elapsed_seconds: float | None
    processed_rows: int = Field(ge=0)
    remaining_rows: int = Field(ge=0)
    processed_rows_per_second: float | None
    eta_seconds: float | None
    estimated_finish_at: str | None
    queue_wait_seconds: float | None
    job_elapsed_seconds: float | None


class ActionRunQueue(WireModel):
    job_id: int
    status: str
    attempts: int = Field(ge=0)
    max_attempts: int = Field(ge=0)
    created_at: str | None
    started_at: str | None
    finished_at: str | None
    locked_by: str | None
    locked_at: str | None
    lease_expires_at: str | None
    lease_expired: bool
    action_kind: str | None
    action_name: str | None
    error: str | None
    queued_seconds: float | None = None
    live_workers: int | None = Field(default=None, ge=0)
    no_live_worker: bool | None = None


class ActionRunMissingQueue(WireModel):
    job_id: int | None
    status: Literal["missing"]


class ActionRunPublicStatus(WireModel):
    run_id: int
    action_kind: str
    action_name: str
    status: str
    total: int = Field(ge=0)
    completed: int = Field(ge=0)
    failed: int = Field(ge=0)
    cost: float | None
    live: bool
    timing: ActionRunTiming
    queue: ActionRunQueue | ActionRunMissingQueue | None = None
    stalled_reason: str | None = None
    # A typed recipe/session halt behind a resumable 'cancelled' (e.g.
    # Parakeet's local_artifact_unavailable): the code is stable for UI
    # branching, the reason is the human sentence to show.
    halted_code: str | None = None
    halted_reason: str | None = None
    error: str | None = None
    row_errors: RunRowErrorSummary | None = None


class ActionJobProgress(ActionRunPublicStatus):
    sheet_id: int


class ActionRunStatusRun(WireModel):
    id: int
    sheet_id: int
    action_kind: str
    action_name: str
    status: str
    total_rows: int = Field(ge=0)
    completed_rows: int = Field(ge=0)
    failed_rows: int = Field(ge=0)
    cost_actual: float | None
    cost_estimate: float | None
    row_errors: RunRowErrorSummary | None = None
    public_status: ActionRunPublicStatus


class ActionRunStatus(WireModel):
    schema_version: Literal["frisket.actions.v1"]
    action: Literal["run_status"]
    project_id: str
    run: ActionRunStatusRun


class ActionRunRowsRun(WireModel):
    id: int
    action_kind: str
    action_name: str
    status: str
    total_rows: int = Field(ge=0)
    completed_rows: int = Field(ge=0)
    failed_rows: int = Field(ge=0)


class ActionRunRowsCell(WireModel):
    column_id: int
    column_name: str
    value: JsonValue
    error: str | None
    tokens_in: int | None
    tokens_out: int | None
    cost: float | None
    confidence: float | None
    justification: str | None
    review_state: str | None


class ActionRunRowsRow(WireModel):
    row_id: int
    row_index: int
    status: str
    error: str | None
    tokens_in: int | None
    tokens_out: int | None
    cost: float | None
    retry_count: int = Field(ge=0)
    retries: list[dict[str, JsonValue]]
    cells: list[ActionRunRowsCell]


class ActionRunRows(WireModel):
    run: ActionRunRowsRun
    offset: int = Field(ge=0)
    limit: int = Field(ge=1, le=500)
    total: int = Field(ge=0)
    has_more: bool
    next_offset: int | None
    rows: list[ActionRunRowsRow]


class ActionRunCancel(WireModel):
    run_id: int
    status: Literal["cancelled"]
    completed: int | None
    total: int | None
    queue_job_id: int | None
    queue_cancelled: bool


class ActionJobLease(WireModel):
    locked_by: str | None
    locked_at: str | None
    lease_expires_at: str | None
    lease_expired: bool


class ActionJobTiming(WireModel):
    created_at: str | None
    started_at: str | None
    finished_at: str | None


class ActionJob(WireModel):
    schema_version: Literal[ACTION_JOB_SCHEMA_VERSION]
    project_id: str
    job_id: int
    kind: str
    run_id: int | None
    receipt_id: str | None = None
    payload_ref: dict[str, JsonValue]
    status: str
    action_kind: str | None = None
    action_name: str | None = None
    result_summary: dict[str, JsonValue] | None = None
    progress: ActionJobProgress | None = None
    attempts: int
    max_attempts: int
    lease: ActionJobLease
    timing: ActionJobTiming
    error: str | None


class ActionJobsPage(WireModel):
    schema_version: Literal[ACTION_JOBS_PAGE_SCHEMA_VERSION]
    project_id: str
    jobs: list[ActionJob]


class WorkbenchPluginContributionSummary(WireModel):
    kind: str
    count: int
    ids: list[str]


class WorkbenchPluginRequirements(WireModel):
    capabilities: list[str]
    secrets: list[str]


class WorkbenchFrontendComponentBinding(WireModel):
    schema_version: Literal[FRONTEND_COMPONENT_BINDING_SCHEMA_VERSION] = Field(
        alias="schemaVersion"
    )
    contribution_id: str = Field(alias="contributionId")
    module_key: str = Field(alias="moduleKey")
    component_key: str = Field(alias="componentKey")
    module_path: str | None = Field(default=None, alias="modulePath")
    module_url: str | None = Field(default=None, alias="moduleUrl")


class WorkbenchDescriptorPackageSummary(WireModel):
    schema_version: Literal[WORKBENCH_DESCRIPTOR_PACKAGE_SCHEMA_VERSION] = Field(
        alias="schemaVersion"
    )
    source_path: str = Field(alias="sourcePath")
    descriptor_count: int = Field(alias="descriptorCount")
    runtime_only_fields_stripped: list[str] = Field(alias="runtimeOnlyFieldsStripped")


class WorkbenchPluginRuntimePlugin(WireModel):
    schema_version: Literal[RUNTIME_PLUGIN_SCHEMA_VERSION] = Field(
        alias="schemaVersion"
    )
    plugin_id: str = Field(alias="pluginId")
    version: str
    install_state: str = Field(alias="installState")
    activation: str
    runtime_source: str = Field(alias="runtimeSource")
    receipt_id: str | None = Field(alias="receiptId")
    manifest_sha256: str = Field(alias="manifestSha256")
    package_sha256: str = Field(alias="packageSha256")
    byte_count: int = Field(alias="byteCount")
    source: dict[str, JsonValue]
    contribution_summary: list[WorkbenchPluginContributionSummary] = Field(
        alias="contributionSummary"
    )
    frontend_component_bindings: list[WorkbenchFrontendComponentBinding] = Field(
        alias="frontendComponentBindings"
    )
    requires: WorkbenchPluginRequirements
    arbitrary_package_load_allowed: bool = Field(alias="arbitraryPackageLoadAllowed")
    registry_activated: bool = Field(alias="registryActivated")
    install_state_schema_version: (
        Literal[PLUGIN_INSTALL_STATE_SCHEMA_VERSION] | None
    ) = Field(alias="installStateSchemaVersion")
    disabled_reason: str | None = Field(alias="disabledReason")
    install_failure: dict[str, JsonValue] | None = Field(
        default=None,
        alias="installFailure",
    )
    workbench_descriptor_package: WorkbenchDescriptorPackageSummary | None = Field(
        default=None,
        alias="workbenchDescriptorPackage",
    )
    workbench_descriptor_manifests: list[dict[str, JsonValue]] | None = Field(
        default=None,
        alias="workbenchDescriptorManifests",
    )
    settings: list[dict[str, JsonValue]] | None = None
    project_id: str | None = Field(default=None, alias="projectId")


class WorkbenchFirstPartyDescriptorPackage(WireModel):
    schema_version: Literal[WORKBENCH_DESCRIPTOR_PACKAGE_SCHEMA_VERSION] = Field(
        alias="schemaVersion"
    )
    descriptors: list[dict[str, JsonValue]]


class WorkbenchPluginRuntimeIndex(WireModel):
    schema_version: Literal[RUNTIME_INDEX_SCHEMA_VERSION] = Field(alias="schemaVersion")
    project_id: str = Field(alias="projectId")
    arbitrary_package_load_allowed: bool = Field(alias="arbitraryPackageLoadAllowed")
    receipt_scan_limit: int = Field(alias="receiptScanLimit")
    skipped_invalid_receipts: int = Field(alias="skippedInvalidReceipts")
    skipped_invalid_manifest_refs: int = Field(alias="skippedInvalidManifestRefs")
    loaded_plugin_count: int = Field(alias="loadedPluginCount")
    plugins: list[WorkbenchPluginRuntimePlugin]
    first_party: WorkbenchFirstPartyDescriptorPackage = Field(alias="firstParty")


# Producer schema literals for the plugin settings/lifecycle wires (restated:
# the contracts package stays out of the plugin runtime layer; the backend
# contract test pins them against the producer constants).
WORKBENCH_PLUGIN_SETTINGS_SCHEMA_VERSION = "frisket.workbench_plugin_settings.v1"
WORKBENCH_PLUGIN_ACTIVATION_SCHEMA_VERSION = "frisket.workbench_plugin_activation.v1"
WORKBENCH_PLUGIN_BACKEND_ACTIVATION_SCHEMA_VERSION = (
    "frisket.workbench_plugin_backend_activation.v1"
)
WORKBENCH_PLUGIN_INSTALL_PLAN_EXECUTION_SCHEMA_VERSION = (
    "frisket.plugin_install_plan_execution.v1"
)


class _WorkbenchCompatibleRequest(CoerciveRequest):
    """Keep the existing operational request coercion at this boundary.

    Field names are the camelCase wire keys directly — no aliases, so no
    alternate internal-name spelling is ever accepted as a wire key. The
    consent/trust flags ride by value and default to the refusing state."""


class WorkbenchPluginSettingsPatchRequest(_WorkbenchCompatibleRequest):
    values: dict[str, JsonValue] = Field(default_factory=dict)


class WorkbenchPluginLocalInstallRequest(_WorkbenchCompatibleRequest):
    source: dict[str, JsonValue]
    arbitraryPackageLoadAllowed: bool = False


class WorkbenchPluginActivationRequest(_WorkbenchCompatibleRequest):
    receiptId: str
    trustAcknowledged: bool = False
    permissionsAccepted: list[str] = Field(default_factory=list)
    arbitraryPackageLoadAllowed: bool = False


class WorkbenchPluginBackendActivationRequest(_WorkbenchCompatibleRequest):
    trustAcknowledged: bool = False
    arbitraryPackageLoadAllowed: bool = False
    executableHandlersAllowed: bool = False


class WorkbenchPluginSettingOption(WireModel):
    """One manifest-declared setting with its effective project value.

    ``default_value``/``effective_value`` are per-plugin variable leaves and
    stay open JSON by design."""

    id: str
    title: str
    type: str
    description: str | None
    default_value: JsonValue = Field(alias="defaultValue")
    effective_value: JsonValue = Field(alias="effectiveValue")
    source: str
    enum: list[str] | None
    min: float | None
    max: float | None
    read_only: bool = Field(alias="readOnly")


class WorkbenchPluginSettings(WireModel):
    schema_version: Literal[WORKBENCH_PLUGIN_SETTINGS_SCHEMA_VERSION] = Field(
        alias="schemaVersion"
    )
    project_id: str = Field(alias="projectId")
    plugin_id: str = Field(alias="pluginId")
    can_mutate: bool = Field(alias="canMutate")
    settings: list[WorkbenchPluginSettingOption]


class WorkbenchPluginInstallExecution(WireModel):
    """Local-install plan execution result; the same envelope carries the
    failed-plan 4xx bodies, so ``install_failure`` stays the open failure
    object the runtime index already uses."""

    schema_version: Literal[WORKBENCH_PLUGIN_INSTALL_PLAN_EXECUTION_SCHEMA_VERSION] = (
        Field(alias="schemaVersion")
    )
    project_id: str = Field(alias="projectId")
    plugin_id: str = Field(alias="pluginId")
    source: dict[str, JsonValue]
    install_state: str = Field(alias="installState")
    activation: str
    runtime_source: str = Field(alias="runtimeSource")
    receipt_id: str | None = Field(alias="receiptId")
    manifest_sha256: str = Field(alias="manifestSha256")
    package_sha256: str = Field(alias="packageSha256")
    arbitrary_package_load_allowed: bool = Field(alias="arbitraryPackageLoadAllowed")
    install_failure: dict[str, JsonValue] | None = Field(alias="installFailure")
    layout_mutated: bool = Field(alias="layoutMutated")
    workbench_descriptor_package: WorkbenchDescriptorPackageSummary | None = Field(
        default=None,
        alias="workbenchDescriptorPackage",
    )
    workbench_descriptor_manifests: list[dict[str, JsonValue]] | None = Field(
        default=None,
        alias="workbenchDescriptorManifests",
    )


class WorkbenchPluginActivation(WireModel):
    """Frontend/manifest activation result — the consent facts
    (``arbitrary_package_load_allowed``, ``permissions_accepted``) ride by
    value, never summarized."""

    schema_version: Literal[WORKBENCH_PLUGIN_ACTIVATION_SCHEMA_VERSION] = Field(
        alias="schemaVersion"
    )
    project_id: str = Field(alias="projectId")
    plugin_id: str = Field(alias="pluginId")
    receipt_id: str = Field(alias="receiptId")
    manifest_sha256: str = Field(alias="manifestSha256")
    package_sha256: str = Field(alias="packageSha256")
    runtime_source: str = Field(alias="runtimeSource")
    activation: str
    install_state: str = Field(alias="installState")
    registry_activated: bool = Field(alias="registryActivated")
    arbitrary_package_load_allowed: bool = Field(alias="arbitraryPackageLoadAllowed")
    permissions_accepted: list[str] = Field(alias="permissionsAccepted")
    registered_plugin_manifests: list[str] = Field(alias="registeredPluginManifests")


class WorkbenchPluginBackendActivation(WireModel):
    """Backend contribution activation result — registers EXECUTABLE handlers,
    so the grant flags ride by value; the ``registered*`` structures are
    per-plugin variable leaves and stay open JSON."""

    schema_version: Literal[WORKBENCH_PLUGIN_BACKEND_ACTIVATION_SCHEMA_VERSION] = Field(
        alias="schemaVersion"
    )
    project_id: str = Field(alias="projectId")
    plugin_id: str = Field(alias="pluginId")
    receipt_id: str = Field(alias="receiptId")
    manifest_sha256: str = Field(alias="manifestSha256")
    package_sha256: str = Field(alias="packageSha256")
    runtime_source: str = Field(alias="runtimeSource")
    arbitrary_package_load_allowed: bool = Field(alias="arbitraryPackageLoadAllowed")
    executable_handlers_registered: bool = Field(alias="executableHandlersRegistered")
    trusted_runtime_bindings_registered: bool = Field(
        alias="trustedRuntimeBindingsRegistered"
    )
    registered_backend_contributions: dict[str, JsonValue] = Field(
        alias="registeredBackendContributions"
    )
    registered_executable_handlers: dict[str, JsonValue] = Field(
        alias="registeredExecutableHandlers"
    )
    registered_runtime_bindings: dict[str, JsonValue] = Field(
        alias="registeredRuntimeBindings"
    )


class WorkbenchPluginInstallState(WireModel):
    """Shared disable/uninstall result: the persisted install-state row."""

    schema_version: Literal[PLUGIN_INSTALL_STATE_SCHEMA_VERSION] = Field(
        alias="schemaVersion"
    )
    project_id: str = Field(alias="projectId")
    plugin_id: str = Field(alias="pluginId")
    receipt_id: str = Field(alias="receiptId")
    manifest_sha256: str = Field(alias="manifestSha256")
    package_sha256: str = Field(alias="packageSha256")
    install_state: str = Field(alias="installState")
    activation: str
    runtime_source: str = Field(alias="runtimeSource")
    permissions_accepted: list[str] = Field(alias="permissionsAccepted")
    registry_activated: bool = Field(alias="registryActivated")
    arbitrary_package_load_allowed: bool = Field(alias="arbitraryPackageLoadAllowed")
    disabled_reason: str | None = Field(alias="disabledReason")
    source: dict[str, JsonValue]
    install_failure: dict[str, JsonValue] | None = Field(alias="installFailure")


__all__ = [
    "ACTION_JOB_SCHEMA_VERSION",
    "ACTION_JOBS_PAGE_SCHEMA_VERSION",
    "PROJECT_SCHEMA_VERSION",
    "WORKBENCH_PLUGIN_ACTIVATION_SCHEMA_VERSION",
    "WORKBENCH_PLUGIN_BACKEND_ACTIVATION_SCHEMA_VERSION",
    "WORKBENCH_PLUGIN_INSTALL_PLAN_EXECUTION_SCHEMA_VERSION",
    "WORKBENCH_PLUGIN_SETTINGS_SCHEMA_VERSION",
    "ActionRunCancel",
    "ActionRunRowsQuery",
    "ActionRunRows",
    "ActionRunStatus",
    "ActionJob",
    "ActionJobsPage",
    "ActionJobsQuery",
    "CreateProjectRequest",
    "EmptyQuery",
    "HttpError",
    "ProjectDeleteRequest",
    "ProjectDelete",
    "ProjectListItem",
    "ProjectList",
    "Project",
    "SheetDataQuery",
    "SheetData",
    "SheetList",
    "Sheet",
    "UpdateProjectRequest",
    "UpdateSheetRequest",
    "WorkbenchPluginActivation",
    "WorkbenchPluginActivationRequest",
    "WorkbenchPluginBackendActivation",
    "WorkbenchPluginBackendActivationRequest",
    "WorkbenchPluginInstallExecution",
    "WorkbenchPluginInstallState",
    "WorkbenchPluginLocalInstallRequest",
    "WorkbenchPluginRuntimeIndex",
    "WorkbenchPluginSettingOption",
    "WorkbenchPluginSettings",
    "WorkbenchPluginSettingsPatchRequest",
]
