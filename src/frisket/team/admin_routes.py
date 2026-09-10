"""HTTP registrar for the operator-facing remote administration API.

These routes are the server side of the local `frisket remote` / `frisket
users` / `frisket secrets` CLI. They are declared to the outer policy layer
as ``admin`` routes: the composition's ``require_user(admin=True)`` accepts
either a browser owner session or an operator bearer token
(frisket.team.operator_service); the ping and rotate routes additionally
require the operator channel itself, because pairing and rotation only make
sense for the credential actually presented.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import sqlalchemy as sa

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from frisket.ops import media_proxy as media_proxy_config
from frisket.server.provider_config import ENV_VAR
from frisket.team.control_plane import MODEL_KEY_PROVIDERS
from frisket.team.db import atomic_upsert, locked_transaction
from frisket.team.identity_service import IdentityAuthService
from frisket.team.invite_service import (
    InviteConflict,
    InviteForbidden,
    InviteNotFound,
    InviteService,
)
from frisket.team.local_auth import SetupError, create_password_reset
from frisket.team.operator_service import OperatorTokenError, OperatorTokenService
from frisket.team.readiness import runtime_identity
from frisket.team.schema import (
    audit_log,
    memberships,
    org_keys,
    orgs,
    pending_invites,
    users,
)
from frisket.team.security.secrets import key_hint

# Invited accounts join by opening the printed link; there is no email
# transport behind this surface, so the link lives as long as the invite
# window itself (InviteService.create_org_invite's 7 days).
INVITE_LINK_TTL_MINUTES = 7 * 24 * 60


class AdminUserBody(BaseModel):
    email: str = Field(default="", max_length=320)
    # None = no opinion: fresh invites default to member, re-issues keep the
    # stored role (a conflicting explicit role is a 409, never a silent reset).
    role: str | None = None


class RoleBody(BaseModel):
    role: str = ""


class SecretBody(BaseModel):
    key: str = ""


class MediaProxyBody(BaseModel):
    url: str = Field(default="", max_length=512)


def _invite_error(exc: Exception) -> HTTPException:
    if isinstance(exc, InviteNotFound):
        return HTTPException(404, str(exc))
    if isinstance(exc, InviteForbidden):
        return HTTPException(403, str(exc))
    if isinstance(exc, InviteConflict):
        return HTTPException(exc.status_code, str(exc))
    return HTTPException(400, str(exc))


def register_admin_api_routes(
    app: FastAPI,
    *,
    engine: sa.Engine,
    org_id: int,
    invites: InviteService,
    auth: IdentityAuthService,
    operator_service: OperatorTokenService,
    secret_box: Any,
    require_user: Callable[..., dict[str, Any]],
    base_url: str,
    data_dir: Path,
) -> None:
    def require_admin(request: Request) -> dict[str, Any]:
        return require_user(request, browser=True, admin=True)

    def require_operator(request: Request) -> dict[str, Any]:
        actor = require_admin(request)
        if actor.get("auth") != "operator":
            raise HTTPException(403, "operator token required")
        return actor

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

    def require_owner_in_transaction(cx: sa.Connection, actor: dict[str, Any]) -> None:
        if actor.get("auth") == "operator":
            return
        role = cx.execute(
            sa.select(memberships.c.role).where(
                memberships.c.org_id == org_id,
                memberships.c.user_id == int(actor["id"]),
            )
        ).scalar_one_or_none()
        if role != "owner":
            raise HTTPException(403, "organization owner required")

    @app.get("/api/admin/ping")
    def admin_ping(request: Request) -> dict[str, Any]:
        actor = require_operator(request)
        with engine.connect() as cx:
            row = cx.execute(
                sa.select(orgs.c.name, orgs.c.display_name).where(orgs.c.id == org_id)
            ).one()
        return {
            "org": row.display_name or row.name,
            "server_version": runtime_identity()["package_version"],
            "token_label": actor.get("operator_label"),
        }

    @app.post("/api/admin/token/rotate")
    def admin_rotate_operator_token(request: Request) -> dict[str, Any]:
        actor = require_operator(request)
        try:
            raw, label = operator_service.rotate(
                token_id=int(actor["operator_token_id"])
            )
        except OperatorTokenError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"token": raw, "label": label}

    @app.post("/api/admin/users")
    def admin_create_user(request: Request, body: AdminUserBody) -> dict[str, Any]:
        actor = require_admin(request)
        try:
            invited = invites.create_org_invite(
                email=body.email,
                actor_user_id=int(actor["id"]),
                role=body.role,
                operator_actor=actor.get("auth") == "operator",
            )
        except (InviteConflict, InviteForbidden, InviteNotFound, ValueError) as exc:
            raise _invite_error(exc) from exc
        token = auth.create_magic_link(
            invited["email"], ttl_minutes=INVITE_LINK_TTL_MINUTES
        )
        return {
            "email": invited["email"],
            "role": invited["role"],
            "reissued": invited["reissued"],
            "invite_link": f"{base_url}/auth/callback?token={token}",
        }

    @app.put("/api/admin/users/{email}/role")
    def admin_set_user_role_by_email(
        email: str, request: Request, body: RoleBody
    ) -> dict[str, Any]:
        actor = require_admin(request)
        clean_email = email.lower().strip()
        clean_role = body.role.lower().strip()
        if clean_role not in InviteService.ORG_ROLES:
            raise HTTPException(400, "role must be member or owner")
        with locked_transaction(engine, lock_scope=("org-membership", org_id)) as cx:
            require_owner_in_transaction(cx, actor)
            member = cx.execute(
                sa.select(users.c.id, memberships.c.role)
                .join(memberships, memberships.c.user_id == users.c.id)
                .where(
                    memberships.c.org_id == org_id,
                    users.c.email == clean_email,
                )
            ).first()
            if member is not None:
                owners_count = cx.execute(
                    sa.select(sa.func.count())
                    .select_from(memberships)
                    .where(
                        memberships.c.org_id == org_id,
                        memberships.c.role == "owner",
                    )
                ).scalar_one()
                if (
                    member.role == "owner"
                    and clean_role != "owner"
                    and owners_count <= 1
                ):
                    raise HTTPException(409, "cannot demote the last owner")
                cx.execute(
                    memberships.update()
                    .where(
                        memberships.c.org_id == org_id,
                        memberships.c.user_id == member.id,
                    )
                    .values(role=clean_role)
                )
                audit(
                    cx,
                    actor=actor,
                    action="org_member_role_set",
                    target=f"{member.id}:{clean_role}",
                )
                return {"email": clean_email, "role": clean_role, "status": "active"}
            invited = cx.execute(
                pending_invites.update()
                .where(
                    pending_invites.c.org_id == org_id,
                    pending_invites.c.email == clean_email,
                )
                .values(role=clean_role)
            )
            if not invited.rowcount:
                raise HTTPException(404, "no member or pending invite for that email")
            audit(
                cx,
                actor=actor,
                action="org_invite_set",
                target=f"{clean_email}:{clean_role}",
            )
            return {"email": clean_email, "role": clean_role, "status": "invited"}

    @app.post("/api/admin/users/{email}/reset")
    def admin_reset_user_password(email: str, request: Request) -> dict[str, Any]:
        actor = require_admin(request)
        try:
            raw = create_password_reset(
                engine,
                org_id=org_id,
                email=email,
                actor_user_id=int(actor["id"]),
            )
        except SetupError as exc:
            raise HTTPException(404, str(exc)) from exc
        return {
            "email": email.lower().strip(),
            "reset_link": f"{base_url}/auth/reset/{raw}",
        }

    @app.get("/api/admin/secrets")
    def admin_list_secrets(request: Request) -> dict[str, Any]:
        require_admin(request)
        with engine.connect() as cx:
            rows = cx.execute(
                sa.select(org_keys.c.provider, org_keys.c.hint).where(
                    org_keys.c.org_id == org_id
                )
            ).all()
        stored = {str(row.provider): str(row.hint) for row in rows}
        return {
            "providers": [
                {
                    "provider": provider,
                    "configured": provider in stored,
                    "hint": stored.get(provider),
                    "source": "stored" if provider in stored else None,
                    "env_var": ENV_VAR.get(provider),
                    "env_present": bool(os.environ.get(ENV_VAR.get(provider, ""))),
                }
                for provider in MODEL_KEY_PROVIDERS
            ]
        }

    @app.put("/api/admin/secrets/{provider}")
    def admin_set_secret(
        provider: str, request: Request, body: SecretBody
    ) -> dict[str, Any]:
        actor = require_admin(request)
        clean_provider = provider.lower().strip()
        if clean_provider not in MODEL_KEY_PROVIDERS:
            raise HTTPException(400, "unsupported provider")
        value = body.key
        if not value.strip():
            raise HTTPException(400, "provider key must not be empty")
        with locked_transaction(
            engine, lock_scope=("org-key", org_id, clean_provider)
        ) as cx:
            values = {"encrypted": secret_box.encrypt(value), "hint": key_hint(value)}
            atomic_upsert(
                cx,
                table=org_keys,
                values={"org_id": org_id, "provider": clean_provider, **values},
                conflict_columns=("org_id", "provider"),
                update_values=values,
            )
            audit(cx, actor=actor, action="org_key_set", target=clean_provider)
        return {"provider": clean_provider, "hint": key_hint(value)}

    def _media_proxy_payload() -> dict[str, Any]:
        resolved = media_proxy_config.read_media_proxy(data_dir)
        return {
            "configured": resolved.url is not None,
            "url": resolved.url,
            "source": resolved.source,
            "env_invalid": resolved.env_invalid,
            # Containerized servers cannot reach a host-loopback tunnel; the
            # CLI binds the tunnel at this gateway instead when present.
            "container_gateway": media_proxy_config.container_gateway(),
        }

    @app.get("/api/admin/media-proxy")
    def admin_get_media_proxy(request: Request) -> dict[str, Any]:
        require_admin(request)
        return _media_proxy_payload()

    @app.put("/api/admin/media-proxy")
    def admin_set_media_proxy(request: Request, body: MediaProxyBody) -> dict[str, Any]:
        actor = require_admin(request)
        try:
            validated = media_proxy_config.write_media_proxy(data_dir, body.url)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        with locked_transaction(engine, lock_scope=("media-proxy", org_id)) as cx:
            audit(cx, actor=actor, action="media_proxy_set", target=validated.url)
        return {**_media_proxy_payload(), "warnings": list(validated.warnings)}

    @app.delete("/api/admin/media-proxy")
    def admin_clear_media_proxy(request: Request) -> dict[str, Any]:
        actor = require_admin(request)
        had_admin_value = (
            media_proxy_config.read_media_proxy(data_dir).source == "admin"
        )
        media_proxy_config.write_media_proxy(data_dir, None)
        if had_admin_value:
            with locked_transaction(engine, lock_scope=("media-proxy", org_id)) as cx:
                audit(cx, actor=actor, action="media_proxy_cleared", target="")
        return {**_media_proxy_payload(), "cleared": had_admin_value}

    @app.post("/api/admin/media-proxy/check")
    def admin_check_media_proxy(request: Request) -> dict[str, Any]:
        require_admin(request)
        resolved = media_proxy_config.read_media_proxy(data_dir)
        if resolved.url is None:
            return {"configured": False, "ok": False, "error": "no media proxy is set"}
        probe = media_proxy_config.probe_media_proxy(resolved.url)
        return {"configured": True, "source": resolved.source, **probe.as_dict()}

    @app.delete("/api/admin/secrets/{provider}")
    def admin_delete_secret(provider: str, request: Request) -> dict[str, Any]:
        actor = require_admin(request)
        clean_provider = provider.lower().strip()
        if clean_provider not in MODEL_KEY_PROVIDERS:
            raise HTTPException(400, "unsupported provider")
        with locked_transaction(
            engine, lock_scope=("org-key", org_id, clean_provider)
        ) as cx:
            result = cx.execute(
                org_keys.delete().where(
                    org_keys.c.org_id == org_id,
                    org_keys.c.provider == clean_provider,
                )
            )
            if result.rowcount:
                audit(cx, actor=actor, action="org_key_deleted", target=clean_provider)
        return {"deleted": bool(result.rowcount)}


__all__ = ["register_admin_api_routes"]
