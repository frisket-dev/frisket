"""Browser-administration HTTP adapters for the public team composition."""

from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import sqlalchemy as sa
from fastapi import FastAPI, HTTPException, Path as PathParam, Query, Request
from fastapi.responses import JSONResponse

from frisket.contracts.http.admin_browser import (
    AdminBrowserAuditQuery,
    AdminBrowserAuditV1,
    AdminBrowserCancelV1,
    AdminBrowserErrorsQuery,
    AdminBrowserErrorsV1,
    AdminBrowserHealthV1,
    AdminBrowserInviteRequest,
    AdminBrowserInviteResult,
    AdminBrowserJobsV1,
    AdminBrowserRemoveUserResult,
    AdminBrowserRevokeInviteResult,
    AdminBrowserTargetOrgQuery,
    AdminBrowserUpdateRoleRequest,
    AdminBrowserUpdateRoleResult,
    AdminBrowserUsersV1,
)
from frisket.contracts.http.admin_operations import (
    AdminAuditResponse,
    AdminErrorsResponse,
    AdminJobsResponse,
    AdminUsersResponse,
)
from frisket.engine.jobs.projection import admin_job_payload
from frisket.server.route_errors import http_error_responses
from frisket.team.admin_browser_service import (
    AdminMembershipConflict,
    AdminMembershipError,
    AdminMembershipForbidden,
    AdminMembershipInvalid,
    AdminMembershipNotFound,
    AdminMembershipService,
)
from frisket.team.identity_service import IdentityAuthService
from frisket.team.invite_service import (
    InviteConflict,
    InviteForbidden,
    InviteNotFound,
    InviteService,
)
from frisket.team.observability_routes import _blob_probe
from frisket.team.schema import (
    audit_log,
    client_errors,
    orgs,
    pending_invites,
    projects,
    users,
)


_JOB_STATUSES = ("queued", "running", "done", "failed", "cancelled")
_WORKER_LIVENESS_SECONDS = 60
_PROJECT_AUDIT_ACTIONS = frozenset(
    {
        "project_created",
        "project_deleted",
        "project_sensitivity_updated",
        "project_network_updated",
        "project_member_set",
        "project_member_removed",
        "project_invited",
        "project_invite_revoked",
        "project_invite_accepted",
    }
)


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    text = value.isoformat() if hasattr(value, "isoformat") else str(value)
    return text.replace("+00:00", "Z")


def _membership_http_error(exc: AdminMembershipError) -> HTTPException:
    status_code = 400
    if isinstance(exc, AdminMembershipForbidden):
        status_code = 403
    elif isinstance(exc, AdminMembershipNotFound):
        status_code = 404
    elif isinstance(exc, AdminMembershipConflict):
        status_code = 409
    elif not isinstance(exc, AdminMembershipInvalid):
        raise TypeError(f"unmapped membership error: {type(exc).__name__}") from exc
    return HTTPException(status_code, str(exc))


def _invite_http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, InviteNotFound):
        return HTTPException(404, str(exc))
    if isinstance(exc, InviteForbidden):
        return HTTPException(403, str(exc))
    if isinstance(exc, InviteConflict):
        return HTTPException(exc.status_code, str(exc))
    return HTTPException(400, str(exc))


def _audit_category(action: str) -> str:
    if "invite" in action or action == "invited":
        return "invite"
    if "role" in action or "member" in action:
        return "member"
    if action.startswith("run_") or action.startswith("admin_job_"):
        return "run"
    if "credit" in action or action.startswith("checkout"):
        return "billing"
    if action in {
        "login",
        "login_google",
        "org_key_set",
        "org_key_deleted",
        "org_env_set",
        "org_env_deleted",
        "pat_created",
        "pat_revoked",
    }:
        return "security"
    if action.startswith("project_"):
        return "project"
    return "system"


