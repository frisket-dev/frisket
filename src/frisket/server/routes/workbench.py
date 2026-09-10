"""Workbench plugin route registration."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response

from frisket.contracts.http.models import (
    WorkbenchPluginActivation,
    WorkbenchPluginActivationRequest,
    WorkbenchPluginBackendActivation,
    WorkbenchPluginBackendActivationRequest,
    WorkbenchPluginInstallExecution,
    WorkbenchPluginInstallState,
    WorkbenchPluginLocalInstallRequest,
    WorkbenchPluginRuntimeIndex,
    WorkbenchPluginSettings,
    WorkbenchPluginSettingsPatchRequest,
)
from frisket.server import schemas
from frisket.server.route_errors import http_error_responses, register_typed_error
from frisket.server.services.workbench import WorkbenchService
from frisket.authoring.workbench.plugin_runtime_shared import (
    WorkbenchPluginActivationError,
    WorkbenchPluginLifecycleError,
)


def _workbench_error_detail(
    exc: WorkbenchPluginActivationError | WorkbenchPluginLifecycleError,
) -> dict[str, Any]:
    detail: dict[str, Any] = {"code": exc.code, "message": exc.message}
    details = getattr(exc, "details", None)
    if isinstance(details, dict) and details:
        detail["details"] = details
    return detail


def _plugin_env_body_error() -> HTTPException:
    return HTTPException(
        400,
        {
            "code": "plugin_env_body_invalid",
            "message": "plugin env var requests require string name and value fields",
        },
    )


def register_workbench_routes(
    app: FastAPI,
    *,
    service: WorkbenchService,
    nonbundled_enabled: bool = True,
) -> None:
    """Register the workbench plugin routes.

    ``nonbundled_enabled=False`` (a selected-bundle-only composition) omits
    every configuration and lifecycle endpoint. The selected reviewed bundles
    expose only the runtime index and admitted frontend component modules.
    """
    register_typed_error(
        app,
        (WorkbenchPluginActivationError, WorkbenchPluginLifecycleError),
        detail=_workbench_error_detail,
    )

    @app.get(
        "/api/projects/{pid}/workbench/plugins",
        response_model=WorkbenchPluginRuntimeIndex,
        response_model_exclude_unset=True,
        responses=http_error_responses(401, 403, 404, 409, 500),
    )
    def workbench_plugins(pid: str) -> WorkbenchPluginRuntimeIndex:
        return WorkbenchPluginRuntimeIndex.model_validate(service.plugin_index(pid))

    @app.get(
        "/api/projects/{pid}/workbench/plugins/{plugin_id}"
        "/frontend-components/{contribution_id}/module.js"
    )
    def workbench_plugin_frontend_component_module_route(
        pid: str,
        plugin_id: str,
        contribution_id: str,
        package_sha256: str | None = Query(default=None, alias="package"),
    ) -> Response:
        source = service.frontend_component_module(
            pid,
            plugin_id=plugin_id,
            contribution_id=contribution_id,
            expected_package_sha256=package_sha256,
        )
        return Response(
            source,
            media_type="application/javascript",
            headers={
                "Cache-Control": "no-store",
                "X-Frisket-Plugin-Frontend-Module": "trusted-local",
            },
        )

    if not nonbundled_enabled:
        return

    @app.get("/api/projects/{pid}/workbench/plugins/{plugin_id}/env")
    def workbench_plugin_env_vars(pid: str, plugin_id: str) -> dict:
        return service.plugin_env_vars(pid, plugin_id=plugin_id)

    @app.get(
        "/api/projects/{pid}/workbench/plugins/{plugin_id}/settings",
        response_model=WorkbenchPluginSettings,
        response_model_exclude_unset=True,
        responses=http_error_responses(401, 403, 404, 409, 422, 500),
    )
    def workbench_plugin_settings(pid: str, plugin_id: str) -> WorkbenchPluginSettings:
        return WorkbenchPluginSettings.model_validate(
            service.plugin_settings(pid, plugin_id=plugin_id)
        )

    @app.post("/api/projects/{pid}/workbench/plugins/{plugin_id}/env")
    async def set_workbench_plugin_env_var_route(
        pid: str, plugin_id: str, request: Request
    ) -> dict:
        try:
            body = await request.json()
        except Exception as exc:
            raise _plugin_env_body_error() from exc
        if not isinstance(body, dict):
            raise _plugin_env_body_error()
        name = body.get("name")
        value = body.get("value")
        if not isinstance(name, str) or not isinstance(value, str):
            raise _plugin_env_body_error()
        return service.set_plugin_env_var(
            pid,
            plugin_id=plugin_id,
            name=name,
            value=value,
        )

    @app.delete("/api/projects/{pid}/workbench/plugins/{plugin_id}/env/{name}")
    def delete_workbench_plugin_env_var_route(
        pid: str, plugin_id: str, name: str
    ) -> dict:
        return service.delete_plugin_env_var(
            pid,
            plugin_id=plugin_id,
            name=name,
        )

    @app.patch(
        "/api/projects/{pid}/workbench/plugins/{plugin_id}/settings",
        response_model=WorkbenchPluginSettings,
        response_model_exclude_unset=True,
        responses=http_error_responses(401, 403, 404, 409, 422, 500),
    )
    def patch_workbench_plugin_settings(
        pid: str,
        plugin_id: str,
        body: WorkbenchPluginSettingsPatchRequest,
    ) -> WorkbenchPluginSettings:
        return WorkbenchPluginSettings.model_validate(
            service.patch_plugin_settings(
                pid,
                plugin_id=plugin_id,
                values=body.values,
            )
        )

    if nonbundled_enabled:

        @app.get("/api/projects/{pid}/workbench/marketplace")
        def workbench_marketplace(
            pid: str,
            contribution_id: str = Query(...),
            query: str = Query(""),
        ) -> dict:
            return service.marketplace(pid, contribution_id=contribution_id, text=query)

        @app.post("/api/projects/{pid}/workbench/marketplace/install-attempt")
        def workbench_marketplace_install_attempt_route(
            pid: str, body: schemas.WorkbenchMarketplaceInstallAttemptBody
        ) -> JSONResponse:
            status_code, payload = service.marketplace_install_attempt(
                pid,
                plugin_id=body.pluginId,
                version=body.version,
                source=body.source,
                arbitrary_package_load_allowed=body.arbitraryPackageLoadAllowed,
            )
            return JSONResponse(payload, status_code=status_code)

        @app.post(
            "/api/projects/{pid}/workbench/plugins/{plugin_id}/install-local",
            response_model=WorkbenchPluginInstallExecution,
            response_model_exclude_unset=True,
            responses={
                403: {"model": WorkbenchPluginInstallExecution},
                409: {"model": WorkbenchPluginInstallExecution},
                **http_error_responses(401, 422, 500),
            },
        )
        def workbench_plugin_local_install_route(
            pid: str, plugin_id: str, body: WorkbenchPluginLocalInstallRequest
        ) -> WorkbenchPluginInstallExecution | JSONResponse:
            status_code, payload = service.install_local(
                pid,
                plugin_id=plugin_id,
                source=body.source,
                arbitrary_package_load_allowed=body.arbitraryPackageLoadAllowed,
            )
            if status_code != 200:
                return JSONResponse(payload, status_code=status_code)
            return WorkbenchPluginInstallExecution.model_validate(payload)

    @app.post(
        "/api/projects/{pid}/workbench/plugins/{plugin_id}/activate",
        response_model=WorkbenchPluginActivation,
        response_model_exclude_unset=True,
        responses=http_error_responses(401, 400, 403, 404, 409, 422, 500),
    )
    def activate_workbench_plugin(
        pid: str, plugin_id: str, body: WorkbenchPluginActivationRequest
    ) -> WorkbenchPluginActivation:
        return WorkbenchPluginActivation.model_validate(
            service.activate_manifest(
                pid,
                plugin_id=plugin_id,
                receipt_id=body.receiptId,
                trust_acknowledged=body.trustAcknowledged,
                permissions_accepted=body.permissionsAccepted,
                arbitrary_package_load_allowed=body.arbitraryPackageLoadAllowed,
            )
        )

    @app.post(
        "/api/projects/{pid}/workbench/plugins/{plugin_id}/backend/activate",
        response_model=WorkbenchPluginBackendActivation,
        response_model_exclude_unset=True,
        responses=http_error_responses(401, 403, 409, 422, 500),
    )
    def activate_workbench_plugin_backend(
        pid: str, plugin_id: str, body: WorkbenchPluginBackendActivationRequest
    ) -> WorkbenchPluginBackendActivation:
        return WorkbenchPluginBackendActivation.model_validate(
            service.activate_backend(
                pid,
                plugin_id=plugin_id,
                trust_acknowledged=body.trustAcknowledged,
                arbitrary_package_load_allowed=body.arbitraryPackageLoadAllowed,
                executable_handlers_allowed=body.executableHandlersAllowed,
            )
        )

    @app.post(
        "/api/projects/{pid}/workbench/plugins/{plugin_id}/disable",
        response_model=WorkbenchPluginInstallState,
        response_model_exclude_unset=True,
        responses=http_error_responses(401, 403, 404, 409, 422, 500),
    )
    def disable_workbench_plugin_route(
        pid: str, plugin_id: str
    ) -> WorkbenchPluginInstallState:
        return WorkbenchPluginInstallState.model_validate(
            service.disable(pid, plugin_id=plugin_id)
        )

    @app.post(
        "/api/projects/{pid}/workbench/plugins/{plugin_id}/uninstall",
        response_model=WorkbenchPluginInstallState,
        response_model_exclude_unset=True,
        responses=http_error_responses(401, 403, 404, 409, 422, 500),
    )
    def uninstall_workbench_plugin_route(
        pid: str, plugin_id: str
    ) -> WorkbenchPluginInstallState:
        return WorkbenchPluginInstallState.model_validate(
            service.uninstall(pid, plugin_id=plugin_id)
        )
