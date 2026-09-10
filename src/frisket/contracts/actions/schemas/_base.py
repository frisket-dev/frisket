"""Shared action-contract primitives: base models, policies, catalog envelope, and cross-family helpers."""

from __future__ import annotations

import json
from collections.abc import Mapping

from typing import Annotated, Any, Literal

from typing_extensions import NotRequired, Required, TypedDict

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_serializer,
)

from frisket.authoring import column_types


ACTION_SCHEMA_VERSION = "frisket.action.v2"
ACTION_CATALOG_SCHEMA_VERSION = "frisket.action_catalog.v2"
CURRENT_ACTION_AUTHORING_CONTRACT_VERSION = 1
ACTION_ERROR_SCHEMA_VERSION = "frisket.action_error.v1"
ACTION_VALIDATION_SCHEMA_VERSION = "frisket.action_validation.v1"
ACTION_RESULT_SCHEMA_VERSION = "frisket.action_result.v1"
RECEIPT_SCHEMA_VERSION = "frisket.receipt.v1"
MAX_IMPORT_ROWS_COLUMNS = 256
MAX_CELL_EDITS = 1_000


ExecutionMode = Literal[
    "per_row",
    "batch_deduped",
    "atomic_column_transform",
    "grouped",
    "source_poll",
    "whole_project",
    "cross_sheet",
    # Queued placements: plugin/project actions launched onto the run queue
    # (server/action_catalog_hints.py emits both into the catalog wire).
    "queued_action_job",
    "queued_project_run",
]
AsyncMode = Literal["sync", "async", "queued"]
CostPolicyKind = Literal["none", "model_metered", "external_metered", "unknown"]
ActionResultStatus = Literal[
    "needs_confirmation",
    "queued",
    "running",
    "completed",
    "partial",
    "failed",
    "cancelled",
]
EvidenceRetention = Literal["compactable", "pinned", "materialized"]


def _strict_authoring_contract_version(value: Any) -> Any:
    if type(value) is not int:
        raise ValueError("authoring_contract_version must be a strict integer")
    return value


ActionAuthoringContractVersion = Annotated[
    Literal[1],
    BeforeValidator(_strict_authoring_contract_version),
]


V1_COLUMN_TYPE_ALIASES = {"url": "link"}


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


StrictPositiveInt = Annotated[int, Field(gt=0, strict=True)]
StrictString = Annotated[str, Field(strict=True)]
StrictPersistedId = Annotated[int, Field(gt=0, le=2**63 - 1, strict=True)]


class AllRowsSelector(ContractModel):
    kind: Literal["all_rows"]


class ExactRowMembership(ContractModel):
    row_ids: list[StrictPersistedId]

    @field_validator("row_ids")
    @classmethod
    def _canonical_row_ids(cls, row_ids: list[int]) -> list[int]:
        return sorted(set(row_ids))


class ExactMembershipSelector(ContractModel):
    kind: Literal["exact_membership"]
    membership: ExactRowMembership


SheetRowSelector = Annotated[
    AllRowsSelector | ExactMembershipSelector,
    Field(discriminator="kind"),
]


class SheetRowScope(ContractModel):
    sheet_id: StrictPersistedId
    selector: SheetRowSelector


class SheetRowScopePolicy(ContractModel):
    kind: Literal["sheet_rows"] = "sheet_rows"
    selectors: list[Literal["all_rows", "exact_membership"]] = Field(min_length=1)

    @field_validator("selectors")
    @classmethod
    def _unique_selectors(
        cls, selectors: list[Literal["all_rows", "exact_membership"]]
    ) -> list[Literal["all_rows", "exact_membership"]]:
        if len(selectors) != len(set(selectors)):
            raise ValueError("row scope selectors must be unique")
        return selectors


class ProjectScopePolicy(ContractModel):
    kind: Literal["project"] = "project"
    selectors: list[Literal["all_rows", "exact_membership"]] = Field(
        default_factory=list, max_length=0
    )


class ActionSpec(ContractModel):
    schema_version: Literal[ACTION_SCHEMA_VERSION] = ACTION_SCHEMA_VERSION
    kind: str = Field(min_length=1)
    params: dict[str, Any]
    row_scope: SheetRowScope | None = None
    input_refs: list[dict[str, Any]] = Field(default_factory=list)
    output_intent: list[dict[str, Any]] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    idempotency_key: str | None = None


