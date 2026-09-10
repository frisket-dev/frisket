"""Trusted projection runtime binding contract.

This module defines the host-owned request/response boundary for projection
runtime bindings. Projection-specific adapters decide when to consult this
contract while keeping host-owned project and sidecar mutation boundaries.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from frisket.authoring.plugin_registry import RuntimeBindingSpec, default_registry
from frisket.engine.store import Project


STATUS_REQUEST_SCHEMA = "frisket.runtime_projection_status_request.v1"
BUILD_REQUEST_SCHEMA = "frisket.runtime_projection_build_request.v1"
STATUS_SCHEMA = "frisket.runtime_projection_status.v1"
BUILD_PLAN_SCHEMA = "frisket.runtime_projection_build_plan.v1"

MAX_RUNTIME_PROJECTION_JSON_BYTES = 100_000


class ProjectionRuntimeError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        projection_kind: str | None = None,
        field: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.projection_kind = projection_kind
        self.field = field


class _StrictRuntimeModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        json_schema_serialization_defaults_required=True,
    )


class RuntimeProjectionFreshness(_StrictRuntimeModel):
    state: Literal["fresh", "stale", "missing", "transient", "failed"]
    generation: str | None = None
    transient: bool = False


class RuntimeProjectionArtifactRef(_StrictRuntimeModel):
    kind: Literal["projection_artifact"]
    projection_kind: str = Field(alias="projectionKind", min_length=1)
    artifact_id: str = Field(alias="artifactId", min_length=1)

    @field_validator("artifact_id")
    @classmethod
    def _reject_raw_paths(cls, value: str) -> str:
        lowered = value.lower()
        if lowered.startswith(("/", "file:", "sqlite:", "sql:")):
            raise ValueError("invalid_runtime_projection_artifact")
        return value


class RuntimeProjectionOutputs(_StrictRuntimeModel):
    artifact_refs: list[RuntimeProjectionArtifactRef] = Field(
        default_factory=list, alias="artifactRefs"
    )
    metrics: dict[str, int | float | str | bool | None] = Field(default_factory=dict)

    @field_validator("metrics")
    @classmethod
    def _validate_metrics(
        cls, metrics: dict[str, int | float | str | bool | None]
    ) -> dict[str, int | float | str | bool | None]:
        _assert_bounded_json(metrics, field="outputs.metrics")
        return metrics


class RuntimeProjectionStatus(_StrictRuntimeModel):
    schema_version: Literal[STATUS_SCHEMA] = Field(alias="schemaVersion")
    status: Literal["ready", "stale", "missing", "building", "failed"]
    freshness: RuntimeProjectionFreshness
    outputs: RuntimeProjectionOutputs = Field(default_factory=RuntimeProjectionOutputs)
    warnings: list[str] = Field(default_factory=list)

    @field_validator("warnings")
    @classmethod
    def _validate_warnings(cls, warnings: list[str]) -> list[str]:
        _assert_bounded_json(warnings, field="warnings")
        return warnings


class RuntimeProjectionBuild(_StrictRuntimeModel):
    operation: Literal["refresh", "rebuild", "noop"]
    idempotency_key: str = Field(alias="idempotencyKey", min_length=1)


class RuntimeProjectionBuildPlan(_StrictRuntimeModel):
    schema_version: Literal[BUILD_PLAN_SCHEMA] = Field(alias="schemaVersion")
    status: Literal["accepted", "noop", "failed"]
    build: RuntimeProjectionBuild
    outputs: RuntimeProjectionOutputs = Field(default_factory=RuntimeProjectionOutputs)
    warnings: list[str] = Field(default_factory=list)

    @field_validator("warnings")
    @classmethod
    def _validate_warnings(cls, warnings: list[str]) -> list[str]:
        _assert_bounded_json(warnings, field="warnings")
        return warnings

    @model_validator(mode="after")
    def _status_matches_operation(self) -> RuntimeProjectionBuildPlan:
        if self.status == "noop" and self.build.operation != "noop":
            raise ValueError("runtime_projection_noop_requires_noop_operation")
        return self


def projection_runtime_binding(projection_kind: str) -> RuntimeBindingSpec | None:
    if not projection_kind:
        return None
    for spec in default_registry().runtime_binding_specs("projections"):
        if spec.kind == projection_kind and spec.handler is not None:
            return spec
    return None


def project_projection_runtime_binding(
    project: Project, projection_kind: str
) -> RuntimeBindingSpec | None:
    from frisket.authoring.workbench.plugin_runtime_capabilities import (
        project_runtime_binding,
    )

    return project_runtime_binding(
        project, binding_type="projections", kind=projection_kind
    )


def runtime_projection_status(
    project: Project,
    *,
    projection_kind: str,
    project_id: str,
    target: dict[str, Any],
    params: dict[str, Any],
) -> RuntimeProjectionStatus:
    binding = _require_projection_binding(project, projection_kind)
    payload = _projection_request_payload(
        schema_version=STATUS_REQUEST_SCHEMA,
        binding=binding,
        projection_kind=projection_kind,
        project_id=project_id,
        target=target,
        params=params,
    )
    try:
        response = _dispatch_projection_status(project, binding, payload)
    except ProjectionRuntimeError:
        raise
    except Exception as exc:
        raise ProjectionRuntimeError(
            "runtime_projection_handler_failed",
            "Runtime projection handler failed",
            projection_kind=projection_kind,
        ) from exc
    return _parse_projection_status(response, projection_kind=projection_kind)


def runtime_projection_build(
    project: Project,
    *,
    projection_kind: str,
    project_id: str,
    target: dict[str, Any],
    params: dict[str, Any],
    mode: Literal["refresh", "rebuild"],
) -> RuntimeProjectionBuildPlan:
    if mode not in {"refresh", "rebuild"}:
        raise ProjectionRuntimeError(
            "invalid_runtime_projection_request",
            "runtime projection build mode must be refresh or rebuild",
            projection_kind=projection_kind,
            field="mode",
        )
    binding = _require_projection_binding(project, projection_kind)
    payload = _projection_request_payload(
        schema_version=BUILD_REQUEST_SCHEMA,
        binding=binding,
        projection_kind=projection_kind,
        project_id=project_id,
        target=target,
        params=params,
        mode=mode,
    )
    try:
        response = _dispatch_projection_build(project, binding, payload)
    except ProjectionRuntimeError:
        raise
    except Exception as exc:
        raise ProjectionRuntimeError(
            "runtime_projection_handler_failed",
            "Runtime projection handler failed",
            projection_kind=projection_kind,
        ) from exc
    return _parse_projection_build_plan(response, projection_kind=projection_kind)


def _dispatch_projection_status(
    project: Project, binding: RuntimeBindingSpec, payload: dict[str, Any]
) -> object:
    if binding.handler_api == "plugin_projection_subprocess":
        from frisket.authoring.workbench.plugin_subprocess_projections import (
            run_plugin_projection_status_subprocess,
        )

        return run_plugin_projection_status_subprocess(
            project,
            binding=binding,
            payload=payload,
        )
    if binding.handler is None:
        raise ProjectionRuntimeError(
            "unsupported_runtime_projection",
            "No trusted projection runtime binding exists for projection_kind",
            projection_kind=str(payload.get("projectionKind") or ""),
            field="projection_kind",
        )
    return binding.handler(payload)


def _dispatch_projection_build(
    project: Project, binding: RuntimeBindingSpec, payload: dict[str, Any]
) -> object:
    if binding.handler_api == "plugin_projection_subprocess":
        from frisket.authoring.workbench.plugin_subprocess_projections import (
            run_plugin_projection_build_subprocess,
        )

        return run_plugin_projection_build_subprocess(
            project,
            binding=binding,
            payload=payload,
        )
    if binding.handler is None:
        raise ProjectionRuntimeError(
            "unsupported_runtime_projection",
            "No trusted projection runtime binding exists for projection_kind",
            projection_kind=str(payload.get("projectionKind") or ""),
            field="projection_kind",
        )
    return binding.handler(payload)


def _require_projection_binding(
    project: Project, projection_kind: str
) -> RuntimeBindingSpec:
    binding = project_projection_runtime_binding(project, projection_kind)
    if binding is None:
        raise ProjectionRuntimeError(
            "unsupported_runtime_projection",
            "No trusted projection runtime binding exists for projection_kind",
            projection_kind=projection_kind,
            field="projection_kind",
        )
    return binding


def _projection_request_payload(
    *,
    schema_version: str,
    binding: RuntimeBindingSpec,
    projection_kind: str,
    project_id: str,
    target: dict[str, Any],
    params: dict[str, Any],
    mode: str | None = None,
) -> dict[str, Any]:
    if not isinstance(target, dict) or not isinstance(params, dict):
        raise ProjectionRuntimeError(
            "invalid_runtime_projection_request",
            "runtime projection target and params must be objects",
            projection_kind=projection_kind,
        )
    _assert_bounded_json(target, field="target")
    _assert_bounded_json(params, field="params")
    payload: dict[str, Any] = {
        "schemaVersion": schema_version,
        "projectId": project_id,
        "pluginId": binding.plugin,
        "handlerKey": binding.handler_key,
        "bindingType": binding.binding_type,
        "projectionKind": projection_kind,
        "target": target,
        "params": params,
    }
    if mode is not None:
        payload["mode"] = mode
    return payload


def _parse_projection_status(
    response: object, *, projection_kind: str
) -> RuntimeProjectionStatus:
    if not isinstance(response, dict):
        raise _invalid_status(projection_kind)
    try:
        return RuntimeProjectionStatus.model_validate(response)
    except Exception as exc:
        raise _invalid_status(projection_kind) from exc


def _parse_projection_build_plan(
    response: object, *, projection_kind: str
) -> RuntimeProjectionBuildPlan:
    if not isinstance(response, dict):
        raise _invalid_build_plan(projection_kind)
    try:
        return RuntimeProjectionBuildPlan.model_validate(response)
    except Exception as exc:
        raise _invalid_build_plan(projection_kind) from exc


def _invalid_status(projection_kind: str) -> ProjectionRuntimeError:
    return ProjectionRuntimeError(
        "invalid_runtime_projection_status",
        "trusted projection returned an invalid status response",
        projection_kind=projection_kind,
    )


def _invalid_build_plan(projection_kind: str) -> ProjectionRuntimeError:
    return ProjectionRuntimeError(
        "invalid_runtime_projection_build_plan",
        "trusted projection returned an invalid build response",
        projection_kind=projection_kind,
    )


def _assert_bounded_json(value: object, *, field: str) -> None:
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ProjectionRuntimeError(
            "invalid_runtime_projection_json",
            "runtime projection payload must be JSON serializable",
            field=field,
        ) from exc
    if len(encoded.encode("utf-8")) > MAX_RUNTIME_PROJECTION_JSON_BYTES:
        raise ProjectionRuntimeError(
            "invalid_runtime_projection_json",
            "runtime projection payload is too large",
            field=field,
        )
