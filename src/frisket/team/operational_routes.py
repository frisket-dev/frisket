"""HTTP bindings for open-team secrets, diagnostics, membership, and jobs."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import json
import os
import re

import sqlalchemy as sa
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from frisket.engine.jobs import model_pull_store
from frisket.engine.jobs.artifact_ref import normalize_artifact_ref
from frisket.engine.jobs.model_pull import (
    MODEL_PULL_MAX_ATTEMPTS,
    InvalidModelRefError,
)
from frisket.engine.jobs.queue import MODEL_PULL_KIND
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
    TeamModelPull,
    TeamModelPullListResponse,
    TeamModelPullStartResponse,
    team_model_http_error_responses,
)
from frisket.contracts.http.error_intake import (
    ClientErrorAccepted,
    ClientErrorReportRequest,
)
from frisket.contracts.http.models import WireModel
from frisket.server.route_errors import http_error_responses
from frisket.team.security.secrets import key_hint
from frisket.team.admin_browser_service import (
    AdminMembershipConflict,
    AdminMembershipError,
    AdminMembershipForbidden,
    AdminMembershipInvalid,
    AdminMembershipNotFound,
    AdminMembershipService,
)
from frisket.team.control_plane import MODEL_KEY_PROVIDERS
from frisket.team.db import atomic_upsert, locked_transaction
from frisket.team.diagnostics import sanitize_text
from frisket.team.observability_routes import sanitize_client_tree
from frisket.team.schema import (
    audit_log,
    client_errors,
    memberships,
    org_env_vars,
    org_keys,
)
from frisket.team.secret_box import TeamSecretBox

_ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")


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


class OperationalBody(BaseModel):
    email: str = ""
    name: str = ""
    role: str = ""
    provider: str = ""
    key: str = ""
    value: str = ""
    message: str = ""
    source: str = "browser"
    validation_token: str | None = None


class AdminUserRoleRequest(WireModel):
    role: Literal["owner", "member"]


def register_operational_routes(
    app: FastAPI,
    *,
    engine: sa.Engine,
    org_id: int,
    require_user: Callable[..., dict[str, Any]],
    resolve_user: Callable[[Request], dict[str, Any] | None],
    membership_role: Callable[[int, int], str | None],
    run_queue: Any,
    validate_provider_key: Callable[[str, str], Awaitable[bool]] | None,
    secret_box: TeamSecretBox,
    workspace: Any,
    admin_membership: AdminMembershipService,
) -> None:
    def audit(
        cx: sa.Connection, *, actor: dict[str, Any], action: str, target: str
    ) -> None:
        cx.execute(
            audit_log.insert().values(
                user_id=actor["id"],
                org_id=org_id,
                action=action,
                detail=target,
            )
        )

    def require_owner(request: Request) -> dict[str, Any]:
        user = require_user(request)
        if membership_role(int(user["id"]), org_id) != "owner":
            raise HTTPException(403, "organization owner required")
        return user

    def require_member(request: Request) -> dict[str, Any]:
        user = require_user(request)
        if membership_role(int(user["id"]), org_id) is None:
            raise HTTPException(403, "organization membership required")
        return user

    def require_owner_in_transaction(cx: sa.Connection, actor: dict[str, Any]) -> None:
        role = cx.execute(
            sa.select(memberships.c.role).where(
                memberships.c.org_id == org_id,
                memberships.c.user_id == int(actor["id"]),
            )
        ).scalar_one_or_none()
        if role != "owner":
            raise HTTPException(403, "organization owner required")

    @app.post(
        "/api/org/keys",
        response_model=OrganizationKeySave,
        responses=http_error_responses(400, 401, 403, 422, 500),
    )
    def set_org_key(
        request: Request, body: OrganizationKeySaveRequest
    ) -> dict[str, str]:
        actor = require_owner(request)
        provider, value = body.provider.lower().strip(), body.key
        if provider not in MODEL_KEY_PROVIDERS:
            raise HTTPException(400, "unsupported provider")
        if not value.strip():
            raise HTTPException(400, "provider key must not be empty")
        if body.validation_token is not None:
            try:
                provider_config.require_validation_token(
                    provider, value, body.validation_token
                )
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
        with locked_transaction(engine, lock_scope=("org-key", org_id, provider)) as cx:
            require_owner_in_transaction(cx, actor)
            values = {"encrypted": secret_box.encrypt(value), "hint": key_hint(value)}
            atomic_upsert(
                cx,
                table=org_keys,
                values={"org_id": org_id, "provider": provider, **values},
                conflict_columns=("org_id", "provider"),
                update_values=values,
            )
            audit(cx, actor=actor, action="org_key_set", target=provider)
        return {"provider": provider, "hint": key_hint(value)}

    @app.get(
        "/api/org/keys",
        response_model=OrganizationKeyList,
        responses=http_error_responses(401, 403, 500),
    )
    def list_org_keys(request: Request) -> list[dict[str, str]]:
        require_member(request)
        with engine.connect() as cx:
            rows = cx.execute(
                sa.select(org_keys.c.provider, org_keys.c.hint)
                .where(org_keys.c.org_id == org_id)
                .order_by(org_keys.c.provider)
            ).all()
        return [dict(row._mapping) for row in rows]

    @app.delete(
        "/api/org/keys/{provider}",
        response_model=OrganizationKeyDelete,
        responses=http_error_responses(400, 401, 403, 500),
    )
    def delete_org_key(provider: str, request: Request) -> dict[str, bool]:
        actor = require_owner(request)
        if provider not in MODEL_KEY_PROVIDERS:
            raise HTTPException(400, "unsupported provider")
        with locked_transaction(engine, lock_scope=("org-key", org_id, provider)) as cx:
            require_owner_in_transaction(cx, actor)
            result = cx.execute(
                org_keys.delete().where(
                    org_keys.c.org_id == org_id, org_keys.c.provider == provider
                )
            )
            if result.rowcount:
                audit(cx, actor=actor, action="org_key_deleted", target=provider)
        return {"deleted": bool(result.rowcount)}

    @app.post(
        "/api/org/keys/validate",
        response_model=OrganizationKeyValidation,
        responses=http_error_responses(400, 401, 403, 404, 422, 500, 501),
    )
    async def validate_org_key(
        request: Request, body: OrganizationKeyValidationRequest
    ) -> dict[str, Any]:
        actor = require_owner(request)
        provider = body.provider.lower().strip()
        if provider not in MODEL_KEY_PROVIDERS:
            raise HTTPException(400, "unsupported provider")
        candidate = body.key.strip()
        if candidate:
            key = candidate
        else:
            with engine.connect() as cx:
                encrypted = cx.execute(
                    sa.select(org_keys.c.encrypted).where(
                        org_keys.c.org_id == org_id,
                        org_keys.c.provider == provider,
                    )
                ).scalar_one_or_none()
            if encrypted is None:
                raise HTTPException(404, "provider key is not configured")
            key = secret_box.decrypt(encrypted)
        if validate_provider_key is None:
            raise HTTPException(501, "provider validation is not configured")
        with engine.begin() as cx:
            audit(
                cx, actor=actor, action="org_key_validation_requested", target=provider
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
        "/api/org/env",
        response_model=OrganizationEnvSave,
        response_model_exclude_unset=True,
        responses=http_error_responses(400, 401, 403, 422, 500),
    )
    def set_org_env_var(
        request: Request, body: OrganizationEnvSaveRequest
    ) -> dict[str, str]:
        actor = require_owner(request)
        name, value = body.name.strip().upper(), body.value
        if not _ENV_NAME_RE.fullmatch(name):
            raise HTTPException(400, "invalid environment variable name")
        if not value:
            raise HTTPException(400, "environment variable value must not be empty")
        with locked_transaction(engine, lock_scope=("org-env", org_id, name)) as cx:
            require_owner_in_transaction(cx, actor)
            values = {"encrypted": secret_box.encrypt(value), "hint": key_hint(value)}
            atomic_upsert(
                cx,
                table=org_env_vars,
                values={"org_id": org_id, "name": name, **values},
                conflict_columns=("org_id", "name"),
                update_values=values,
            )
            audit(cx, actor=actor, action="org_env_set", target=name)
        return {"name": name, "hint": key_hint(value)}

    @app.get(
        "/api/org/env",
        response_model=OrganizationEnvList,
        response_model_exclude_unset=True,
        responses=http_error_responses(401, 403, 500),
    )
    def list_org_env_vars(request: Request) -> list[dict[str, str]]:
        require_member(request)
        with engine.connect() as cx:
            rows = cx.execute(
                sa.select(org_env_vars.c.name, org_env_vars.c.hint).where(
                    org_env_vars.c.org_id == org_id
                )
            ).all()
        return [dict(row._mapping) for row in rows]

    @app.delete(
        "/api/org/env/{name}",
        response_model=OrganizationEnvDelete,
        response_model_exclude_unset=True,
        responses=http_error_responses(400, 401, 403, 500),
    )
    def delete_org_env_var(name: str, request: Request) -> dict[str, bool]:
        actor = require_owner(request)
        canonical = name.strip().upper()
        if not _ENV_NAME_RE.fullmatch(canonical):
            raise HTTPException(400, "invalid environment variable name")
        with locked_transaction(
            engine, lock_scope=("org-env", org_id, canonical)
        ) as cx:
            require_owner_in_transaction(cx, actor)
            result = cx.execute(
                org_env_vars.delete().where(
                    org_env_vars.c.org_id == org_id, org_env_vars.c.name == canonical
                )
            )
            if result.rowcount:
                audit(cx, actor=actor, action="org_env_deleted", target=canonical)
        return {"deleted": bool(result.rowcount)}

    # ---------- team local-model catalog + provisioning ----------
    # The team tier's live local-model surface. Org surfaces resolve ONLY
    # the operator env bundle (`provider_config.resolve_env_local_endpoint`)
    # -- never a per-workspace file, which is local-tier UI infrastructure
    # with no org analogue.

    @app.get(
        "/api/org/local-endpoints",
        response_model=LocalEndpointCatalog,
        response_model_exclude_unset=True,
        responses=team_model_http_error_responses(401, 403, 500),
    )
    def list_org_local_endpoints(request: Request) -> dict[str, Any]:
        require_member(request)
        env = dict(os.environ)
        config, _notes = provider_config.resolve_env_local_endpoint(env)
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

    def _org_enqueue_pull_and_respond(
        *,
        canonical: str,
        endpoint_id: str | None,
        endpoint_origin: str | None,
        actor,
        payload_extra: dict,
    ) -> dict[str, Any]:
        # Owner re-check + audit are atomic together; the enqueue runs against
        # the run-queue engine (a different database) and cannot join it.
        with locked_transaction(
            engine, lock_scope=("org-local-model-pull", org_id)
        ) as cx:
            require_owner_in_transaction(cx, actor)
            target = f"{canonical}@{endpoint_origin}" if endpoint_origin else canonical
            audit(cx, actor=actor, action="model_pull_requested", target=target)

        queue_engine = workspace.queue.engine
        root_str = str(workspace.root)
        try:
            row, created = model_pull_store.create_or_get_active(
                queue_engine,
                workspace_root=root_str,
                model_ref=canonical,
                initiated_by=str(actor["id"]),
                endpoint_id=endpoint_id,
                endpoint_origin=endpoint_origin,
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
                detail["active"] = model_pull_store.to_dto(active)
            raise HTTPException(409, detail) from exc
        if not created:
            return {"pull": model_pull_store.to_dto(row), "deduplicated": True}
        try:
            job_id = workspace.queue.enqueue(
                MODEL_PULL_KIND,
                {
                    "pull_id": row.id,
                    "workspace_root": root_str,
                    **payload_extra,
                    **workspace.queue_payload_extra,
                },
                max_attempts=MODEL_PULL_MAX_ATTEMPTS,
            )
        except Exception as exc:  # noqa: BLE001 -- must not leave an orphan active row
            model_pull_store.mark_failed(
                queue_engine,
                row.id,
                error_code="enqueue_failed",
                error_message=f"failed to enqueue the pull job ({type(exc).__name__})",
            )
            raise HTTPException(
                503,
                {
                    "code": "enqueue_failed",
                    "message": "failed to schedule the model pull",
                },
            ) from exc
        model_pull_store.set_job_id(queue_engine, row.id, job_id=job_id)
        row = model_pull_store.get(queue_engine, row.id)
        return {"pull": model_pull_store.to_dto(row), "deduplicated": False}

    @app.post(
        "/api/org/models/pull",
        status_code=202,
        response_model=TeamModelPullStartResponse,
        responses=team_model_http_error_responses(400, 401, 403, 409, 422, 500, 503),
    )
    def post_org_models_pull(
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
            local_endpoint, _notes = provider_config.resolve_env_local_endpoint(env)
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
        return _org_enqueue_pull_and_respond(
            canonical=art.canonical,
            endpoint_id=endpoint_id,
            endpoint_origin=endpoint_origin,
            actor=actor,
            payload_extra=payload_extra,
        )

    @app.get(
        "/api/org/models/pulls",
        response_model=TeamModelPullListResponse,
        responses=team_model_http_error_responses(401, 403, 500),
    )
    def list_org_model_pulls(request: Request) -> dict[str, Any]:
        require_member(request)
        queue_engine = workspace.queue.engine
        rows = model_pull_store.list_recent(queue_engine, str(workspace.root), limit=20)
        return {"pulls": [model_pull_store.to_dto(r) for r in rows]}

    @app.get(
        "/api/org/models/pulls/{pull_id}",
        response_model=TeamModelPull,
        responses=team_model_http_error_responses(401, 403, 404, 422, 500),
    )
    def get_org_model_pull(pull_id: int, request: Request) -> dict[str, Any]:
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
        return model_pull_store.to_dto(row)

    @app.post(
        "/api/org/models/pulls/{pull_id}/cancel",
        status_code=202,
        response_model=TeamModelPull,
        responses=team_model_http_error_responses(401, 403, 404, 409, 422, 500),
    )
    def cancel_org_model_pull(pull_id: int, request: Request) -> dict[str, Any]:
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
            engine, lock_scope=("org-local-model-pull", org_id)
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
                row.job_id, org_id=str(org_id), kind=MODEL_PULL_KIND
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
            engine, lock_scope=("org-local-model-pull", org_id)
        ) as cx:
            audit(
                cx,
                actor=actor,
                action="model_pull_cancelled",
                target=f"{pull_id}:{row.model_ref}",
            )
        row = model_pull_store.get(queue_engine, pull_id)
        return model_pull_store.to_dto(row)

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
                    client_errors.c.org_id == org_id,
                    identity_filter,
                    client_errors.c.created_at >= now - timedelta(minutes=1),
                )
            ).scalar_one()
            if recent >= 20:
                raise HTTPException(429, "client error report rate limit exceeded")
            cx.execute(
                client_errors.delete().where(
                    client_errors.c.org_id == org_id,
                    client_errors.c.created_at < now - timedelta(days=30),
                )
            )
            inserted = cx.execute(
                client_errors.insert().values(
                    org_id=org_id,
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
            record_id = int(inserted.inserted_primary_key[0])
            keep = (
                cx.execute(
                    sa.select(client_errors.c.id)
                    .where(client_errors.c.org_id == org_id)
                    .order_by(client_errors.c.id.desc())
                    .limit(5000)
                )
                .scalars()
                .all()
            )
            if keep:
                cx.execute(
                    client_errors.delete().where(
                        client_errors.c.org_id == org_id, client_errors.c.id < min(keep)
                    )
                )
        return ClientErrorAccepted.model_validate(
            {"accepted": True, "ok": True, "id": record_id}
        )

    @app.patch("/api/admin/users/{user_id}/role")
    def admin_update_user_role(
        user_id: int, request: Request, body: AdminUserRoleRequest
    ) -> dict[str, bool]:
        actor = require_user(request, browser=True, admin=True)
        try:
            result = admin_membership.update_role(
                actor=actor, user_id=user_id, role=body.role
            )
        except AdminMembershipError as exc:
            raise _admin_membership_http_error(exc) from exc
        return {"updated": True, "self": bool(result["self"])}

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

    def _remediate(job_id: int, action: str, request: Request) -> dict[str, bool]:
        # Ruling 4 ("a retry is a resume"): CANCEL is the only remediation an
        # operator gets. The admin retry/recover doors re-executed work
        # without passing the consent and cost gates the user's own doors
        # pass, and nothing else in the repo can do that. Re-execution is
        # run.backfill (the user's, consented) or the automatic budget-capped
        # requeue/stale-lease sweeps (internal). Making a run FREE is a
        # pricing-axis decision — price the run at $0 — not a retry door.
        actor = require_user(request, browser=True, admin=True)
        methods = {"cancel": run_queue.cancel}
        if action not in methods:
            raise HTTPException(404, "unknown job action")
        with engine.begin() as cx:
            audit(
                cx,
                actor=actor,
                action=f"admin_job_{action}_requested",
                target=str(job_id),
            )
        return {"ok": bool(methods[action](job_id))}

    @app.post("/api/admin/jobs/{job_id}/cancel")
    def admin_job_cancel(job_id: int, request: Request) -> dict[str, bool]:
        return _remediate(job_id, "cancel", request)


__all__ = ["AdminUserRoleRequest", "register_operational_routes"]
