"""Generic runtime projection route services."""

from __future__ import annotations

from typing import Any, Literal

from frisket.engine.projections.runtime import (
    ProjectionRuntimeError,
    project_projection_runtime_binding,
    runtime_projection_build,
    runtime_projection_status,
)
from frisket.server.workspace import Workspace
from frisket.authoring.workbench.plugin_subprocess_projections import (
    read_plugin_projection_artifact,
)
from frisket.server.route_errors import RouteError


class RuntimeProjectionRouteError(RouteError):
    pass


class RuntimeProjectionService:
    def __init__(self, workspace: Workspace):
        self._workspace = workspace

    def status(
        self,
        project_id: str,
        *,
        projection_kind: str,
        target: dict[str, Any],
        params: dict[str, Any],
    ) -> dict[str, Any]:
        project = self._workspace.get(project_id)
        try:
            status = runtime_projection_status(
                project,
                projection_kind=projection_kind,
                project_id=project_id,
                target=target,
                params=params,
            )
        except ProjectionRuntimeError as exc:
            raise RuntimeProjectionRouteError(
                _runtime_projection_status_code(exc),
                _runtime_projection_error_detail(exc),
            ) from exc
        return status.model_dump(mode="json", by_alias=True)

    def build(
        self,
        project_id: str,
        *,
        projection_kind: str,
        target: dict[str, Any],
        params: dict[str, Any],
        mode: Literal["refresh", "rebuild"],
    ) -> dict[str, Any]:
        project = self._workspace.get(project_id)
        try:
            build = runtime_projection_build(
                project,
                projection_kind=projection_kind,
                project_id=project_id,
                target=target,
                params=params,
                mode=mode,
            )
        except ProjectionRuntimeError as exc:
            raise RuntimeProjectionRouteError(
                _runtime_projection_status_code(exc),
                _runtime_projection_error_detail(exc),
            ) from exc
        return build.model_dump(mode="json", by_alias=True)

    def artifact(
        self,
        project_id: str,
        *,
        projection_kind: str,
        artifact_id: str,
        target: dict[str, Any],
        params: dict[str, Any],
    ) -> dict[str, Any]:
        project = self._workspace.get(project_id)
        if project_projection_runtime_binding(project, projection_kind) is None:
            raise RuntimeProjectionRouteError(
                404,
                {
                    "code": "unsupported_runtime_projection",
                    "message": "No trusted projection runtime binding exists",
                    "projection_kind": projection_kind,
                    "field": "projectionKind",
                },
            )
        try:
            return read_plugin_projection_artifact(
                project,
                projection_kind=projection_kind,
                artifact_id=artifact_id,
                target=target,
                params=params,
            )
        except ProjectionRuntimeError as exc:
            raise RuntimeProjectionRouteError(
                _runtime_projection_status_code(exc),
                _runtime_projection_error_detail(exc),
            ) from exc


def _runtime_projection_error_detail(exc: ProjectionRuntimeError) -> dict[str, Any]:
    detail: dict[str, Any] = {"code": exc.code, "message": exc.message}
    if exc.projection_kind is not None:
        detail["projection_kind"] = exc.projection_kind
    if exc.field is not None:
        detail["field"] = exc.field
    return detail


def _runtime_projection_status_code(exc: ProjectionRuntimeError) -> int:
    if exc.code == "unsupported_runtime_projection":
        return 404
    if exc.code.endswith("_missing"):
        return 404
    if exc.code.startswith("invalid_runtime_projection"):
        return 400
    return 502
