"""Compatibility facade for the v1 action contract catalog.

This module defines the schema/validation-only surface for the first v1 action
catalog slice. It does not execute actions or mutate projects.

Class and constant definitions live in per-family modules under
`frisket.contracts.actions.schemas.<family>`; shared primitives (base models,
policies, the catalog envelope, and cross-family helpers) live in
`frisket.contracts.actions.schemas._base`. This module re-exports the public
surface so existing `from frisket.contracts.action import X` call sites keep
working unchanged.
"""

from __future__ import annotations

from frisket.authoring.templates import column_template_names

from frisket.contracts.actions.schemas._base import (
    ACTION_CATALOG_SCHEMA_VERSION,
    ACTION_ERROR_SCHEMA_VERSION,
    ACTION_RESULT_SCHEMA_VERSION,
    ACTION_SCHEMA_VERSION,
    ACTION_VALIDATION_SCHEMA_VERSION,
    CURRENT_ACTION_AUTHORING_CONTRACT_VERSION,
    ActionAuthoringContractVersion,
    ActionCatalog,
    ActionCatalogEntry,
    ActionError,
    ActionErrorSpec,
    ActionIdentity,
    ActionOutput,
    ActionResult,
    ActionResultStatus,
    ActionSpec,
    ActionValidationResult,
    AllRowsSelector,
    AsyncMode,
    ContractModel,
    CostPolicy,
    CostPolicyKind,
    EvidenceRetention,
    ExecutionMode,
    IdempotencyPolicy,
    MAX_CELL_EDITS,
    MAX_IMPORT_ROWS_COLUMNS,
    RECEIPT_SCHEMA_VERSION,
    Receipt,
    ReceiptEvidence,
    ReceiptIO,
    RetryPolicy,
    ExactMembershipSelector,
    ExactRowMembership,
    SheetRowScope,
    SheetRowScopePolicy,
    StrictPositiveInt,
    StrictString,
    V1_COLUMN_TYPE_ALIASES,
    _idempotency_stale_running_error,
    _is_json_value,
    _is_v1_column_type,
    _value_matches_column_type,
    canonical_column_type,
)

from frisket.contracts.actions.schemas.temporal import (
    TemporalColumnSelection,
    TemporalDraftPoint,
    TemporalDraftPointsSelection,
    TemporalDraftRange,
    TemporalDraftRangeSelection,
    TemporalDraftRangesSelection,
    TemporalSelectionInput,
    TemporalTypedValueSelection,
)


from frisket.contracts.actions.schemas.imports import (
    ImportRowsColumn,
    ImportRowsOutput,
    ImportRowsParams,
    ImportRowsSource,
)


from frisket.contracts.actions.schemas.media import (
    OCR_SYMBOLIC_ENGINES,
    TO_MARKDOWN_SYMBOLIC_ENGINES,
    TRANSCRIBE_SYMBOLIC_ENGINES,
    transcribe_diarization_mode,
    transcribe_engine_capabilities,
    transcribe_max_speakers,
    transcribe_speaker_hint,
    transcribe_supports_diarization,
)
from frisket.contracts.actions.schemas._engines import (
    project_transcription_engine_options,
)
from frisket.contracts.actions.schemas._language import (
    AUTO_SENTINEL,
    LanguageChoice,
    LanguageDeclaration,
    canonicalize_languages,
)


__all__ = [
    "ACTION_CATALOG_SCHEMA_VERSION",
    "ACTION_ERROR_SCHEMA_VERSION",
    "ACTION_RESULT_SCHEMA_VERSION",
    "ACTION_SCHEMA_VERSION",
    "ACTION_VALIDATION_SCHEMA_VERSION",
    "CURRENT_ACTION_AUTHORING_CONTRACT_VERSION",
    "ActionAuthoringContractVersion",
    "ActionCatalog",
    "ActionCatalogEntry",
    "ActionError",
    "ActionErrorSpec",
    "ActionIdentity",
    "ActionOutput",
    "ActionResult",
    "ActionResultStatus",
    "ActionSpec",
    "ActionValidationResult",
    "AllRowsSelector",
    "AsyncMode",
    "ContractModel",
    "CostPolicy",
    "CostPolicyKind",
    "EvidenceRetention",
    "ExecutionMode",
    "IdempotencyPolicy",
    "MAX_CELL_EDITS",
    "MAX_IMPORT_ROWS_COLUMNS",
    "RECEIPT_SCHEMA_VERSION",
    "Receipt",
    "ReceiptEvidence",
    "ReceiptIO",
    "RetryPolicy",
    "ExactMembershipSelector",
    "ExactRowMembership",
    "SheetRowScope",
    "SheetRowScopePolicy",
    "StrictPositiveInt",
    "StrictString",
    "V1_COLUMN_TYPE_ALIASES",
    "_idempotency_stale_running_error",
    "_is_json_value",
    "_is_v1_column_type",
    "_value_matches_column_type",
    "canonical_column_type",
    "TemporalColumnSelection",
    "TemporalDraftPoint",
    "TemporalDraftPointsSelection",
    "TemporalDraftRange",
    "TemporalDraftRangeSelection",
    "TemporalDraftRangesSelection",
    "TemporalSelectionInput",
    "TemporalTypedValueSelection",
    "ImportRowsColumn",
    "ImportRowsOutput",
    "ImportRowsParams",
    "ImportRowsSource",
    "column_template_names",
    "AUTO_SENTINEL",
    "LanguageChoice",
    "LanguageDeclaration",
    "canonicalize_languages",
    "project_transcription_engine_options",
    "transcribe_diarization_mode",
    "transcribe_engine_capabilities",
    "transcribe_max_speakers",
    "transcribe_speaker_hint",
    "transcribe_supports_diarization",
    "OCR_SYMBOLIC_ENGINES",
    "TO_MARKDOWN_SYMBOLIC_ENGINES",
    "TRANSCRIBE_SYMBOLIC_ENGINES",
]
