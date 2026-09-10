"""Project lifecycle route registration."""

from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request

from frisket.contracts.http.graph_lineage import ProjectLineageResponse
from frisket.contracts.http.models import (
    CreateProjectRequest,
    EmptyQuery,
    Project,
    ProjectDelete,
    ProjectDeleteRequest,
    ProjectList,
    Sheet,
    SheetDeleteResponse,
    SheetList,
    UpdateProjectRequest,
    UpdateProjectSensitivityRequest,
    UpdateSheetRequest,
)
from frisket.contracts.http.organization_providers import OrganizationProviderCatalog
from frisket.contracts.http.project_config import (
    ProjectCompactResponse,
    ProjectNetworkRequest,
    ProjectNetworkResponse,
    ProjectProviderKeyCatalogResponse,
    ProjectProviderKeyDeleteResponse,
    ProjectProviderKeyRequest,
    ProjectProviderKeyValidateRequest,
    ProjectProviderKeyValidationResponse,
    ProjectRetentionRequest,
    ProjectRetentionResponse,
    ProjectSecretCatalogResponse,
    ProjectSecretDeleteResponse,
    ProjectSecretRequest,
    ProjectSettingsRequest,
    ProjectSettingsResponse,
)
from frisket.server.route_errors import (
    http_error_responses,
    reject_unknown_query_parameters,
    register_typed_error,
)
from frisket.server.services.projects import (
    ProjectDeletionBlocked,
    ProjectDeletionRequiresConfirmation,
    ProjectLifecycleService,
    ProjectNetworkPolicyError,
    ProjectNotFound,
    ProjectRetentionError,
    ProjectSettingsError,
    SheetDeletionBlocked,
)