def _audit_target(action: str, detail: str | None) -> dict[str, Any]:
    text = detail or ""
    parts = text.split(":")
    if action in _PROJECT_AUDIT_ACTIONS and parts[0]:
        project_id = parts[0]
        object_type = "project"
        object_id = project_id
        if "member" in action:
            object_type = "project_member"
            object_id = parts[1] if len(parts) > 1 else None
        elif "invite" in action:
            object_type = "invite"
            object_id = parts[1] if len(parts) > 1 else None
        return {
            "project_id": project_id,
            "object_type": object_type,
            "object_id": object_id,
            "context": {"project_id": project_id},
        }
    if action.startswith("org_key_"):
        return {
            "project_id": None,
            "object_type": "org_key",
            "object_id": text or None,
            "context": {"provider": text} if text else {},
        }
    if action.startswith("org_env_"):
        return {
            "project_id": None,
            "object_type": "org_env",
            "object_id": text or None,
            "context": {"name": text} if text else {},
        }
    if action.startswith("pat_"):
        return {
            "project_id": None,
            "object_type": "api_token",
            "object_id": text or None,
            "context": {"prefix": text} if text else {},
        }
    if action.startswith("org_member_"):
        return {
            "project_id": None,
            "object_type": "org_member",
            "object_id": parts[0] or None,
            "context": {"user_id": parts[0]} if parts[0] else {},
        }
    if action.startswith("org_invite_"):
        return {
            "project_id": None,
            "object_type": "invite",
            "object_id": parts[0] or None,
            "context": {"email": parts[0]} if parts[0] else {},
        }
    return {
        "project_id": None,
        "object_type": None,
        "object_id": None,
        "context": {},
    }