class ActionError(ContractModel):
    schema_version: Literal[ACTION_ERROR_SCHEMA_VERSION] = ACTION_ERROR_SCHEMA_VERSION
    code: str
    message: str
    action_kind: str | None = None
    field: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    # Generic confirmation marker: a deterministic guard sets this so the
    # executor's failed-result path emits the SAME HTTP 402
    # needs_confirmation envelope as the model-cost gate, instead of a code
    # allowlist. Any error carrying it (e.g.
    # join_fanout_requires_confirmation) is a confirmation gate; the default
    # keeps plain validation errors at 400.
    needs_confirmation: bool = False


class ActionErrorSpec(ContractModel):
    code: str
    message: str


def _idempotency_stale_running_error() -> ActionErrorSpec:
    return ActionErrorSpec(
        code="idempotency_stale_running",
        message=(
            "A stale running idempotency reservation was cleared; retry the "
            "action to continue."
        ),
    )


class ActionIdentity(ContractModel):
    kind: str
    action_id: str


class ActionOutputPayload(TypedDict, total=False):
    """Serialized ActionOutput shape, including its one conditional omission."""

    kind: Required[str]
    name: str | None
    sheet_id: int | None
    column_id: int | None
    row_ids: list[int]
    ref: dict[str, Any]


class ActionOutput(ContractModel):
    kind: str
    name: str | None = None
    sheet_id: int | None = None
    column_id: int | None = None
    row_ids: list[int] = Field(default_factory=list)
    ref: dict[str, Any] = Field(default_factory=dict)

    @model_serializer
    def _serialize_action_output(self) -> ActionOutputPayload:
        payload: ActionOutputPayload = {
            "kind": self.kind,
            "name": self.name,
            "sheet_id": self.sheet_id,
            "column_id": self.column_id,
            "row_ids": self.row_ids,
            "ref": self.ref,
        }
        # A large streamed CSV represents its selected rows with the compact
        # artifact/receipt digest, never with an output-level placeholder that
        # could be mistaken for a complete rowset. Other action outputs retain
        # the established `row_ids: []` wire shape.
        row_count = self.ref.get("row_count") if isinstance(self.ref, Mapping) else None
        if (
            self.kind == "export"
            and not self.row_ids
            and isinstance(self.ref, Mapping)
            and self.ref.get("kind") == "export_artifact"
            and self.ref.get("export_kind") == "sheet_csv"
            and isinstance(row_count, int)
            and not isinstance(row_count, bool)
            and row_count > 10_000
        ):
            payload.pop("row_ids")
        return payload


class ActionResult(ContractModel):
    schema_version: Literal[ACTION_RESULT_SCHEMA_VERSION] = ACTION_RESULT_SCHEMA_VERSION
    action: ActionIdentity
    status: ActionResultStatus
    project_id: str
    run_id: int | None = None
    job_id: int | None = None
    op_ids: list[int] = Field(default_factory=list)
    outputs: list[ActionOutput] = Field(default_factory=list)
    value: JsonValue | None = None
    receipt_id: str | None = None
    warnings: list[str] = Field(default_factory=list)
    errors: list[ActionError] = Field(default_factory=list)


class ReceiptIOPayload(TypedDict):
    """Closed serialized form for optional-kind receipt inputs and outputs."""

    name: str
    ref: dict[str, Any]
    kind: NotRequired[str]


class ReceiptIO(ContractModel):
    name: str
    ref: dict[str, Any]
    kind: str | None = None

    @model_serializer
    def _serialize_receipt_io(self) -> ReceiptIOPayload:
        payload: ReceiptIOPayload = {"name": self.name, "ref": self.ref}
        if self.kind is not None:
            payload["kind"] = self.kind
        return payload


class ReceiptEvidence(ContractModel):
    ref: dict[str, Any]
    retention: EvidenceRetention = "compactable"


