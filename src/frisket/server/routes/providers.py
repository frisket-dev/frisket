"""Local-tier provider config routes: model catalog, UI key management, and a
per-provider validate probe.

Keys the UI enters persist to a gitignored workspace file (never echoed back);
the validate probe issues the cheapest real request per provider. Route bodies
call ``provider_config`` functions by attribute so probes stay injectable in
tests.
"""

from __future__ import annotations

import os

from fastapi import FastAPI, HTTPException

from frisket.ai.llm.endpoint_config import (
    LocalModelEndpointConfig,
    resolve_env_local_endpoint,
)
from frisket.contracts.http.local_providers import (
    ArtifactPullRequest,
    ArtifactUninstallRequest,
    EngineSetupRequest,
    LocalEndpointCreateRequest,
    LocalEndpointDiscoveryResponse,
    LocalEndpointPatchRequest,
    LocalProviderCatalog,
    LocalHttpEndpointProvider,
    ModelPull,
    ModelPullListResponse,
    ModelPullStartResponse,
    PlatformProvider,
    ProviderKeyRequest,
    ProviderValidationRequest,
    ProviderValidationResponse,
)
from frisket.engine.jobs import model_pull_store
from frisket.engine.jobs.artifact_ref import normalize_artifact_ref
from frisket.engine.jobs.model_pull import (
    InvalidModelRefError,
    ModelPullEnqueueError,
    schedule_model_pull,
)
from frisket.engine.jobs.queue import SERVER_SCOPED_JOB_PAYLOAD_KEY
from frisket.local_provider_config import (
    ENV_VAR,
    InvalidProviderConfigError,
    load_local_provider_keys,
    resolve_local_endpoints,
)
from frisket.server import provider_config
from frisket.server.route_errors import (
    RouteError,
    http_error_responses,
    register_typed_error,
)
from frisket.server.services.project_model_routes import (
    ProjectModelRouteError,
    register_project_model_route_error,
    ProjectModelRoutesService,
)
from frisket.server.workspace import Workspace


# DTO builder lives in model_pull_store (the shared frisket.model_pull.v2
# wire shape) so local and team routes cannot drift on field names/types.
_pull_dto = model_pull_store.to_dto