def _json_object(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    try:
        decoded = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return decoded if isinstance(decoded, dict) else None


def register_admin_browser_routes(
    app: FastAPI,
    *,
    engine: sa.Engine,
    org_id: int,
    invites: InviteService,
    auth: IdentityAuthService,
    membership: AdminMembershipService,
    require_user: Callable[..., dict[str, Any]],
    base_url: str,
    send_mail: Callable[[str, str], Awaitable[bool]],
    run_queue: Any,
    workspace_root: Path,
) -> None:
    """Register the ten browser operations without changing legacy wires."""

    def require_admin(request: Request) -> dict[str, Any]:
        return require_user(request, browser=True, admin=True)

    @app.get(
        "/api/admin/browser/health",
        response_model=AdminBrowserHealthV1,
        responses=http_error_responses(401, 403, 500),
    )
    def admin_browser_health(request: Request) -> dict[str, Any]:
        require_admin(request)
        started = time.perf_counter()
        try:
            with engine.connect() as cx:
                cx.execute(sa.text("SELECT 1"))
            db = {
                "ok": True,
                "dialect": engine.dialect.name,
                "latency_ms": int((time.perf_counter() - started) * 1000),
                "error": None,
            }
        except Exception as exc:  # noqa: BLE001 - health is fail-soft
            db = {
                "ok": False,
                "dialect": engine.dialect.name,
                "latency_ms": None,
                "error": str(exc)[:300],
            }
        blob_probe = _blob_probe(workspace_root)
        blob = {
            "ok": bool(blob_probe.get("ok")),
            "error": None if blob_probe.get("ok") else "blob store probe failed",
        }
        try:
            raw_counts = dict(run_queue.counts())
            counts = {
                status: int(raw_counts.get(status, 0)) for status in _JOB_STATUSES
            }
            queue = {"ok": True, "counts": counts, "error": None}
        except Exception as exc:  # noqa: BLE001 - health is fail-soft
            counts = dict.fromkeys(_JOB_STATUSES, 0)
            queue = {"ok": False, "counts": counts, "error": str(exc)[:300]}
        return {
            "schema_version": "frisket.admin_health.v1",
            "ok": bool(db["ok"] and blob["ok"] and queue["ok"]),
            "db": db,
            "blob_store": blob,
            "active_runs": counts["running"],
            "queue": queue,
        }

    @app.get(
        "/api/admin/users",
        name="admin_list_users",
        response_model=AdminUsersResponse,
        responses=http_error_responses(401, 403, 500),
    )
    @app.get(
        "/api/admin/browser/users",
        response_model=AdminBrowserUsersV1,
        responses=http_error_responses(401, 403, 500),
    )
    def admin_browser_users(request: Request) -> dict[str, Any]:
        require_admin(request)
        return membership.browser_users_payload()

    @app.post(
        "/api/admin/browser/users/invite",
        response_model=AdminBrowserInviteResult,
        responses=http_error_responses(400, 401, 403, 404, 409, 422, 500),
    )
    async def admin_browser_invite_user(
        request: Request, body: AdminBrowserInviteRequest
    ) -> dict[str, Any]:
        actor = require_admin(request)
        try:
            membership.require_org(body.org_id)
            invited = invites.create_org_invite(
                email=body.email,
                actor_user_id=int(actor["id"]),
                operator_actor=actor.get("auth") == "operator",
            )
        except AdminMembershipError as exc:
            raise _membership_http_error(exc) from exc
        except (InviteConflict, InviteForbidden, InviteNotFound, ValueError) as exc:
            raise _invite_http_error(exc) from exc
        token = auth.create_magic_link(invited["email"], ttl_minutes=30)
        sent = bool(
            await send_mail(invited["email"], f"{base_url}/auth/callback?token={token}")
        )
        with engine.connect() as cx:
            expires_at = cx.execute(
                sa.select(pending_invites.c.expires_at).where(
                    pending_invites.c.org_id == org_id,
                    pending_invites.c.email == invited["email"],
                )
            ).scalar_one()
        return {
            "ok": True,
            "sent": sent,
            "org_id": org_id,
            "email": invited["email"],
            "role": invited["role"],
            "expires_at": _iso(expires_at),
        }

    @app.patch(
        "/api/admin/browser/users/{user_id}/role",
        response_model=AdminBrowserUpdateRoleResult,
        responses=http_error_responses(400, 401, 403, 404, 409, 422, 500),
    )
    def admin_browser_update_user_role(
        user_id: Annotated[int, PathParam(gt=0)],
        request: Request,
        body: AdminBrowserUpdateRoleRequest,
    ) -> dict[str, Any]:
        actor = require_admin(request)
        try:
            result = membership.update_role(
                actor=actor,
                user_id=user_id,
                role=body.role,
                requested_org_id=body.org_id,
            )
        except AdminMembershipError as exc:
            raise _membership_http_error(exc) from exc
        return {"ok": True, "org_id": org_id, **result}

    @app.delete(
        "/api/admin/browser/users/invites/{email}",
        response_model=AdminBrowserRevokeInviteResult,
        responses=http_error_responses(401, 403, 404, 422, 500),
    )
    def admin_browser_revoke_invite(
        email: str,
        request: Request,
        target_org_id: int = Query(alias="org_id", gt=0),
    ) -> dict[str, Any]:
        actor = require_admin(request)
        query = AdminBrowserTargetOrgQuery.model_validate({"org_id": target_org_id})
        try:
            membership.require_org(query.org_id)
        except AdminMembershipError as exc:
            raise _membership_http_error(exc) from exc
        clean_email = email.lower().strip()
        try:
            revoked = invites.revoke_org_invite(
                email=clean_email,
                actor_user_id=int(actor["id"]),
                operator_actor=actor.get("auth") == "operator",
            )
        except (InviteConflict, InviteForbidden, InviteNotFound, ValueError) as exc:
            raise _invite_http_error(exc) from exc
        if not revoked:
            raise HTTPException(404, "pending invite not found")
        return {
            "ok": True,
            "org_id": org_id,
            "email": clean_email,
            "revoked": True,
        }

    @app.delete(
        "/api/admin/browser/users/{user_ref}",
        response_model=AdminBrowserRemoveUserResult,
        responses=http_error_responses(401, 403, 404, 409, 422, 500),
    )
    def admin_browser_remove_user(
        user_ref: str,
        request: Request,
        target_org_id: int = Query(alias="org_id", gt=0),
    ) -> dict[str, Any]:
        actor = require_admin(request)
        query = AdminBrowserTargetOrgQuery.model_validate({"org_id": target_org_id})
        try:
            result = membership.remove_user(
                actor=actor,
                user_ref=user_ref,
                requested_org_id=query.org_id,
            )
        except AdminMembershipError as exc:
            raise _membership_http_error(exc) from exc
        return {"ok": True, "org_id": org_id, **result}

    @app.get(
        "/api/admin/jobs",
        name="admin_jobs",
        response_model=AdminJobsResponse,
        response_model_exclude_unset=True,
        responses=http_error_responses(401, 403, 500),
    )
    @app.get(
        "/api/admin/browser/jobs",
        response_model=AdminBrowserJobsV1,
        response_model_exclude_unset=True,
        responses=http_error_responses(401, 403, 500),
    )
    def admin_browser_jobs(request: Request) -> dict[str, Any]:
        require_admin(request)
        now = datetime.now(UTC)
        jobs = [
            admin_job_payload(
                job,
                now=now,
            )
            for job in run_queue.list_jobs(limit=200)
        ]
        raw_counts = dict(run_queue.counts())
        counts = {status: int(raw_counts.get(status, 0)) for status in _JOB_STATUSES}
        live_workers = int(
            run_queue.count_live_workers(
                within_seconds=_WORKER_LIVENESS_SECONDS,
                now=now,
            )
        )
        return {
            "schema_version": "frisket.admin_jobs.v1",
            "summary": {
                **counts,
                "stalled": sum(1 for job in jobs if job["stalled"]),
                "live_workers": live_workers,
                "no_live_worker": counts["queued"] > 0 and live_workers == 0,
            },
            "workers": {
                "live": live_workers,
                "liveness_window_seconds": _WORKER_LIVENESS_SECONDS,
            },
            "jobs": jobs,
        }

    @app.post(
        "/api/admin/browser/jobs/{job_id}/cancel",
        response_model=AdminBrowserCancelV1,
        responses={
            **http_error_responses(401, 403, 422, 500),
            404: {"model": AdminBrowserCancelV1},
            409: {"model": AdminBrowserCancelV1},
        },
    )
    def admin_browser_cancel_job(
        job_id: Annotated[int, PathParam(gt=0)], request: Request
    ) -> AdminBrowserCancelV1 | JSONResponse:
        actor = require_admin(request)
        before = run_queue.get(job_id)
        if before is None:
            body = AdminBrowserCancelV1(
                ok=False,
                outcome="conflict",
                job_id=job_id,
                retryable=False,
                detail="job not found",
            )
            return JSONResponse(body.model_dump(mode="json"), status_code=404)
        if run_queue.cancel(job_id):
            with engine.begin() as cx:
                cx.execute(
                    audit_log.insert().values(
                        user_id=actor["id"],
                        org_id=org_id,
                        action="admin_job_cancel_requested",
                        detail=str(job_id),
                    )
                )
            return AdminBrowserCancelV1(
                ok=True,
                outcome="cancelled",
                job_id=job_id,
                retryable=False,
                detail=None,
            )
        after = run_queue.get(job_id)
        status = str(after.status if after is not None else before.status)
        terminal = status in {"done", "failed", "cancelled"}
        body = AdminBrowserCancelV1(
            ok=False,
            outcome="already_terminal" if terminal else "conflict",
            job_id=job_id,
            retryable=not terminal,
            detail=(
                f"job is already {status}"
                if terminal
                else f"job in {status} state cannot be cancelled"
            ),
        )
        return JSONResponse(body.model_dump(mode="json"), status_code=409)

    @app.get(
        "/api/admin/audit",
        name="admin_audit",
        response_model=AdminAuditResponse,
        responses=http_error_responses(401, 403, 422, 500),
    )
    @app.get(
        "/api/admin/browser/audit",
        response_model=AdminBrowserAuditV1,
        responses=http_error_responses(401, 403, 422, 500),
    )
    def admin_browser_audit(
        request: Request,
        target_org_id: int | None = Query(default=None, alias="org_id", gt=0),
        project_id: str | None = None,
        user: str | None = None,
        action: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict[str, Any]:
        require_admin(request)
        query = AdminBrowserAuditQuery.model_validate(
            {
                "org_id": target_org_id,
                "project_id": project_id,
                "user": user,
                "action": action,
                "limit": limit,
            }
        )
        user_filter = (query.user or "").strip().lower()
        action_filter = (query.action or "").strip().lower()
        project_filter = (query.project_id or "").strip()
        audit_predicates = [audit_log.c.org_id == org_id]
        if query.org_id is not None:
            audit_predicates.append(audit_log.c.org_id == query.org_id)
        if action_filter:
            audit_predicates.append(sa.func.lower(audit_log.c.action) == action_filter)
        if user_filter:
            audit_predicates.append(
                sa.or_(
                    sa.cast(audit_log.c.user_id, sa.String) == user_filter,
                    sa.func.lower(users.c.email).contains(user_filter, autoescape=True),
                )
            )
        if project_filter:
            audit_predicates.extend(
                (
                    audit_log.c.action.in_(_PROJECT_AUDIT_ACTIONS),
                    sa.or_(
                        audit_log.c.detail == project_filter,
                        audit_log.c.detail.startswith(
                            f"{project_filter}:", autoescape=True
                        ),
                    ),
                )
            )
        with engine.connect() as cx:
            org_rows = cx.execute(
                sa.select(orgs.c.id, orgs.c.name)
                .where(orgs.c.id == org_id)
                .order_by(orgs.c.id)
            ).all()
            project_rows = cx.execute(
                sa.select(projects.c.org_id, projects.c.slug, projects.c.name)
                .where(projects.c.org_id == org_id)
                .order_by(projects.c.slug)
            ).all()
            audit_rows = cx.execute(
                sa.select(
                    audit_log.c.id,
                    audit_log.c.user_id,
                    audit_log.c.org_id,
                    audit_log.c.action,
                    audit_log.c.detail,
                    audit_log.c.created_at,
                    users.c.email.label("actor_email"),
                )
                .select_from(
                    audit_log.outerjoin(users, users.c.id == audit_log.c.user_id)
                )
                .where(*audit_predicates)
                .order_by(audit_log.c.created_at.desc(), audit_log.c.id.desc())
                .limit(query.limit)
            ).all()
            action_rows = (
                cx.execute(
                    sa.select(audit_log.c.action)
                    .where(audit_log.c.org_id == org_id)
                    .distinct()
                    .order_by(audit_log.c.action)
                )
                .scalars()
                .all()
            )
        org_names = {int(row.id): str(row.name) for row in org_rows}
        project_names = {
            (int(row.org_id), str(row.slug)): str(row.name) for row in project_rows
        }
        events: list[dict[str, Any]] = []
        for row in audit_rows:
            target = _audit_target(str(row.action), row.detail)
            project_id = target["project_id"]
            events.append(
                {
                    "id": f"audit:{row.id}",
                    "source": "audit_log",
                    "category": _audit_category(str(row.action)),
                    "created_at": _iso(row.created_at),
                    "actor_user_id": int(row.user_id)
                    if row.user_id is not None
                    else None,
                    "actor_email": str(row.actor_email) if row.actor_email else None,
                    "org_id": int(row.org_id) if row.org_id is not None else None,
                    "org_name": org_names.get(int(row.org_id)) if row.org_id else None,
                    "project_id": project_id,
                    "project_name": (
                        project_names.get((int(row.org_id), str(project_id)))
                        if row.org_id is not None and project_id is not None
                        else None
                    ),
                    "action": str(row.action),
                    "detail": str(row.detail or ""),
                    "object_type": target["object_type"],
                    "object_id": target["object_id"],
                    "route": f"/p/{project_id}" if project_id is not None else None,
                    "context": {
                        **target["context"],
                        "audit_log_id": int(row.id),
                    },
                }
            )
        return {
            "schema_version": "frisket.admin_audit.v1",
            "events": events,
            "filters": {
                "orgs": [
                    {"id": int(row.id), "name": str(row.name)} for row in org_rows
                ],
                "projects": [
                    {
                        "org_id": int(row.org_id),
                        "project_id": str(row.slug),
                        "name": str(row.name),
                    }
                    for row in project_rows
                ],
                "actions": [str(action) for action in action_rows],
            },
        }

    @app.get(
        "/api/admin/errors",
        name="admin_errors",
        response_model=AdminErrorsResponse,
        response_model_exclude_unset=True,
        responses=http_error_responses(401, 403, 422, 500),
    )
    @app.get(
        "/api/admin/browser/errors",
        response_model=AdminBrowserErrorsV1,
        response_model_exclude_unset=True,
        responses=http_error_responses(401, 403, 422, 500),
    )
    def admin_browser_errors(
        request: Request,
        limit: int = Query(default=50, ge=1, le=500),
    ) -> dict[str, Any]:
        require_admin(request)
        query = AdminBrowserErrorsQuery.model_validate({"limit": limit})
        with engine.connect() as cx:
            rows = cx.execute(
                sa.select(client_errors)
                .where(client_errors.c.org_id == org_id)
                .order_by(client_errors.c.id.desc())
                .limit(query.limit)
            ).all()
        return {
            "schema_version": "frisket.admin_errors.v1",
            "errors": [
                {
                    "id": f"client:{row.id}",
                    "record_id": int(row.id),
                    "kind": "client",
                    "source": str(row.source),
                    "severity": str(row.severity),
                    "name": str(row.name) if row.name is not None else None,
                    "message": str(row.message),
                    "stack": str(row.stack) if row.stack is not None else None,
                    "route": str(row.route) if row.route is not None else None,
                    "org_id": int(row.org_id) if row.org_id is not None else None,
                    "user_id": int(row.user_id) if row.user_id is not None else None,
                    "project_id": (
                        str(row.project_id) if row.project_id is not None else None
                    ),
                    "sheet_id": str(row.sheet_id) if row.sheet_id is not None else None,
                    "run_id": str(row.run_id) if row.run_id is not None else None,
                    "job_id": str(row.job_id) if row.job_id is not None else None,
                    "trace_id": str(row.trace_id) if row.trace_id is not None else None,
                    "context": _json_object(row.context_json) or {},
                    "bundle": _json_object(row.bundle_json),
                    "at": _iso(row.created_at),
                }
                for row in rows
            ],
        }


__all__ = ["register_admin_browser_routes"]
