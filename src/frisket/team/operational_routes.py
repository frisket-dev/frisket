"""HTTP bindings for open-team secrets, diagnostics, membership, and jobs."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import json
import os
import re

import sqlalchemy as sa
from fastapi import FastAPI, HTTPException, Request

from frisket.engine.jobs import model_pull_store
from frisket.engine.jobs.artifact_ref import normalize_artifact_ref
from frisket.engine.jobs.model_pull import (
    InvalidModelRefError,
    ModelPullEnqueueError,
    schedule_model_pull,
)
from frisket.engine.jobs.queue import MODEL_PULL_KIND, inserted_id
from frisket.ai.llm.endpoint_config import resolve_env_local_endpoint
from frisket.server import provider_config
from frisket.contracts.http.organization_providers import (
    OrganizationKeyDelete,
    OrganizationKeyList,
    OrganizationKeySave,
    OrganizationKeySaveRequest,
    OrganizationKeyValidation,
    OrganizationKeyValidationRequest,
)
from frisket.contracts.http.organization_operations import (
    OrganizationEnvDelete,
    OrganizationEnvList,
    OrganizationEnvSave,
    OrganizationEnvSaveRequest,
)
from frisket.contracts.http.local_providers import LocalEndpointCatalog
from frisket.contracts.http.team_local_models import (
    TeamArtifactPullRequest,
    TeamEngineSetupRequest,
    TeamModelPull,
    TeamModelPullListResponse,
    TeamModelPullStartResponse,
    team_model_http_error_responses,
)
from frisket.contracts.http.error_intake import (
    ClientErrorAccepted,
    ClientErrorReportRequest,
)
from frisket.server.route_errors import http_error_responses
from frisket.security.secrets import key_hint
from frisket.team.admin_browser_service import (
    AdminMembershipConflict,
    AdminMembershipError,
    AdminMembershipForbidden,
    AdminMembershipInvalid,
    AdminMembershipNotFound,
    AdminMembershipService,
)
from frisket.server.routes.approved_endpoints import (
    ApprovedEndpointAccess,
    register_approved_endpoint_routes,
)
from frisket.team.control_plane import ORGANIZATION_KEY_PROVIDERS
from frisket.team.db import atomic_upsert, locked_transaction
from frisket.team.diagnostics import sanitize_text
from frisket.team.observability_routes import sanitize_client_tree
from frisket.team.schema import (
    client_errors,
    memberships,
    organization_env_vars,
    organization_keys,
    record_audit,
)
from frisket.team.secret_box import TeamSecretBox

_ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")
_RESERVED_ORGANIZATION_ENV_NAMES = frozenset(
    {"FRISKET_MODELS_URL", "FRISKET_MODELS_TOKEN"}
)


def _admin_membership_http_error(exc: AdminMembershipError) -> HTTPException:
    if isinstance(exc, AdminMembershipForbidden):
        return HTTPException(403, str(exc))
    if isinstance(exc, AdminMembershipInvalid):
        return HTTPException(400, str(exc))
    if isinstance(exc, AdminMembershipNotFound):
        return HTTPException(404, str(exc))
    if isinstance(exc, AdminMembershipConflict):
        return HTTPException(409, str(exc))
    raise TypeError(f"unmapped membership error: {type(exc).__name__}") from exc


def register_operational_routes(
    app: FastAPI,
    *,
    engine: sa.Engine,
    organization_id: int,
    require_user: Callable[..., dict[str, Any]],
    resolve_user: Callable[[Request], dict[str, Any] | None],
    membership_role: Callable[[int, int], str | None],
    validate_provider_key: Callable[[str, str], Awaitable[bool]] | None,
    secret_box: TeamSecretBox,
    workspace: Any,
    admin_membership: AdminMembershipService,
) -> None:
    def require_owner(request: Request) -> dict[str, Any]:
        user = require_user(request)
        if membership_role(int(user["id"]), organization_id) != "owner":
            raise HTTPException(403, "organization owner required")
        return user

    def require_member(request: Request) -> dict[str, Any]:
        user = require_user(request)
        if membership_role(int(user["id"]), organization_id) is None:
            raise HTTPException(403, "organization membership required")
        return user

    register_approved_endpoint_routes(
        app,
        workspace_root=workspace.root,
        access=ApprovedEndpointAccess(
            path="/api/organization/approved-endpoints",
            name_prefix="organization_",
            require_reader=require_member,
            require_manager=require_owner,
        ),
    )

    def require_owner_in_transaction(cx: sa.Connection, actor: dict[str, Any]) -> None:
        role = cx.execute(
            sa.select(memberships.c.role).where(
                memberships.c.organization_id == organization_id,
                memberships.c.user_id == int(actor["id"]),
            )
        ).scalar_one_or_none()
        if role != "owner":
            raise HTTPException(403, "organization owner required")

    @app.post(
        "/api/organization/keys",
        response_model=OrganizationKeySave,
        responses=http_error_responses(400, 401, 403, 422, 500),
    )
    def set_organization_key(
        request: Request, body: OrganizationKeySaveRequest
    ) -> dict[str, str]:
        actor = require_owner(request)
        provider, value = body.provider.lower().strip(), body.key
        if provider not in ORGANIZATION_KEY_PROVIDERS:
            raise HTTPException(400, "unsupported provider")
        if not value.strip():
            raise HTTPException(400, "provider key must not be empty")
        try:
            provider_config.require_validation_token(
                provider, value, body.validation_token
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        with locked_transaction(
            engine, lock_scope=("organization-key", organization_id, provider)
        ) as cx:
            require_owner_in_transaction(cx, actor)
            values = {"encrypted": secret_box.encrypt(value), "hint": key_hint(value)}
            atomic_upsert(
                cx,
                table=organization_keys,
                values={
                    "organization_id": organization_id,
                    "provider": provider,
                    **values,
                },
                conflict_columns=("organization_id", "provider"),
                update_values=values,
            )
            record_audit(
                cx,
                user_id=actor["id"],
                organization_id=organization_id,
                action="organization_key_set",
                detail=provider,
            )
        return {"provider": provider, "hint": key_hint(value)}

    @app.get(
        "/api/organization/keys",
        response_model=OrganizationKeyList,
        responses=http_error_responses(401, 403, 500),
    )
    def list_organization_keys(request: Request) -> list[dict[str, str]]:
        require_member(request)
        with engine.connect() as cx:
            rows = cx.execute(
                sa.select(organization_keys.c.provider, organization_keys.c.hint)
                .where(organization_keys.c.organization_id == organization_id)
                .order_by(organization_keys.c.provider)
            ).all()
        return [dict(row._mapping) for row in rows]

    @app.delete(
        "/api/organization/keys/{provider}",
        response_model=OrganizationKeyDelete,
        responses=http_error_responses(400, 401, 403, 500),
    )
    def delete_organization_key(provider: str, request: Request) -> dict[str, bool]:
        actor = require_owner(request)
        if provider not in ORGANIZATION_KEY_PROVIDERS:
            raise HTTPException(400, "unsupported provider")
        with locked_transaction(
            engine, lock_scope=("organization-key", organization_id, provider)
        ) as cx:
            require_owner_in_transaction(cx, actor)
            result = cx.execute(
                organization_keys.delete().where(
                    organization_keys.c.organization_id == organization_id,
                    organization_keys.c.provider == provider,
                )
            )
            if result.rowcount:
                record_audit(
                    cx,
                    user_id=actor["id"],
                    organization_id=organization_id,
                    action="organization_key_deleted",
                    detail=provider,
                )
        return {"deleted": bool(result.rowcount)}

    @app.post(
        "/api/organization/keys/validate",
        response_model=OrganizationKeyValidation,
        responses=http_error_responses(400, 401, 403, 404, 422, 500, 501),
    )
    async def validate_organization_key(
        request: Request, body: OrganizationKeyValidationRequest
    ) -> dict[str, Any]:
        actor = require_owner(request)
        provider = body.provider.lower().strip()
        if provider not in ORGANIZATION_KEY_PROVIDERS:
            raise HTTPException(400, "unsupported provider")
        candidate = body.key.strip()
        if candidate:
            key = candidate
        else:
            with engine.connect() as cx:
                encrypted = cx.execute(
                    sa.select(organization_keys.c.encrypted).where(
                        organization_keys.c.organization_id == organization_id,
                        organization_keys.c.provider == provider,
                    )
                ).scalar_one_or_none()
            if encrypted is None:
                raise HTTPException(404, "provider key is not configured")
            key = secret_box.decrypt(encrypted)
        if validate_provider_key is None:
            raise HTTPException(501, "provider validation is not configured")
        with engine.begin() as cx:
            record_audit(
                cx,
                user_id=actor["id"],
                organization_id=organization_id,
                action="organization_key_validation_requested",
                detail=provider,
            )
        try:
            ok = bool(await validate_provider_key(provider, key))
            reachable = True
            status = 200 if ok else 401
            detail = None if ok else "provider rejected the credential"
        except Exception:  # noqa: BLE001 - canonical safe probe result
            ok = False
            reachable = False
            status = None
            detail = "provider validation failed"
        return {
            "provider": provider,
            "ok": ok,
            "reachable": reachable,
            "status": status,
            "detail": detail,
            "validation_token": (
                provider_config.issue_validation_token(provider, key)
                if candidate and ok
                else None
            ),
        }

    @app.post(
        "/api/organization/env",
        response_model=OrganizationEnvSave,
        response_model_exclude_unset=True,
        responses=http_error_responses(400, 401, 403, 422, 500),
    )
    def set_organization_env_var(
        request: Request, body: OrganizationEnvSaveRequest
    ) -> dict[str, str]:
        actor = require_owner(request)
        name, value = body.name.strip().upper(), body.value
        if not _ENV_NAME_RE.fullmatch(name):
            raise HTTPException(400, "invalid environment variable name")
        if name in _RESERVED_ORGANIZATION_ENV_NAMES:
            raise HTTPException(
                400, "this environment variable is managed by Models gateway"
            )
        if not value:
            raise HTTPException(400, "environment variable value must not be empty")
        with locked_transaction(
            engine, lock_scope=("organization-env", organization_id, name)
        ) as cx:
            require_owner_in_transaction(cx, actor)
            values = {"encrypted": secret_box.encrypt(value), "hint": key_hint(value)}
            atomic_upsert(
                cx,
                table=organization_env_vars,
                values={"organization_id": organization_id, "name": name, **values},
                conflict_columns=("organization_id", "name"),
                update_values=values,
            )
            record_audit(
                cx,
                user_id=actor["id"],
                organization_id=organization_id,
                action="organization_env_set",
                detail=name,
            )
        return {"name": name, "hint": key_hint(value)}

    @app.get(
        "/api/organization/env",
        response_model=OrganizationEnvList,
        response_model_exclude_unset=True,
        responses=http_error_responses(401, 403, 500),
    )
    def list_organization_env_vars(request: Request) -> list[dict[str, str]]:
        require_member(request)
        with engine.connect() as cx:
            rows = cx.execute(
                sa.select(
                    organization_env_vars.c.name, organization_env_vars.c.hint
                ).where(organization_env_vars.c.organization_id == organization_id)
            ).all()
        return [dict(row._mapping) for row in rows]

    @app.delete(
        "/api/organization/env/{name}",
        response_model=OrganizationEnvDelete,
        response_model_exclude_unset=True,
        responses=http_error_responses(400, 401, 403, 500),
    )
    def delete_organization_env_var(name: str, request: Request) -> dict[str, bool]:
        actor = require_owner(request)
        canonical = name.strip().upper()
        if not _ENV_NAME_RE.fullmatch(canonical):
            raise HTTPException(400, "invalid environment variable name")
        if canonical in _RESERVED_ORGANIZATION_ENV_NAMES:
            raise HTTPException(
                400, "this environment variable is managed by Models gateway"
            )
        with locked_transaction(
            engine, lock_scope=("organization-env", organization_id, canonical)
        ) as cx:
            require_owner_in_transaction(cx, actor)
            result = cx.execute(
                organization_env_vars.delete().where(
                    organization_env_vars.c.organization_id == organization_id,
                    organization_env_vars.c.name == canonical,
                )
            )
            if result.rowcount:
                record_audit(
                    cx,
                    user_id=actor["id"],
                    organization_id=organization_id,
                    action="organization_env_deleted",
                    detail=canonical,
                )
        return {"deleted": bool(result.rowcount)}

    # ---------- team local-model catalog + provisioning ----------
    # The team tier's live local-model surface. Organization surfaces resolve ONLY
    # the operator env bundle (`provider_config.resolve_env_local_endpoint`)
    # -- never a per-workspace file, which is local-tier UI infrastructure
    # with no organization analogue.

    @app.get(
        "/api/organization/local-endpoints",
        response_model=LocalEndpointCatalog,
        response_model_exclude_unset=True,
        responses=team_model_http_error_responses(401, 403, 500),
    )
    def list_organization_local_endpoints(request: Request) -> dict[str, Any]:
        require_member(request)
        env = dict(os.environ)
        config, _notes = resolve_env_local_endpoint(env)
        if config is None:
            return {"schemaVersion": "frisket.local_endpoints.v1", "endpoints": []}
        return {
            "schemaVersion": "frisket.local_endpoints.v1",
            "endpoints": [
                provider_config.local_endpoint_catalog_entry(
                    workspace.root,
                    config,
                    env,
                    authority="organization",
                )
            ],
        }

    def _organization_enqueue_pull_and_respond(
        *,
        canonical: str,
        endpoint_id: str | None,
        endpoint_origin: str | None,
        actor,
        payload_extra: dict,
        audit_action: str = "model_pull_requested",
    ) -> dict[str, Any]:
        # Owner re-check + audit are atomic together; the enqueue runs against
        # the run-queue engine (a different database) and cannot join it.
        with locked_transaction(
            engine, lock_scope=("organization-local-model-pull", organization_id)
        ) as cx:
            require_owner_in_transaction(cx, actor)
            target = f"{canonical}@{endpoint_origin}" if endpoint_origin else canonical
            record_audit(
                cx,
                user_id=actor["id"],
                organization_id=organization_id,
                action=audit_action,
                detail=target,
            )

        try:
            row, created = schedule_model_pull(
                workspace.queue,
                workspace_root=workspace.root,
                model_ref=canonical,
                initiated_by=str(actor["id"]),
                endpoint_id=endpoint_id,
                endpoint_origin=endpoint_origin,
                queue_payload_extra={
                    **payload_extra,
                    **workspace.queue_payload_extra,
                },
            )
        except model_pull_store.ModelPullBusyError as exc:
            active = getattr(exc, "active", None)
            detail: dict[str, Any] = {
                "code": "pull_busy",
                "message": (
                    f"a pull is already in progress for {active.model_ref}"
                    if active is not None
                    else "a pull is already in progress for this workspace"
                ),
            }
            if active is not None:
                detail["active"] = model_pull_store.to_dto(active, allow_remove=False)
            raise HTTPException(409, detail) from exc
        except ModelPullEnqueueError as exc:
            raise HTTPException(
                503,
                {
                    "code": "enqueue_failed",
                    "message": "failed to schedule the model pull",
                },
            ) from exc
        return {
            "pull": model_pull_store.to_dto(row, allow_remove=False),
            "deduplicated": not created,
        }

    @app.post(
        "/api/organization/models/pull",
        status_code=202,
        response_model=TeamModelPullStartResponse,
        responses=team_model_http_error_responses(400, 401, 403, 409, 422, 500, 503),
    )
    def post_organization_models_pull(
        request: Request,
        body: TeamArtifactPullRequest,
    ) -> dict[str, Any]:
        """Owner-gated, audited pull for any supported artifact scheme."""
        actor = require_owner(request)
        env = dict(os.environ)
        try:
            art = normalize_artifact_ref(body.ref)
        except InvalidModelRefError as exc:
            raise HTTPException(
                400, {"code": "invalid_model_ref", "message": str(exc)}
            ) from exc

        endpoint_id: str | None = None
        endpoint_origin: str | None = None
        payload_extra: dict = {}
        if art.is_ollama:
            local_endpoint, _notes = resolve_env_local_endpoint(env)
            if local_endpoint is None:
                raise HTTPException(
                    503,
                    {
                        "code": "local_server_unreachable",
                        "message": (
                            "no local-model endpoint is configured for this organization"
                        ),
                    },
                )
            if art.endpoint_id != local_endpoint.endpoint_id:
                raise HTTPException(
                    404,
                    {
                        "code": "local_endpoint_not_found",
                        "message": f"unknown local model endpoint: {art.endpoint_id}",
                    },
                )
            if not local_endpoint.pull_enabled:
                raise HTTPException(
                    409,
                    {
                        "code": "model_pull_disabled",
                        "message": "model download is not enabled for this endpoint",
                    },
                )
            endpoint_origin = local_endpoint.origin
            endpoint_id = local_endpoint.endpoint_id
            payload_extra["endpoint_id"] = local_endpoint.endpoint_id
            probe = provider_config.ollama_reachable(
                endpoint_origin,
                token=local_endpoint.bearer_for_provisioning(),
                edge_auth=local_endpoint.edge_auth,
            )
            if not probe.get("reachable"):
                raise HTTPException(
                    503,
                    {
                        "code": "local_server_unreachable",
                        "message": f"no local server answered at {endpoint_origin}",
                        "url": endpoint_origin,
                    },
                )
            if probe.get("auth_status") == "unauthorized":
                raise HTTPException(
                    409,
                    {
                        "code": "local_server_unauthorized",
                        "message": (
                            f"the local server at {endpoint_origin} "
                            "requires authentication"
                        ),
                        "url": endpoint_origin,
                    },
                )
            if probe.get("protocol") != "ollama_native":
                raise HTTPException(
                    409,
                    {
                        "code": "pull_unsupported",
                        "message": (
                            "your server lists models but does not accept downloads"
                        ),
                        "url": endpoint_origin,
                        "protocol": probe.get("protocol"),
                    },
                )
        if art.scheme == "hf":
            from frisket.ai.models import artifact_manifest

            if artifact_manifest.lookup(art.canonical) is None:
                if not body.unpinned_acknowledged:
                    raise HTTPException(
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
        return _organization_enqueue_pull_and_respond(
            canonical=art.canonical,
            endpoint_id=endpoint_id,
            endpoint_origin=endpoint_origin,
            actor=actor,
            payload_extra=payload_extra,
        )

    @app.post(
        "/api/organization/models/setup",
        status_code=202,
        response_model=TeamModelPullStartResponse,
        responses=team_model_http_error_responses(400, 401, 403, 409, 422, 500, 503),
    )
    def setup_organization_model_engine(
        request: Request, body: TeamEngineSetupRequest
    ) -> dict[str, Any]:
        """Owner-gated, audited start for an allowlisted local engine bundle."""
        from frisket.engine.jobs.engine_setup import (
            EMBEDDINGGEMMA_SETUP_REF,
            INDRA_OCR_SETUP_REF,
            PARAKEET_TDT_SETUP_REF,
            RAPIDOCR_SETUP_REF,
        )

        actor = require_owner(request)
        if body.setup_ref not in {
            PARAKEET_TDT_SETUP_REF,
            RAPIDOCR_SETUP_REF,
            INDRA_OCR_SETUP_REF,
            EMBEDDINGGEMMA_SETUP_REF,
        }:
            raise HTTPException(
                400,
                {
                    "code": "unknown_engine_setup",
                    "message": "the requested engine setup is not supported",
                },
            )
        return _organization_enqueue_pull_and_respond(
            canonical=body.setup_ref,
            endpoint_id=None,
            endpoint_origin=None,
            actor=actor,
            payload_extra={},
            audit_action="engine_setup_requested",
        )

    @app.get(
        "/api/organization/models/pulls",
        response_model=TeamModelPullListResponse,
        responses=team_model_http_error_responses(401, 403, 500),
    )
    def list_organization_model_pulls(request: Request) -> dict[str, Any]:
        require_member(request)
        queue_engine = workspace.queue.engine
        rows = model_pull_store.list_recent(queue_engine, str(workspace.root), limit=20)
        return {"pulls": [model_pull_store.to_dto(r, allow_remove=False) for r in rows]}

    @app.get(
        "/api/organization/models/pulls/{pull_id}",
        response_model=TeamModelPull,
        responses=team_model_http_error_responses(401, 403, 404, 422, 500),
    )
    def get_organization_model_pull(pull_id: int, request: Request) -> dict[str, Any]:
        require_member(request)
        queue_engine = workspace.queue.engine
        row = model_pull_store.get(queue_engine, pull_id)
        if row is None or row.workspace_root != str(workspace.root):
            raise HTTPException(
                404,
                {
                    "code": "pull_not_found",
                    "message": f"no pull with id {pull_id} in this workspace",
                },
            )
        return model_pull_store.to_dto(row, allow_remove=False)

    @app.post(
        "/api/organization/models/pulls/{pull_id}/cancel",
        status_code=202,
        response_model=TeamModelPull,
        responses=team_model_http_error_responses(401, 403, 404, 409, 422, 500),
    )
    def cancel_organization_model_pull(
        pull_id: int, request: Request
    ) -> dict[str, Any]:
        actor = require_owner(request)
        queue_engine = workspace.queue.engine
        row = model_pull_store.get(queue_engine, pull_id)
        if row is None or row.workspace_root != str(workspace.root):
            raise HTTPException(
                404,
                {
                    "code": "pull_not_found",
                    "message": f"no pull with id {pull_id} in this workspace",
                },
            )
        with locked_transaction(
            engine, lock_scope=("organization-local-model-pull", organization_id)
        ) as cx:
            require_owner_in_transaction(cx, actor)
        # The queue-level mutation runs AFTER the owner-gated lock releases
        # (never nested inside it): `queue_engine` and `engine` can be the
        # SAME physical database in a team deployment, and nesting a second
        # write transaction on that database inside the first's still-open
        # transaction deadlocks/locks out on sqlite and is needless
        # contention on Postgres too.
        #
        # Order matters here: a running `model.pull` handler polls
        # `cancel_requested_at` on the row with NO org/kind scoping of its
        # own (it only knows its own pull_id), so stamping that cooperative
        # flag before the authorization check below has refused would let a
        # refused cancel silently stop the pull anyway, with no audit trail.
        # Attempt the actual cancel FIRST; only stamp the flag and audit on
        # a PROVEN success; a refusal returns 409 without ever touching the
        # row.
        if row.job_id is not None:
            # Org/kind-scoped cancel: never a plain fetch-then-authorize
            # `queue.cancel`. A job id that does not belong to THIS org/kind
            # is refused atomically by the WHERE clause itself.
            cancel_succeeded = workspace.queue.cancel_scoped(
                row.job_id,
                organization_id=str(organization_id),
                kind=MODEL_PULL_KIND,
            )
            if cancel_succeeded:
                model_pull_store.request_cancel(queue_engine, pull_id)
        else:
            # Job-less row (an enqueue-wedge remnant, or a pre-existing
            # never-enqueued row): there is no queue job to scope a cancel
            # through, and none of the org/kind refusal cases above can apply
            # -- ownership of this row was already established by the
            # workspace-scoped lookup above. Stamp the cooperative flag (for
            # DTO/observability parity with the scoped path) then terminalize
            # the row directly; `mark_cancelled`'s own ACTIVE_STATUSES guard
            # is the authority on whether there was actually anything to
            # cancel (an already-terminal row correctly reports failure
            # here).
            model_pull_store.request_cancel(queue_engine, pull_id)
            cancel_succeeded = model_pull_store.mark_cancelled(queue_engine, pull_id)
        if not cancel_succeeded:
            raise HTTPException(
                409,
                {
                    "code": "pull_not_cancellable",
                    "message": "this pull cannot be cancelled",
                },
            )
        with locked_transaction(
            engine, lock_scope=("organization-local-model-pull", organization_id)
        ) as cx:
            record_audit(
                cx,
                user_id=actor["id"],
                organization_id=organization_id,
                action="model_pull_cancelled",
                detail=f"{pull_id}:{row.model_ref}",
            )
        row = model_pull_store.get(queue_engine, pull_id)
        assert row is not None
        return model_pull_store.to_dto(row, allow_remove=False)

    @app.post(
        "/api/client-errors",
        status_code=202,
        name="client_errors",
        response_model=ClientErrorAccepted,
        response_model_exclude_unset=True,
    )
    def client_errors_route(
        request: Request, body: ClientErrorReportRequest
    ) -> ClientErrorAccepted:
        user = resolve_user(request)
        now = datetime.now(UTC)
        with engine.begin() as cx:
            identity_filter = (
                client_errors.c.user_id == user["id"]
                if user
                else client_errors.c.user_id.is_(None)
            )
            recent = cx.execute(
                sa.select(sa.func.count())
                .select_from(client_errors)
                .where(
                    client_errors.c.organization_id == organization_id,
                    identity_filter,
                    client_errors.c.created_at >= now - timedelta(minutes=1),
                )
            ).scalar_one()
            if recent >= 20:
                raise HTTPException(429, "client error report rate limit exceeded")
            cx.execute(
                client_errors.delete().where(
                    client_errors.c.organization_id == organization_id,
                    client_errors.c.created_at < now - timedelta(days=30),
                )
            )
            inserted = cx.execute(
                client_errors.insert().values(
                    organization_id=organization_id,
                    user_id=user["id"] if user else None,
                    source=sanitize_text(body.source, limit=80),
                    severity=sanitize_text(body.severity, limit=40),
                    name=sanitize_text(body.name, limit=200) if body.name else None,
                    message=sanitize_text(body.message, limit=1000),
                    stack=sanitize_text(body.stack, limit=5000) if body.stack else None,
                    route=sanitize_text(body.route, limit=400) if body.route else None,
                    context_json=json.dumps(
                        sanitize_client_tree(body.context or {}),
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                )
            )
            record_id = inserted_id(inserted)
            keep = (
                cx.execute(
                    sa.select(client_errors.c.id)
                    .where(client_errors.c.organization_id == organization_id)
                    .order_by(client_errors.c.id.desc())
                    .limit(5000)
                )
                .scalars()
                .all()
            )
            if keep:
                cx.execute(
                    client_errors.delete().where(
                        client_errors.c.organization_id == organization_id,
                        client_errors.c.id < min(keep),
                    )
                )
        return ClientErrorAccepted.model_validate(
            {"accepted": True, "ok": True, "id": record_id}
        )

    @app.delete("/api/admin/users/{user_ref}")
    def admin_remove_user(user_ref: str, request: Request) -> dict[str, bool]:
        """Remove by numeric user id (the SPA's form) or by email (the remote
        CLI's form). The email form additionally revokes a pending invite, so
        one verb retires an address whether or not it ever signed in."""
        actor = require_user(request, browser=True, admin=True)
        try:
            result = admin_membership.remove_user(actor=actor, user_ref=user_ref)
        except AdminMembershipError as exc:
            raise _admin_membership_http_error(exc) from exc
        return {"removed": True, "self": bool(result["self"])}


__all__ = ["register_operational_routes"]
