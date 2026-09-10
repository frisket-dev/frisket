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

from frisket.ai.llm.endpoint_config import LocalModelEndpointConfig
from frisket.contracts.http.local_providers import (
    ArtifactPullRequest,
    ArtifactUninstallRequest,
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
    MODEL_PULL_MAX_ATTEMPTS,
    InvalidModelRefError,
)
from frisket.engine.jobs.queue import (
    MODEL_PULL_KIND,
    SERVER_SCOPED_JOB_PAYLOAD_KEY,
)
from frisket.server import provider_config
from frisket.server.route_errors import RouteError, http_error_responses
from frisket.server.workspace import Workspace


# DTO builder lives in model_pull_store (the shared frisket.model_pull.v2
# wire shape) so local and team routes cannot drift on field names/types.
_pull_dto = model_pull_store.to_dto


def register_provider_config_routes(app: FastAPI, *, workspace: Workspace) -> None:
    root = workspace.root

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
            network_off = project.effective_network_policy() == "off"
        return provider_config.build_provider_catalog(
            root, dict(os.environ), network_off=network_off
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
        responses=http_error_responses(400, 422, 500),
    )
    def set_provider_key(
        provider: str, body: ProviderKeyRequest
    ) -> LocalProviderCatalog:
        try:
            provider_config.require_validation_token(
                provider,
                body.key,
                body.validation_token,
            )
            provider_config.save_local_provider_key(root, provider, body.key)
        except provider_config.UnknownProviderError as exc:
            raise HTTPException(400, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return provider_config.build_provider_catalog(root, dict(os.environ))

    @app.delete(
        "/api/providers/keys/{provider}",
        response_model=LocalProviderCatalog,
        response_model_exclude_unset=True,
        responses=http_error_responses(400, 500),
    )
    def delete_provider_key(provider: str) -> LocalProviderCatalog:
        try:
            provider_config.delete_local_provider_key(root, provider)
        except provider_config.UnknownProviderError as exc:
            raise HTTPException(400, str(exc)) from exc
        return provider_config.build_provider_catalog(root, dict(os.environ))

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
        except (provider_config.InvalidProviderConfigError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return provider_config.build_provider_catalog(root, dict(os.environ))

    @app.post(
        "/api/providers/local-endpoints/discover",
        response_model=LocalEndpointDiscoveryResponse,
        responses=http_error_responses(400, 500),
    )
    def discover_local_endpoints() -> LocalEndpointDiscoveryResponse:
        try:
            return provider_config.discover_local_endpoints(root)
        except (provider_config.InvalidProviderConfigError, ValueError) as exc:
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
        env_endpoint, _notes = provider_config.resolve_env_local_endpoint(
            dict(os.environ)
        )
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
        except (provider_config.InvalidProviderConfigError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return provider_config.build_provider_catalog(root, dict(os.environ))

    @app.delete(
        "/api/providers/local-endpoints/{endpoint_id}",
        response_model=LocalProviderCatalog,
        response_model_exclude_unset=True,
        responses=http_error_responses(400, 404, 409, 500),
    )
    def delete_local_endpoint(endpoint_id: str) -> LocalProviderCatalog:
        env_endpoint, _notes = provider_config.resolve_env_local_endpoint(
            dict(os.environ)
        )
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
        except (provider_config.InvalidProviderConfigError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc
        if not deleted:
            raise HTTPException(404, f"unknown local model endpoint: {endpoint_id}")
        return provider_config.build_provider_catalog(root, dict(os.environ))

    @app.get(
        "/api/providers/models/pulls",
        response_model=ModelPullListResponse,
        responses=http_error_responses(500),
    )
    def list_model_pulls() -> ModelPullListResponse:
        engine = workspace.queue.engine
        rows = model_pull_store.list_recent(engine, str(root), limit=20)
        return {"pulls": [_pull_dto(r) for r in rows]}

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
        return _pull_dto(row)

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
        row = model_pull_store.get(engine, pull_id)
        return _pull_dto(row)

    def _ollama_reachability_preflight(
        env: dict, endpoint_id: str
    ) -> LocalModelEndpointConfig:
        """Validate the explicitly selected daemon before enqueueing its pull."""
        local_endpoints, _notes = provider_config.resolve_local_endpoints(root, env)
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
    ) -> dict:
        engine = workspace.queue.engine
        root_str = str(root)
        try:
            row, created = model_pull_store.create_or_get_active(
                engine,
                workspace_root=root_str,
                model_ref=canonical,
                endpoint_id=endpoint_id,
                endpoint_origin=endpoint_origin,
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
        if not created:
            return {"pull": _pull_dto(row), "deduplicated": True}
        try:
            job_id = workspace.queue.enqueue(
                MODEL_PULL_KIND,
                {
                    "pull_id": row.id,
                    "workspace_root": root_str,
                    # The artifact lands in this workspace's shared model
                    # cache, owned by no tenant.
                    SERVER_SCOPED_JOB_PAYLOAD_KEY: True,
                    **payload_extra,
                },
                max_attempts=MODEL_PULL_MAX_ATTEMPTS,
            )
        except Exception as exc:  # noqa: BLE001 -- must not leave an orphan active row
            model_pull_store.mark_failed(
                engine,
                row.id,
                error_code="enqueue_failed",
                error_message=f"failed to enqueue the pull job ({type(exc).__name__})",
            )
            raise RouteError(
                503,
                {
                    "code": "enqueue_failed",
                    "message": "failed to schedule the model pull",
                },
            ) from exc
        model_pull_store.set_job_id(engine, row.id, job_id=job_id)
        row = model_pull_store.get(engine, row.id)
        return {"pull": _pull_dto(row), "deduplicated": False}

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
        # Fence lookup->tombstone->delete atomically. The store refuses if
        # a pull is in flight and flips ALL done rows for the ref in ONE
        # transaction, so a concurrent install can't race the delete.
        try:
            row = model_pull_store.mark_ref_uninstalled(
                engine, workspace_root=str(root), model_ref=art.canonical
            )
        except model_pull_store.UninstallInFlightError as exc:
            raise RouteError(
                409,
                {
                    "code": "pull_in_flight",
                    "message": (
                        f"a pull is in progress for {art.canonical}; cannot uninstall"
                    ),
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
        from frisket.ai.models import model_cache

        # Bytes are removed AFTER the tombstone commits; the translator-cache
        # invalidation covers the brief in-flight-load window.
        model_cache.uninstall(art)
        opus_mt._clear_translator_cache()
        return _pull_dto(model_pull_store.get(engine, row.id))

    @app.post(
        "/api/providers/{provider}/validate",
        response_model=ProviderValidationResponse,
        response_model_exclude_unset=True,
        responses=http_error_responses(400, 422, 500),
    )
    def validate_provider_key(
        provider: str, body: ProviderValidationRequest
    ) -> ProviderValidationResponse:
        try:
            normalized = provider_config.normalize_provider(provider)
        except provider_config.UnknownProviderError as exc:
            raise HTTPException(400, str(exc)) from exc
        env = dict(os.environ)
        key = (
            (body.key or "").strip()
            or env.get(provider_config.ENV_VAR[normalized])
            or provider_config.load_local_provider_keys(root).get(normalized)
        )
        if not key:
            return {
                "provider": normalized,
                "ok": False,
                "reachable": False,
                "status": None,
                "detail": "no key configured",
            }
        result = provider_config.probe_provider(normalized, key)
        # The live probe always supplies these, but retain the complete typed
        # response contract for injected/legacy probe implementations too.
        result = {
            "provider": normalized,
            "detail": None,
            **result,
        }
        if body.key and result.get("ok") is True:
            result = {
                **result,
                "validation_token": provider_config.issue_validation_token(
                    normalized,
                    body.key,
                ),
            }
        return result