class Receipt(ContractModel):
    schema_version: Literal[RECEIPT_SCHEMA_VERSION] = RECEIPT_SCHEMA_VERSION
    receipt_id: str
    project_id: str
    action_id: str
    action_kind: str
    run_id: int | None = None
    op_ids: list[int] = Field(default_factory=list)
    idempotency_key: str | None = None
    params_hash: str | None = None
    status: ActionResultStatus
    inputs: list[ReceiptIO] = Field(default_factory=list)
    outputs: list[ReceiptIO] = Field(default_factory=list)
    # Authored domain return, distinct from host-recorded effects and evidence.
    value: JsonValue | None = None
    provider_use: list[dict[str, Any]] = Field(default_factory=list)
    evidence: list[ReceiptEvidence] = Field(default_factory=list)
    trace_ref: dict[str, Any] | None = None
    review: dict[str, Any] | None = None
    exports: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    errors: list[ActionError] = Field(default_factory=list)


class CostPolicy(ContractModel):
    kind: CostPolicyKind
    requires_confirmation: bool = False
    notes: str | None = None


class IdempotencyPolicy(ContractModel):
    supported: bool
    scope: Literal["project"]
    key_field: Literal["idempotency_key"]
    behavior: str


class RetryPolicy(ContractModel):
    supported: bool
    strategy: str
    notes: str | None = None


class ConditionalCapabilityWhen(ContractModel):
    param: str
    value: str


class ConditionalCapability(ContractModel):
    capability: str
    when: ConditionalCapabilityWhen


class ActionCatalogEntry(ContractModel):
    kind: str
    authoring_contract_version: ActionAuthoringContractVersion = (
        CURRENT_ACTION_AUTHORING_CONTRACT_VERSION
    )
    title: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    errors: list[ActionErrorSpec]
    side_effects: list[str]
    required_capabilities: list[str]
    cost_policy: CostPolicy
    idempotency: IdempotencyPolicy
    retry_policy: RetryPolicy
    execution_mode: ExecutionMode
    async_mode: AsyncMode
    writes_project: bool
    examples: list[dict[str, Any]] = Field(default_factory=list)
    ui_hints: dict[str, Any] = Field(default_factory=dict)
    receipt_policy: Literal[
        "writes_receipt",
        "deferred_until_receipts_table",
    ] = "deferred_until_receipts_table"
    # Credential names (env-var style, e.g. "CENSUS_API_KEY") the action
    # needs to run. Resolved by frisket.credentials.missing_required_
    # credentials (env var, then the project secrets store) and gated at
    # queue time by server/action_enqueue.py before reservation — a state
    # distinct from the cost-gate's needs_confirmation/402 envelope. Empty
    # for every action today except enrich.census_demographics (the pinned
    # concrete case).
    required_credentials: list[str] = Field(default_factory=list)
    # Server-owned emission vocabulary: capabilities added to an ActionSpec
    # only when the assembled canonical param (or its schema default) matches.
    # This is descriptive catalog data; backend prechecks remain authoritative.
    conditional_capabilities: list[ConditionalCapability] = Field(default_factory=list)
    row_scope_policy: SheetRowScopePolicy | ProjectScopePolicy | None = None


class ActionCatalog(ContractModel):
    schema_version: Literal[ACTION_CATALOG_SCHEMA_VERSION] = (
        ACTION_CATALOG_SCHEMA_VERSION
    )
    action_schema: dict[str, Any]
    error_schema: dict[str, Any]
    result_schema: dict[str, Any]
    receipt_schema: dict[str, Any]
    validation_result_schema: dict[str, Any]
    actions: list[ActionCatalogEntry]


class ActionValidationResult(ContractModel):
    schema_version: Literal[ACTION_VALIDATION_SCHEMA_VERSION] = (
        ACTION_VALIDATION_SCHEMA_VERSION
    )
    ok: bool
    action: ActionSpec | None = None
    params: dict[str, Any] | None = None
    error: ActionError | None = None


class ConfirmationContextParams(ContractModel):
    """Retry-flow fields shared by actions that can return HTTP 402."""

    confirmed: bool = False
    consented_promise_set_hash: StrictString | None = None


def _is_v1_column_type(type_name: str) -> bool:
    return column_types.is_registered(canonical_column_type(type_name))


def canonical_column_type(type_name: str) -> str:
    return V1_COLUMN_TYPE_ALIASES.get(type_name, type_name)


def _value_matches_column_type(type_name: str, value: Any) -> bool:
    if value is None:
        return True
    return column_types.validate_value(canonical_column_type(type_name), value)


def _is_json_value(value: Any) -> bool:
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError):
        return False
    return True
