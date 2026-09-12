"""Typed request and response models for the project selector facade.

The selector response is a read projection.  Authored values remain the action,
Copilot, or embedding contract's values; ``choice_id`` is only a stable UI key.
Setup descriptors carry effective capabilities so browser code never derives
authority from roles, route presence, or labels.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, JsonValue

from frisket.contracts.http.models import WireModel


SELECTOR_CHOICES_QUERY_SCHEMA_VERSION = "frisket.selector_choices_query.v1"
SELECTOR_CHOICES_SCHEMA_VERSION = "frisket.selector_choices.v1"


class ActionSelectorSubject(WireModel):
    kind: Literal["action"]
    action_id: str = Field(min_length=1)
    field: str = Field(min_length=1)
    params: dict[str, JsonValue] = Field(default_factory=dict)


class CopilotSelectorSubject(WireModel):
    kind: Literal["copilot"]
    model: str | None = None


class EmbeddingSelectorSubject(WireModel):
    kind: Literal["embedding"]
    provider: str | None = None
    model: str | None = None
    modality: str | None = None
    source_column_type: str | None = None


SelectorSubject = Annotated[
    ActionSelectorSubject | CopilotSelectorSubject | EmbeddingSelectorSubject,
    Field(discriminator="kind"),
]


class SelectorChoicesQuery(WireModel):
    schema_version: Literal["frisket.selector_choices_query.v1"]
    subject: SelectorSubject


class NormalizedActionSelectorSubject(WireModel):
    kind: Literal["action"]
    action_id: str
    field: str


class NormalizedCopilotSelectorSubject(WireModel):
    kind: Literal["copilot"]


class NormalizedEmbeddingSelectorSubject(WireModel):
    kind: Literal["embedding"]
    modality: str | None
    source_column_type: str | None


NormalizedSelectorSubject = Annotated[
    NormalizedActionSelectorSubject
    | NormalizedCopilotSelectorSubject
    | NormalizedEmbeddingSelectorSubject,
    Field(discriminator="kind"),
]


class EngineSelection(WireModel):
    kind: Literal["engine"]
    engine: str


class ModelSelection(WireModel):
    kind: Literal["model"]
    model: str


class EngineModelSelection(WireModel):
    kind: Literal["engine_model"]
    engine: str
    model: str | None


class EmbeddingSelection(WireModel):
    kind: Literal["embedding"]
    provider: str
    model: str


AuthoredSelection = Annotated[
    EngineSelection | ModelSelection | EngineModelSelection | EmbeddingSelection,
    Field(discriminator="kind"),
]


class SelectorTextFact(WireModel):
    kind: Literal["text"]
    label: str
    value: str


class SelectorListFact(WireModel):
    kind: Literal["list"]
    label: str
    values: list[str]


class SelectorRateFact(WireModel):
    kind: Literal["rate"]
    label: str
    amount: float = Field(ge=0)
    currency: Literal["USD"] = "USD"
    unit: str
    source_url: str | None = None
    updated: str | None = None


SelectorFact = Annotated[
    SelectorTextFact | SelectorListFact | SelectorRateFact,
    Field(discriminator="kind"),
]


class SelectorResolvedTarget(WireModel):
    """The active route only. Other viable targets never unlock a choice."""

    target_id: str
    operator: str | None
    egress_class: str | None


class SelectorProcessingDestination(WireModel):
    kind: Literal["local", "operator_network", "external", "unknown"]
    label: str


class SelectorBlocker(WireModel):
    code: str
    message: str
    field: str | None = None


class SelectorSetupScope(WireModel):
    scope: Literal["workspace", "project", "organization", "environment"]
    configured: bool
    can_mutate: bool
    source: Literal["missing", "environment", "workspace", "project", "organization"]


class SelectorOperationCapabilities(WireModel):
    cancel: bool
    retry: bool
    remove: bool


class SelectorActiveOperation(WireModel):
    operation_id: int
    operation_kind: Literal["artifact", "local_model", "engine_setup"]
    display_name: str
    status: Literal[
        "queued", "running", "completed", "failed", "cancel_requested", "cancelled"
    ]
    phase: str | None
    completed_bytes: int | None = Field(default=None, ge=0)
    total_bytes: int | None = Field(default=None, ge=0)
    error: SelectorBlocker | None
    capabilities: SelectorOperationCapabilities


class ApiKeySetup(WireModel):
    kind: Literal["api_key"]
    provider: str
    scopes: list[SelectorSetupScope]


class ModelsGatewaySetup(WireModel):
    kind: Literal["models_gateway"]
    scopes: list[SelectorSetupScope]


class ArtifactDownloadSetup(WireModel):
    kind: Literal["artifact_download"]
    setup_ref: str
    scope: Literal["workspace", "organization"]
    can_mutate: bool
    can_start: bool
    blocked_by_operation: int | None


class EngineSetup(WireModel):
    kind: Literal["engine_setup"]
    setup_ref: str
    scope: Literal["workspace", "organization"]
    can_mutate: bool
    can_start: bool
    blocked_by_operation: int | None


class FirstUseDownloadSetup(WireModel):
    kind: Literal["first_use_download"]
    disclosure: str


class InstructionsSetup(WireModel):
    kind: Literal["instructions"]
    title: str
    steps: list[str]
    url: str | None = None


SelectorSetup = Annotated[
    ApiKeySetup
    | ModelsGatewaySetup
    | ArtifactDownloadSetup
    | EngineSetup
    | FirstUseDownloadSetup
    | InstructionsSetup,
    Field(discriminator="kind"),
]


class SelectorChoice(WireModel):
    choice_id: str
    label: str
    summary: str
    description: str
    model_card_url: str | None
    authored_selection: AuthoredSelection
    resolved_target: SelectorResolvedTarget | None
    processing_destination: SelectorProcessingDestination
    facts: list[SelectorFact]
    status: Literal["ready", "needs_setup", "working", "unavailable"]
    can_author: bool
    can_run: bool
    blocker: SelectorBlocker | None
    setup: SelectorSetup | None
    active_operation: SelectorActiveOperation | None
    is_default: bool
    is_current: bool


class SelectorChoiceGroup(WireModel):
    group_id: str
    kind: Literal["local", "server", "provider"]
    label: str
    status: Literal["ready", "needs_setup", "working", "unavailable"]
    choices: list[SelectorChoice]


class SelectorChoicesResponse(WireModel):
    schema_version: Literal["frisket.selector_choices.v1"]
    project_id: str
    subject: NormalizedSelectorSubject
    depends_on: list[str]
    current_choice_id: str | None
    default_choice_id: str | None
    groups: list[SelectorChoiceGroup]
    orphaned_current: SelectorChoice | None


__all__ = [
    "SELECTOR_CHOICES_QUERY_SCHEMA_VERSION",
    "SELECTOR_CHOICES_SCHEMA_VERSION",
    "ActionSelectorSubject",
    "ApiKeySetup",
    "ArtifactDownloadSetup",
    "AuthoredSelection",
    "CopilotSelectorSubject",
    "EmbeddingSelectorSubject",
    "EmbeddingSelection",
    "EngineModelSelection",
    "EngineSelection",
    "EngineSetup",
    "FirstUseDownloadSetup",
    "InstructionsSetup",
    "ModelSelection",
    "ModelsGatewaySetup",
    "NormalizedSelectorSubject",
    "SelectorActiveOperation",
    "SelectorBlocker",
    "SelectorChoice",
    "SelectorChoiceGroup",
    "SelectorChoicesQuery",
    "SelectorChoicesResponse",
    "SelectorFact",
    "SelectorOperationCapabilities",
    "SelectorProcessingDestination",
    "SelectorResolvedTarget",
    "SelectorSetup",
    "SelectorSetupScope",
]
