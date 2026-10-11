"""Solo-only local routes for the installation Cloud connection."""

from __future__ import annotations

from fastapi import FastAPI

from frisket.contracts.http.cloud_connection import (
    CloudConnectionPreferencesPatch,
    CloudConnectionPreferencesResponse,
    CloudConnectionResponse,
    CloudConnectionSignOutResponse,
    CloudDeviceAuthorizationCancelResponse,
    CloudDeviceAuthorizationPollResponse,
    CloudDeviceAuthorizationStartResponse,
    ProjectCloudRoutingPatch,
    ProjectCloudRoutingResponse,
)
from frisket.server.route_errors import http_error_responses
from frisket.server.services.cloud_connection import (
    CloudConnectionService,
    CloudConnectionServiceError,
)


def register_cloud_connection_routes(
    app: FastAPI, *, service: CloudConnectionService
) -> None:
    @app.exception_handler(CloudConnectionServiceError)
    async def cloud_connection_error(_request, exc: CloudConnectionServiceError):
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=exc.status, content={"detail": str(exc)})

    @app.get(
        "/api/cloud-connection",
        response_model=CloudConnectionResponse,
        responses=http_error_responses(500),
    )
    def get_cloud_connection() -> CloudConnectionResponse:
        return service.connection()

    @app.delete(
        "/api/cloud-connection",
        response_model=CloudConnectionSignOutResponse,
        responses=http_error_responses(500, 503),
    )
    def delete_cloud_connection() -> CloudConnectionSignOutResponse:
        return service.sign_out()

    @app.post(
        "/api/cloud-connection/device-authorizations",
        response_model=CloudDeviceAuthorizationStartResponse,
        responses=http_error_responses(503),
    )
    def begin_cloud_authorization() -> CloudDeviceAuthorizationStartResponse:
        return service.begin_authorization()

    @app.get(
        "/api/cloud-connection/device-authorizations/{authorization_id}",
        response_model=CloudDeviceAuthorizationPollResponse,
        responses=http_error_responses(404, 503),
    )
    def poll_cloud_authorization(
        authorization_id: str,
    ) -> CloudDeviceAuthorizationPollResponse:
        return service.poll_authorization(authorization_id)

    @app.delete(
        "/api/cloud-connection/device-authorizations/{authorization_id}",
        response_model=CloudDeviceAuthorizationCancelResponse,
        responses=http_error_responses(500),
    )
    def cancel_cloud_authorization(
        authorization_id: str,
    ) -> CloudDeviceAuthorizationCancelResponse:
        return CloudDeviceAuthorizationCancelResponse(
            cancelled=service.cancel_authorization(authorization_id)
        )

    @app.get(
        "/api/cloud-connection/preferences",
        response_model=CloudConnectionPreferencesResponse,
        responses=http_error_responses(500),
    )
    def get_cloud_preferences() -> CloudConnectionPreferencesResponse:
        return service.preferences()

    @app.patch(
        "/api/cloud-connection/preferences",
        response_model=CloudConnectionPreferencesResponse,
        responses=http_error_responses(422, 500),
    )
    def patch_cloud_preferences(
        body: CloudConnectionPreferencesPatch,
    ) -> CloudConnectionPreferencesResponse:
        return service.set_preference(body.preference)

    @app.get(
        "/api/projects/{pid}/cloud-routing",
        response_model=ProjectCloudRoutingResponse,
        responses=http_error_responses(404, 409, 500),
    )
    def get_project_cloud_routing(pid: str) -> ProjectCloudRoutingResponse:
        return service.project_routing(pid)

    @app.patch(
        "/api/projects/{pid}/cloud-routing",
        response_model=ProjectCloudRoutingResponse,
        responses=http_error_responses(404, 409, 422, 500),
    )
    def patch_project_cloud_routing(
        pid: str, body: ProjectCloudRoutingPatch
    ) -> ProjectCloudRoutingResponse:
        return service.patch_project_routing(pid, body)

    app.router.add_event_handler("shutdown", service.close)


__all__ = ["register_cloud_connection_routes"]
