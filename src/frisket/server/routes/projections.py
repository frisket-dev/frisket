"""Generic runtime projection route registration."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict, Field

from frisket.contracts.http.runtime_projections import (
    RuntimeProjectionArtifactRequest,
    RuntimeProjectionArtifactResponse,
)
from frisket.engine.projections.runtime import (
    RuntimeProjectionBuildPlan,
    RuntimeProjectionStatus,
)
from frisket.server.route_errors import http_error_responses
from frisket.server.services.projections import (
    RuntimeProjectionService,
)


class _RuntimeProjectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    projection_kind: str = Field(alias="projectionKind", min_length=1)
    target: dict[str, Any] = Field(default_factory=dict)
    params: dict[str, Any] = Field(default_factory=dict)


class _RuntimeProjectionBuildRequest(_RuntimeProjectionRequest):
    mode: Literal["refresh", "rebuild"] = "refresh"


def register_runtime_projection_routes(
    app: FastAPI,
    *,
    service: RuntimeProjectionService,
) -> None:
    @app.post(
        "/api/projects/{pid}/projections/runtime/status",
        response_model=RuntimeProjectionStatus,
        response_model_by_alias=True,
        responses=http_error_responses(400, 401, 403, 404, 422, 500, 502),
    )
    def runtime_projection_status(
        pid: str,
        body: _RuntimeProjectionRequest,
    ) -> RuntimeProjectionStatus:
        return RuntimeProjectionStatus.model_validate(
            service.status(
                pid,
                projection_kind=body.projection_kind,
                target=body.target,
                params=body.params,
            )
        )

    @app.post(
        "/api/projects/{pid}/projections/runtime/build",
        response_model=RuntimeProjectionBuildPlan,
        response_model_by_alias=True,
        responses=http_error_responses(400, 401, 403, 404, 422, 500, 502),
    )
    def runtime_projection_build(
        pid: str,
        body: _RuntimeProjectionBuildRequest,
    ) -> RuntimeProjectionBuildPlan:
        return RuntimeProjectionBuildPlan.model_validate(
            service.build(
                pid,
                projection_kind=body.projection_kind,
                target=body.target,
                params=body.params,
                mode=body.mode,
            )
        )

    @app.post(
        "/api/projects/{pid}/projections/runtime/artifact",
        response_model=RuntimeProjectionArtifactResponse,
        responses=http_error_responses(400, 401, 403, 404, 422, 500, 502),
    )
    def runtime_projection_artifact(
        pid: str,
        body: RuntimeProjectionArtifactRequest,
    ) -> RuntimeProjectionArtifactResponse:
        return RuntimeProjectionArtifactResponse.model_validate(
            service.artifact(
                pid,
                projection_kind=body.projection_kind,
                artifact_id=body.artifact_id,
                target=body.target,
                params=body.params,
            )
        )