def register_provider_config_routes(app: FastAPI, *, workspace: Workspace) -> None:
    register_project_model_route_error(app)
    root = workspace.root
    model_routes = ProjectModelRoutesService(workspace)
    register_typed_error(app, provider_config.UnknownProviderError, 400)

    @app.get(
        "/api/providers",
        response_model=LocalProviderCatalog,
        response_model_exclude_unset=True,
        responses=http_error_responses(404, 409, 500),
    )
    def list_providers(project_id: str | None = None) -> LocalProviderCatalog:
        # The catalog is instance-scoped; an optional project_id folds in
        # that project's effective network policy so the picker only offers
        # providers dispatch would accept.
        network_off = False
        if project_id:
            # Workspace.get raises its own 404 for unknown/malformed ids.
            project = workspace.get(project_id)
            network_off = project.remote_services_refused()
        return LocalProviderCatalog.model_validate(
            provider_config.build_provider_catalog(
                root, dict(os.environ), network_off=network_off
            )
        )

    @app.get(
        "/api/providers/{provider}/status",
        response_model=PlatformProvider | LocalHttpEndpointProvider,
        response_model_exclude_unset=True,
        responses=http_error_responses(404, 500),
    )
    def provider_status(provider: str) -> PlatformProvider | LocalHttpEndpointProvider:
        catalog = provider_config.build_provider_catalog(root, dict(os.environ))
        entry = next(
            (
                candidate
                for candidate in catalog["providers"]
                if (
                    candidate.get("endpoint_id")
                    if candidate.get("kind") == "local_http"
                    else candidate.get("id")
                )
                == provider
            ),
            None,
        )
        if entry is None:
            raise HTTPException(404, f"unknown provider: {provider}")
        return entry

    @app.put(
        "/api/providers/keys/{provider}",
        response_model=LocalProviderCatalog,
        response_model_exclude_unset=True,
        responses=http_error_responses(400, 409, 422, 500),
    )
    def set_provider_key(
        provider: str, body: ProviderKeyRequest
    ) -> LocalProviderCatalog:
        try:
            normalized_provider = provider_config.normalize_provider(provider)
            provider_config.require_validation_token(
                normalized_provider,
                body.key,
                body.validation_token,
            )
            model_routes.pin_all_projects()
            provider_config.save_local_provider_key(
                root,
                normalized_provider,
                body.key,
            )
        except ProjectModelRouteError:
            # Rendered by its app-level handler; the ValueError below must not
            # swallow it.
            raise
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return LocalProviderCatalog.model_validate(
            provider_config.build_provider_catalog(root, dict(os.environ))
        )

    @app.delete(
        "/api/providers/keys/{provider}",
        response_model=LocalProviderCatalog,
        response_model_exclude_unset=True,
        responses=http_error_responses(400, 409, 500),
    )
    def delete_provider_key(provider: str) -> LocalProviderCatalog:
        normalized_provider = provider_config.normalize_provider(provider)
        model_routes.pin_all_projects()
        provider_config.delete_local_provider_key(root, normalized_provider)
        return LocalProviderCatalog.model_validate(
            provider_config.build_provider_catalog(root, dict(os.environ))
        )

    @app.post(
        "/api/providers/local-endpoints",
        response_model=LocalProviderCatalog,
        response_model_exclude_unset=True,
        responses=http_error_responses(400, 422, 500),
    )
    def create_local_endpoint(
        body: LocalEndpointCreateRequest,
    ) -> LocalProviderCatalog:
        try:
            provider_config.create_local_endpoint(
                root,
                name=body.display_name,
                url=body.origin,
                inference_token=body.inference_token,
                provisioning_token=body.provisioning_token,
                edge_auth=body.edge_auth,
                pull_enabled=body.pull_enabled,
            )
        except (InvalidProviderConfigError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return LocalProviderCatalog.model_validate(
            provider_config.build_provider_catalog(root, dict(os.environ))
        )

    @app.post(
        "/api/providers/local-endpoints/discover",
        response_model=LocalEndpointDiscoveryResponse,
        responses=http_error_responses(400, 500),
    )
    def discover_local_endpoints() -> LocalEndpointDiscoveryResponse:
        try:
            return provider_config.discover_local_endpoints(root)
        except (InvalidProviderConfigError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.patch(
        "/api/providers/local-endpoints/{endpoint_id}",
        response_model=LocalProviderCatalog,
        response_model_exclude_unset=True,
        responses=http_error_responses(400, 404, 409, 422, 500),
    )
    def update_local_endpoint(
        endpoint_id: str,
        body: LocalEndpointPatchRequest,
    ) -> LocalProviderCatalog:
        env_endpoint, _notes = resolve_env_local_endpoint(dict(os.environ))
        if env_endpoint is not None and env_endpoint.endpoint_id == endpoint_id:
            raise HTTPException(
                409,
                "this local model endpoint is managed by the server environment",
            )
        try:
            provider_config.patch_local_endpoint(
                root, endpoint_id, body.model_dump(exclude_unset=True)
            )
        except KeyError as exc:
            raise HTTPException(
                404, f"unknown local model endpoint: {endpoint_id}"
            ) from exc
        except (InvalidProviderConfigError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return LocalProviderCatalog.model_validate(
            provider_config.build_provider_catalog(root, dict(os.environ))
        )

    @app.delete(
        "/api/providers/local-endpoints/{endpoint_id}",
        response_model=LocalProviderCatalog,
        response_model_exclude_unset=True,
        responses=http_error_responses(400, 404, 409, 500),
    )
    def delete_local_endpoint(endpoint_id: str) -> LocalProviderCatalog:
        env_endpoint, _notes = resolve_env_local_endpoint(dict(os.environ))
        if env_endpoint is not None and env_endpoint.endpoint_id == endpoint_id:
            raise HTTPException(
                409,
                "this local model endpoint is managed by the server environment",
            )
        try:
            deleted = provider_config.delete_local_endpoint(
                root,
                endpoint_id,
            )
        except (InvalidProviderConfigError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc
        if not deleted:
            raise HTTPException(404, f"unknown local model endpoint: {endpoint_id}")
        return LocalProviderCatalog.model_validate(
            provider_config.build_provider_catalog(root, dict(os.environ))
        )

    @app.get(
        "/api/providers/models/pulls",
        response_model=ModelPullListResponse,
        responses=http_error_responses(500),
    )
    def list_model_pulls() -> ModelPullListResponse:
        engine = workspace.queue.engine
        rows = model_pull_store.list_recent(engine, str(root), limit=20)
        return ModelPullListResponse(
            pulls=[ModelPull.model_validate(_pull_dto(r)) for r in rows]
        )

    @app.get(
        "/api/providers/models/pulls/{pull_id}",
        response_model=ModelPull,
        responses=http_error_responses(404, 422, 500),
    )
    def get_model_pull(pull_id: int) -> ModelPull:
        engine = workspace.queue.engine
        row = model_pull_store.get(engine, pull_id)
        if row is None or row.workspace_root != str(root):
            raise RouteError(
                404,
                {
                    "code": "pull_not_found",
                    "message": f"no pull with id {pull_id} in this workspace",
                },
            )
        return ModelPull.model_validate(_pull_dto(row))

    @app.post(
        "/api/providers/models/pulls/{pull_id}/cancel",
        status_code=202,
        response_model=ModelPull,
        responses=http_error_responses(404, 422, 500),
    )
    def cancel_model_pull(pull_id: int) -> ModelPull:
        engine = workspace.queue.engine
        row = model_pull_store.get(engine, pull_id)
        if row is None or row.workspace_root != str(root):
            raise RouteError(
                404,
                {
                    "code": "pull_not_found",
                    "message": f"no pull with id {pull_id} in this workspace",
                },
            )
        model_pull_store.request_cancel(engine, pull_id)
        if row.job_id is not None:
            # A job that was still 'queued' (never claimed) has no handler
            # that will EVER run to mark this row terminal -- the route
            # finalizes it directly. `mark_cancelled` is guarded (only
            # writes an ACTIVE row), so this tolerates a race against the
            # handler's own cooperative cancel check if the job was claimed
            # in between.
            report = workspace.queue.cancel_with_report(row.job_id)
            if report.cancelled and report.previous_status == "queued":
                model_pull_store.mark_cancelled(engine, pull_id)
        updated = model_pull_store.get(engine, pull_id)
        return ModelPull.model_validate(_pull_dto(updated or row))

    def _ollama_reachability_preflight(
        env: dict, endpoint_id: str
    ) -> LocalModelEndpointConfig:
        """Validate the explicitly selected daemon before enqueueing its pull."""
        local_endpoints, _notes = resolve_local_endpoints(root, env)
        local_endpoint = next(
            (
                endpoint
                for endpoint in local_endpoints
                if endpoint.endpoint_id == endpoint_id
            ),
            None,
        )
        if local_endpoint is None:
            raise RouteError(
                404,
                {
                    "code": "local_endpoint_not_found",
                    "message": f"unknown local model endpoint: {endpoint_id}",
                },
            )
        url = local_endpoint.origin
        probe = provider_config.ollama_reachable(
            url,
            token=local_endpoint.bearer_for_provisioning(),
            edge_auth=local_endpoint.edge_auth,
        )
        if not probe.get("reachable"):
            raise RouteError(
                503,
                {
                    "code": "local_server_unreachable",
                    "message": f"no local server answered at {url}",
                    "url": url,
                },
            )
        if probe.get("auth_status") == "unauthorized":
            raise RouteError(
                409,
                {
                    "code": "local_server_unauthorized",
                    "message": f"the local server at {url} requires authentication",
                    "url": url,
                },
            )
        if probe.get("protocol") != "ollama_native":
            raise RouteError(
                409,
                {
                    "code": "pull_unsupported",
                    "message": (
                        "your server lists models but does not accept downloads"
                    ),
                    "url": url,
                    "protocol": probe.get("protocol"),
                },
            )
        if not local_endpoint.pull_enabled:
            raise RouteError(
                403,
                {
                    "code": "model_pull_disabled",
                    "message": f"model download is not enabled for {endpoint_id}",
                },
            )
        return local_endpoint

    def _enqueue_and_respond(
        canonical: str,
        *,
        endpoint_id: str | None,
        endpoint_origin: str | None,
        payload_extra: dict,
    ) -> ModelPullStartResponse:
        try:
            row, created = schedule_model_pull(
                workspace.queue,
                workspace_root=root,
                model_ref=canonical,
                endpoint_id=endpoint_id,
                endpoint_origin=endpoint_origin,
                queue_payload_extra={
                    SERVER_SCOPED_JOB_PAYLOAD_KEY: True,
                    **payload_extra,
                },
            )
        except model_pull_store.ModelPullBusyError as exc:
            raise RouteError(
                409,
                {
                    "code": "pull_busy",
                    "message": (
                        f"a pull is already in progress for {exc.active.model_ref}"
                    ),
                    "active": _pull_dto(exc.active),
                },
            ) from exc
        except ModelPullEnqueueError as exc:
            raise RouteError(
                503,
                {
                    "code": "enqueue_failed",
                    "message": "failed to schedule the model pull",
                },
            ) from exc
        return ModelPullStartResponse(
            pull=ModelPull.model_validate(_pull_dto(row)),
            deduplicated=not created,
        )

    @app.post(
        "/api/providers/models/pull",
        status_code=202,
        response_model=ModelPullStartResponse,
        responses=http_error_responses(400, 403, 409, 422, 500, 503),
    )
    def pull_artifact(body: ArtifactPullRequest) -> ModelPullStartResponse:
        """Pull any supported artifact scheme into the workspace model cache."""
        env = dict(os.environ)
        try:
            art = normalize_artifact_ref(body.ref)
        except InvalidModelRefError as exc:
            raise RouteError(
                400, {"code": "invalid_model_ref", "message": str(exc)}
            ) from exc

        endpoint_id: str | None = None
        endpoint_origin: str | None = None
        payload_extra: dict = {}
        if art.is_ollama:
            if not art.endpoint_id:
                raise RouteError(
                    400,
                    {
                        "code": "invalid_model_ref",
                        "message": "Ollama artifact ref must name an endpoint id",
                    },
                )
            endpoint = _ollama_reachability_preflight(env, art.endpoint_id)
            endpoint_id = endpoint.endpoint_id
            endpoint_origin = endpoint.origin
            payload_extra["endpoint_id"] = endpoint.endpoint_id
        else:
            from frisket.ai.models import artifact_manifest

            if art.scheme == "hf" and artifact_manifest.lookup(art.canonical) is None:
                if not body.unpinned_acknowledged:
                    raise RouteError(
                        400,
                        {
                            "code": "unpinned_unacknowledged",
                            "message": (
                                "this artifact is not pinned by frisket -- pulling it "
                                "requires acknowledging it has no checksum or license "
                                "vetting"
                            ),
                        },
                    )
                payload_extra["unpinned_acknowledged"] = True
        return _enqueue_and_respond(
            art.canonical,
            endpoint_id=endpoint_id,
            endpoint_origin=endpoint_origin,
            payload_extra=payload_extra,
        )

    @app.post(
        "/api/providers/models/setup",
        status_code=202,
        response_model=ModelPullStartResponse,
        responses=http_error_responses(400, 409, 422, 500, 503),
    )
    def setup_model_engine(body: EngineSetupRequest) -> ModelPullStartResponse:
        """Start an allowlisted local engine setup operation."""
        from frisket.contracts.classification import LOCAL_CLASSIFIERS
        from frisket.engine.jobs.engine_setup import (
            CHROMIUM_SETUP_REF,
            SPACY_SETUP_REF,
            OPUS_MT_SETUP_REF,
            DOCLING_SETUP_REF,
            EMBEDDINGGEMMA_SETUP_REF,
            FASTER_WHISPER_SETUP_REF,
            INDRA_OCR_SETUP_REF,
            PARAKEET_TDT_SETUP_REF,
            RAPIDOCR_SETUP_REF,
        )

        if body.setup_ref not in {
            CHROMIUM_SETUP_REF,
            SPACY_SETUP_REF,
            OPUS_MT_SETUP_REF,
            FASTER_WHISPER_SETUP_REF,
            PARAKEET_TDT_SETUP_REF,
            RAPIDOCR_SETUP_REF,
            DOCLING_SETUP_REF,
            INDRA_OCR_SETUP_REF,
            EMBEDDINGGEMMA_SETUP_REF,
            *(spec.setup_ref for spec in LOCAL_CLASSIFIERS.values()),
        }:
            raise RouteError(
                400,
                {
                    "code": "unknown_engine_setup",
                    "message": "the requested engine setup is not supported",
                },
            )
        return _enqueue_and_respond(
            body.setup_ref,
            endpoint_id=None,
            endpoint_origin=None,
            payload_extra={},
        )

    @app.post(
        "/api/providers/models/uninstall",
        response_model=ModelPull,
        responses=http_error_responses(400, 404, 409, 422, 500),
    )
    def uninstall_artifact(body: ArtifactUninstallRequest) -> ModelPull:
        """Remove a pulled artifact's bytes and tombstone the row with the
        distinct ``uninstalled`` status. Ollama models are the
        daemon's to manage -- rejected here."""
        try:
            art = normalize_artifact_ref(body.ref)
        except InvalidModelRefError as exc:
            raise RouteError(
                400, {"code": "invalid_model_ref", "message": str(exc)}
            ) from exc
        if art.is_ollama:
            raise RouteError(
                400,
                {
                    "code": "uninstall_unsupported",
                    "message": "Ollama models are managed by the Ollama daemon.",
                },
            )
        if art.scheme == "hf-snapshot":
            # These bytes live in the Hugging Face hub cache (HF_HUB_CACHE),
            # not the frisket-owned model cache -- model_cache.uninstall has
            # nothing to remove and would raise on the scheme.
            raise RouteError(
                400,
                {
                    "code": "uninstall_unsupported",
                    "message": (
                        "hf-snapshot artifacts live in the Hugging Face hub "
                        "cache (HF_HUB_CACHE); remove them there if needed"
                    ),
                },
            )
        engine = workspace.queue.engine
        from frisket.engine.jobs.artifact_pull import (
            uninstall_artifact as remove_artifact,
        )

        try:
            row = remove_artifact(engine, workspace_root=str(root), art=art)
        except model_pull_store.UninstallInFlightError as exc:
            raise RouteError(
                409,
                {
                    "code": "pull_in_flight",
                    "message": "The model is being installed or removed; try again when it finishes.",
                },
            ) from exc
        except OSError as exc:
            raise RouteError(
                409,
                {
                    "code": "artifact_remove_failed",
                    "message": "Could not remove the model files. Stop work using this model and retry.",
                },
            ) from exc
        if row is None:
            raise RouteError(
                404,
                {
                    "code": "artifact_not_installed",
                    "message": f"no installed artifact for {art.canonical}",
                },
            )
        from frisket.ops.integrations import opus_mt

        opus_mt._clear_translator_cache()
        updated = model_pull_store.get(engine, row.id)
        return ModelPull.model_validate(_pull_dto(updated or row))

    @app.post(
        "/api/providers/{provider}/validate",
        response_model=ProviderValidationResponse,
        response_model_exclude_unset=True,
        responses=http_error_responses(400, 422, 500),
    )
    def validate_provider_key(
        provider: str, body: ProviderValidationRequest
    ) -> ProviderValidationResponse:
        normalized = provider_config.normalize_provider(provider)
        env = dict(os.environ)
        key = (
            (body.key or "").strip()
            or env.get(ENV_VAR[normalized])
            or load_local_provider_keys(root).get(normalized)
        )
        if not key:
            return ProviderValidationResponse(
                provider=normalized,
                ok=False,
                reachable=False,
                status=None,
                detail="no key configured",
            )
        probed = provider_config.probe_provider(normalized, key)
        # The live probe always supplies these, but retain the complete typed
        # response contract for injected/legacy probe implementations too.
        response = ProviderValidationResponse.model_validate(
            {"provider": normalized, "detail": None, **probed}
        )
        if body.key and probed.get("ok") is True:
            response = response.model_copy(
                update={
                    "validation_token": provider_config.issue_validation_token(
                        normalized,
                        body.key,
                    )
                }
            )
        return response