def register_project_lifecycle_routes(
    app: FastAPI,
    *,
    service: ProjectLifecycleService,
) -> None:
    register_typed_error(app, ProjectNotFound, 404)
    register_typed_error(
        app,
        (ProjectRetentionError, ProjectNetworkPolicyError, ProjectSettingsError),
        400,
    )
    register_typed_error(app, ProjectDeletionRequiresConfirmation, 422)
    register_typed_error(app, ProjectDeletionBlocked, 409)
    register_typed_error(app, SheetDeletionBlocked, 409)

    @app.get(
        "/api/projects",
        response_model=ProjectList,
        responses=http_error_responses(401, 500),
    )
    def list_projects() -> ProjectList:
        return service.list_projects()

    @app.post(
        "/api/projects",
        response_model=Project,
        response_model_exclude_unset=True,
        responses=http_error_responses(401, 422, 500),
    )
    def create_project(request: Request, body: CreateProjectRequest) -> Project:
        reject_unknown_query_parameters(request, EmptyQuery)
        user = getattr(request.state, "user", None)
        actor_id = user.get("id") if isinstance(user, dict) else None
        return Project.model_validate(
            service.create_project(
                body.name,
                sensitive=body.sensitive,
                idempotency_key=request.headers.get("idempotency-key"),
                idempotency_scope=(
                    f"user:{int(actor_id)}" if actor_id is not None else None
                ),
            )
        )

    @app.get(
        "/api/org/provider-catalog",
        response_model=OrganizationProviderCatalog,
        responses=http_error_responses(401, 500),
    )
    def provider_catalog() -> dict:
        return service.provider_catalog()

    @app.get(
        "/api/projects/{pid}",
        response_model=Project,
        response_model_exclude_unset=True,
        responses=http_error_responses(401, 403, 404, 409, 500),
    )
    def get_project(pid: str) -> Project:
        return Project.model_validate(service.project_metadata(pid))

    @app.patch(
        "/api/projects/{pid}",
        response_model=Project,
        response_model_exclude_unset=True,
        responses=http_error_responses(401, 403, 404, 409, 422, 500),
    )
    def update_project(
        request: Request, pid: str, body: UpdateProjectRequest
    ) -> Project:
        reject_unknown_query_parameters(request, EmptyQuery)
        return Project.model_validate(
            service.update_project_metadata(
                pid,
                name=body.name,
                description=body.description,
                starred=body.starred,
                archived=body.archived,
            )
        )

    @app.patch(
        "/api/projects/{pid}/sensitivity",
        response_model=Project,
        response_model_exclude_unset=True,
        name="update_project_sensitivity",
    )
    def update_project_sensitivity(
        pid: str, body: UpdateProjectSensitivityRequest
    ) -> Project:
        return Project.model_validate(
            service.update_project_sensitivity(pid, sensitive=body.sensitive)
        )

    @app.get(
        "/api/projects/{pid}/retention",
        response_model=ProjectRetentionResponse,
        response_model_exclude_unset=True,
        responses=http_error_responses(401, 403, 404, 500),
    )
    def get_project_retention(pid: str) -> ProjectRetentionResponse:
        return ProjectRetentionResponse.model_validate(service.retention_policy(pid))

    @app.patch(
        "/api/projects/{pid}/retention",
        response_model=ProjectRetentionResponse,
        response_model_exclude_unset=True,
        responses=http_error_responses(400, 401, 403, 404, 422, 500),
    )
    def update_project_retention(
        pid: str, body: ProjectRetentionRequest
    ) -> ProjectRetentionResponse:
        return ProjectRetentionResponse.model_validate(
            service.update_retention_policy(
                pid,
                default_evidence=body.default_evidence,
                pin_evidence_by_default=body.pin_evidence_by_default,
                no_compact=body.no_compact,
            )
        )

    @app.get(
        "/api/projects/{pid}/network",
        response_model=ProjectNetworkResponse,
        response_model_exclude_unset=True,
        responses=http_error_responses(401, 403, 404, 500),
    )
    def get_project_network(pid: str) -> ProjectNetworkResponse:
        return ProjectNetworkResponse.model_validate(service.network_policy(pid))

    @app.patch(
        "/api/projects/{pid}/network",
        response_model=ProjectNetworkResponse,
        response_model_exclude_unset=True,
        responses=http_error_responses(400, 401, 403, 404, 422, 500),
    )
    def update_project_network(
        pid: str, body: ProjectNetworkRequest
    ) -> ProjectNetworkResponse:
        return ProjectNetworkResponse.model_validate(
            service.update_network_policy(pid, mode=body.mode)
        )

    @app.get(
        "/api/projects/{pid}/settings",
        response_model=ProjectSettingsResponse,
        response_model_exclude_unset=True,
        responses=http_error_responses(401, 403, 404, 500),
    )
    def get_project_settings(pid: str) -> ProjectSettingsResponse:
        return ProjectSettingsResponse.model_validate(service.project_settings(pid))

    @app.patch(
        "/api/projects/{pid}/settings",
        response_model=ProjectSettingsResponse,
        response_model_exclude_unset=True,
        responses=http_error_responses(400, 401, 403, 404, 422, 500),
    )
    def update_project_settings(
        pid: str, body: ProjectSettingsRequest
    ) -> ProjectSettingsResponse:
        return ProjectSettingsResponse.model_validate(
            service.update_project_settings(
                pid,
                patch=body.model_dump(exclude_none=True),
            )
        )

    @app.post(
        "/api/projects/{pid}/compact",
        response_model=ProjectCompactResponse,
        response_model_exclude_unset=True,
        responses=http_error_responses(401, 403, 404, 500),
    )
    def compact_project(pid: str) -> ProjectCompactResponse:
        return ProjectCompactResponse.model_validate(service.compact_project(pid))

    @app.get(
        "/api/projects/{pid}/provider-keys",
        response_model=ProjectProviderKeyCatalogResponse,
        response_model_exclude_unset=True,
        responses=http_error_responses(401, 403, 404, 500),
    )
    def get_project_provider_keys(pid: str) -> ProjectProviderKeyCatalogResponse:
        return ProjectProviderKeyCatalogResponse.model_validate(
            service.project_provider_keys(pid)
        )

    @app.post(
        "/api/projects/{pid}/provider-keys",
        response_model=ProjectProviderKeyCatalogResponse,
        response_model_exclude_unset=True,
        responses=http_error_responses(400, 401, 403, 404, 422, 500),
    )
    def set_project_provider_key(
        pid: str, body: ProjectProviderKeyRequest
    ) -> ProjectProviderKeyCatalogResponse:
        return ProjectProviderKeyCatalogResponse.model_validate(
            service.set_project_provider_key(
                pid,
                provider=body.provider,
                key=body.key,
                spend_cap_usd=body.spend_cap_usd,
                validation_token=body.validation_token,
            )
        )

    @app.post(
        "/api/projects/{pid}/provider-keys/validate",
        response_model=ProjectProviderKeyValidationResponse,
        response_model_exclude_unset=True,
        responses=http_error_responses(400, 401, 403, 404, 422, 500),
    )
    def validate_project_provider_key(
        pid: str, body: ProjectProviderKeyValidateRequest
    ) -> ProjectProviderKeyValidationResponse:
        return ProjectProviderKeyValidationResponse.model_validate(
            service.validate_project_provider_key(
                pid,
                provider=body.provider,
                key=body.key,
            )
        )

    @app.delete(
        "/api/projects/{pid}/provider-keys/{provider}",
        response_model=ProjectProviderKeyDeleteResponse,
        response_model_exclude_unset=True,
        responses=http_error_responses(400, 401, 403, 404, 422, 500),
    )
    def delete_project_provider_key(
        pid: str, provider: str
    ) -> ProjectProviderKeyDeleteResponse:
        return ProjectProviderKeyDeleteResponse.model_validate(
            service.delete_project_provider_key(pid, provider=provider)
        )

    @app.get(
        "/api/projects/{pid}/secrets",
        response_model=ProjectSecretCatalogResponse,
        response_model_exclude_unset=True,
        responses=http_error_responses(401, 403, 404, 500),
    )
    def get_project_secrets(pid: str) -> ProjectSecretCatalogResponse:
        return ProjectSecretCatalogResponse.model_validate(service.project_secrets(pid))

    @app.post(
        "/api/projects/{pid}/secrets",
        response_model=ProjectSecretCatalogResponse,
        response_model_exclude_unset=True,
        responses=http_error_responses(400, 401, 403, 404, 422, 500),
    )
    def set_project_secret(
        pid: str, body: ProjectSecretRequest
    ) -> ProjectSecretCatalogResponse:
        return ProjectSecretCatalogResponse.model_validate(
            service.set_project_secret(pid, name=body.name, value=body.value)
        )

    @app.delete(
        "/api/projects/{pid}/secrets/{name}",
        response_model=ProjectSecretDeleteResponse,
        response_model_exclude_unset=True,
        responses=http_error_responses(400, 401, 403, 404, 422, 500),
    )
    def delete_project_secret(pid: str, name: str) -> ProjectSecretDeleteResponse:
        return ProjectSecretDeleteResponse.model_validate(
            service.delete_project_secret(pid, name=name)
        )

    @app.delete(
        "/api/projects/{pid}",
        response_model=ProjectDelete,
        responses=http_error_responses(401, 403, 404, 409, 422, 500),
    )
    def delete_project(
        request: Request, pid: str, body: ProjectDeleteRequest
    ) -> ProjectDelete:
        reject_unknown_query_parameters(request, EmptyQuery)
        return ProjectDelete.model_validate(
            service.delete_project(pid, confirm_name=body.confirm_name)
        )

    @app.get(
        "/api/projects/{pid}/sheets",
        response_model=SheetList,
        response_model_exclude_unset=True,
        responses=http_error_responses(401, 403, 404, 409, 500),
    )
    def list_sheets(pid: str) -> SheetList:
        return SheetList.model_validate(service.list_sheets(pid))

    @app.patch(
        "/api/projects/{pid}/sheets/{sheet_id}",
        response_model=Sheet,
        responses=http_error_responses(400, 401, 403, 404, 409, 422, 500),
    )
    def update_sheet(
        request: Request,
        pid: str,
        sheet_id: int,
        body: UpdateSheetRequest,
    ) -> Sheet:
        reject_unknown_query_parameters(request, EmptyQuery)
        try:
            return Sheet.model_validate(
                service.update_sheet_metadata(
                    pid,
                    sheet_id,
                    title_column_id=body.title_column_id,
                    title_column_id_provided=(
                        "title_column_id" in body.model_fields_set
                    ),
                )
            )
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.delete(
        "/api/projects/{pid}/sheets/{sheet_id}",
        response_model=SheetDeleteResponse,
        responses=http_error_responses(401, 403, 404, 409, 422, 500),
    )
    def delete_sheet(request: Request, pid: str, sheet_id: int) -> SheetDeleteResponse:
        reject_unknown_query_parameters(request, EmptyQuery)
        return SheetDeleteResponse.model_validate(service.delete_sheet(pid, sheet_id))

    @app.get(
        "/api/projects/{pid}/lineage",
        response_model=ProjectLineageResponse,
        responses=http_error_responses(401, 403, 404, 422, 500),
    )
    def project_lineage(pid: str) -> ProjectLineageResponse:
        return ProjectLineageResponse.model_validate(service.lineage(pid))
