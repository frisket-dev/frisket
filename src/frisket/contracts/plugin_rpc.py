"""Pure contract authority for the trusted-local plugin RPC boundary.

The models in this module describe messages only. Process creation, framing,
sandbox enforcement and domain validation belong to the runtime
owners that consume this contract. Streaming modes expose one frame at a time;
no contract type requires a materialized importer or projection stream.
The transport is intentionally unversioned because host and child ship from the
same Frisket build. Nested persisted or domain-owned payloads may still carry
their own schema versions.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Annotated, Any, Final, Literal, Union

from pydantic import BaseModel, ConfigDict, Field

RUNTIME_OPERATOR_PLAN_SCHEMA_VERSION: Final = "frisket.runtime_operator_plan.v1"

# Bounds are protocol facts, not transport implementation details.
PLUGIN_ACTION_WALL_SECONDS = 60


@dataclass(frozen=True, slots=True)
class PluginInvocationSemantics:
    """Delegation contract for the PLUGIN-02 process client.

    The sandbox supervisor enforces ``wall_seconds`` and kills the complete
    process tree when an invocation is cancelled or times out. A process is
    never pooled: every invocation receives one fresh child.
    """

    wall_seconds: int = PLUGIN_ACTION_WALL_SECONDS
    kill_process_tree_on_cancel: bool = True
    one_process_per_invocation: bool = True


PLUGIN_INVOCATION_SEMANTICS: Final = PluginInvocationSemantics()


class PluginRpcModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", populate_by_name=True, hide_input_in_errors=True
    )


class PluginErrorDetails(PluginRpcModel):
    """Allowlisted child error details; deprecated 429 duck typing is absent."""

    retryable: bool | None = None
    retry_after_ms: int | None = Field(default=None, ge=0)
    debug_traceback: str | None = None
    diagnostics: list[dict[str, Any]] | None = None


class StructuredPluginError(PluginRpcModel):
    code: str
    message: str
    details: PluginErrorDetails | None = None


class PluginImporterCapabilityContext(PluginRpcModel):
    project_id: str = Field(alias="projectId")
    plugin_id: str = Field(alias="pluginId")
    handler_key: str = Field(alias="handlerKey")
    importer_kind: str = Field(alias="importerKind")
    capabilities: tuple[Literal["project:write"], ...]
    # Values travel only in the supervised child's stdin request, never its
    # environment.  Hiding the field from repr prevents accidental diagnostic
    # disclosure while leaving it present in the typed wire payload.
    requires_secrets: tuple[str, ...] = Field(default=(), alias="requiresSecrets")
    secret_values: dict[str, str] = Field(
        default_factory=dict, alias="secretValues", repr=False
    )


class PluginOperatorCapabilityContext(PluginRpcModel):
    project_id: str = Field(alias="projectId")
    plugin_id: str = Field(alias="pluginId")
    handler_key: str = Field(alias="handlerKey")
    operator_kind: str = Field(alias="operatorKind")
    capabilities: tuple[Literal["operator.filter"], ...]
    requires_secrets: tuple[str, ...] = Field(default=(), alias="requiresSecrets")
    secret_values: dict[str, str] = Field(
        default_factory=dict, alias="secretValues", repr=False
    )


type ProjectionBuildCapabilities = tuple[
    Literal["projection.build"], Literal["projection.artifact.write"]
]
type ProjectionPlanCapabilities = (
    tuple[Literal["projection.build"]] | tuple[Literal["projection.status"]]
)


class PluginProjectionCapabilityContext(PluginRpcModel):
    """Projection authority stays split between build and plan/status posture."""

    project_id: str = Field(alias="projectId")
    plugin_id: str = Field(alias="pluginId")
    handler_key: str = Field(alias="handlerKey")
    projection_kind: str = Field(alias="projectionKind")
    capabilities: ProjectionBuildCapabilities | ProjectionPlanCapabilities
    requires_secrets: tuple[str, ...] = Field(default=(), alias="requiresSecrets")
    secret_values: dict[str, str] = Field(
        default_factory=dict, alias="secretValues", repr=False
    )


class OperatorInputRow(PluginRpcModel):
    row_id: int | str = Field(alias="rowId")
    value: Any = None


class ImporterRequest(PluginRpcModel):
    plugin_id: str = Field(alias="pluginId")
    handler_key: str = Field(alias="handlerKey")
    importer_kind: str = Field(alias="importerKind")
    module_path: str = Field(alias="modulePath")
    source: dict[str, Any]
    handler_params: dict[str, Any] = Field(default_factory=dict, alias="handlerParams")
    context: PluginImporterCapabilityContext


class ProjectionRequest(PluginRpcModel):
    plugin_id: str = Field(alias="pluginId")
    handler_key: str = Field(alias="handlerKey")
    projection_kind: str = Field(alias="projectionKind")
    module_path: str = Field(alias="modulePath")
    operation: Literal["timeline", "status", "build"] = "timeline"
    target: dict[str, Any] = Field(default_factory=dict)
    params: dict[str, Any] = Field(default_factory=dict)
    context: PluginProjectionCapabilityContext
    rows: list[dict[str, Any]] = Field(default_factory=list)


class OperatorRequest(PluginRpcModel):
    plugin_id: str = Field(alias="pluginId")
    handler_key: str = Field(alias="handlerKey")
    operator_kind: str = Field(alias="operatorKind")
    module_path: str = Field(alias="modulePath")
    target: dict[str, Any] = Field(default_factory=dict)
    params: dict[str, Any] = Field(default_factory=dict)
    value: Any = None
    context: PluginOperatorCapabilityContext
    rows: list[OperatorInputRow] = Field(default_factory=list)


class TableSchemaFrame(PluginRpcModel):
    type: Literal["schema"] = "schema"
    columns: list[dict[str, Any]]


class TableRowFrame(PluginRpcModel):
    type: Literal["row"] = "row"
    row: dict[str, Any]


class ImporterDiagnosticFrame(PluginRpcModel):
    type: Literal["diagnostic"]
    diagnostic: dict[str, Any]


class TableErrorFrame(PluginRpcModel):
    type: Literal["error"] = "error"
    error: StructuredPluginError


class TableDoneFrame(PluginRpcModel):
    type: Literal["done"] = "done"
    row_count: int = Field(ge=0)
    warnings: list[str] = Field(default_factory=list)


ImporterFrame = Annotated[
    Union[
        TableSchemaFrame,
        TableRowFrame,
        ImporterDiagnosticFrame,
        TableErrorFrame,
        TableDoneFrame,
    ],
    Field(discriminator="type"),
]


class ProjectionTimelineItemFrame(PluginRpcModel):
    type: Literal["timeline_item"]
    item: dict[str, Any]


class ProjectionPlanFrame(PluginRpcModel):
    type: Literal["plan"]
    plan: dict[str, Any]


class ProjectionErrorFrame(PluginRpcModel):
    type: Literal["error"]
    error: StructuredPluginError


class ProjectionDoneFrame(PluginRpcModel):
    type: Literal["done"]
    metrics: dict[str, Any] = Field(default_factory=dict)


ProjectionFrame = Annotated[
    Union[
        ProjectionTimelineItemFrame,
        ProjectionPlanFrame,
        ProjectionErrorFrame,
        ProjectionDoneFrame,
    ],
    Field(discriminator="type"),
]


class RuntimeOperatorPlan(PluginRpcModel):
    schema_version: Literal[RUNTIME_OPERATOR_PLAN_SCHEMA_VERSION] = Field(
        default=RUNTIME_OPERATOR_PLAN_SCHEMA_VERSION,
        alias="schemaVersion",
    )
    row_ids: list[int] = Field(alias="rowIds")


class OperatorResponse(PluginRpcModel):
    mode: Literal["operator"]
    status: Literal["completed", "failed"]
    plan: RuntimeOperatorPlan | None
    errors: list[StructuredPluginError] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


# Iterator aliases make incremental consumption part of the public contract.
# In particular, no stream alias can be satisfied only by collecting all frames.
type ImporterFrameStream = Iterator[ImporterFrame]
type ProjectionFrameStream = Iterator[ProjectionFrame]
