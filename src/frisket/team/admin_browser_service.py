"""Transport-neutral public administration membership authority."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa

from frisket.team.db import locked_transaction
from frisket.team.schema import (
    api_tokens,
    audit_log,
    memberships,
    orgs,
    pending_invites,
    project_roles,
    sessions,
    users,
)


class AdminMembershipError(ValueError):
    """Base class for a refused administration membership transition."""


class AdminMembershipInvalid(AdminMembershipError):
    """The requested transition is not a supported membership operation."""


class AdminMembershipForbidden(AdminMembershipError):
    """The actor no longer has organization-owner authority."""


class AdminMembershipNotFound(AdminMembershipError):
    """The target organization, member, or invitation does not exist."""


class AdminMembershipConflict(AdminMembershipError):
    """The transition would violate a membership invariant."""


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    text = value.isoformat() if hasattr(value, "isoformat") else str(value)
    return text.replace("+00:00", "Z")


class AdminMembershipService:
    """One membership mutation authority shared by old and browser wires."""

    def __init__(self, engine: sa.Engine, *, org_id: int):
        self._engine = engine
        self.org_id = org_id

    def require_org(self, requested_org_id: int) -> None:
        if requested_org_id != self.org_id:
            raise AdminMembershipNotFound("organization not found")

    def _audit(
        self, cx: sa.Connection, *, actor: dict[str, Any], action: str, target: str
    ) -> None:
        cx.execute(
            audit_log.insert().values(
                user_id=actor["id"],
                org_id=self.org_id,
                action=action,
                detail=target,
            )
        )

    def _require_owner_in_transaction(
        self, cx: sa.Connection, actor: dict[str, Any]
    ) -> None:
        if actor.get("auth") == "operator":
            return
        role = cx.execute(
            sa.select(memberships.c.role).where(
                memberships.c.org_id == self.org_id,
                memberships.c.user_id == int(actor["id"]),
            )
        ).scalar_one_or_none()
        if role != "owner":
            raise AdminMembershipForbidden("organization owner required")

    def _inventory(self) -> tuple[Any, list[Any], list[Any]]:
        now = datetime.now(UTC)
        with self._engine.connect() as cx:
            org = cx.execute(
                sa.select(orgs.c.id, orgs.c.name).where(orgs.c.id == self.org_id)
            ).one()
            rows = cx.execute(
                sa.select(
                    users.c.id,
                    users.c.email,
                    users.c.name,
                    users.c.created_at,
                    memberships.c.role,
                )
                .select_from(
                    users.join(memberships, memberships.c.user_id == users.c.id)
                )
                .where(memberships.c.org_id == self.org_id)
                .order_by(users.c.email)
            ).all()
            invited = cx.execute(
                sa.select(
                    pending_invites.c.email,
                    pending_invites.c.org_id,
                    pending_invites.c.role,
                    pending_invites.c.expires_at,
                )
                .where(
                    pending_invites.c.org_id == self.org_id,
                    pending_invites.c.expires_at >= now,
                )
                .order_by(pending_invites.c.email)
            ).all()
        return org, rows, invited

    def browser_users_payload(self) -> dict[str, Any]:
        org, rows, invited = self._inventory()
        members = {str(row.email) for row in rows}
        return {
            "schema_version": "frisket.admin_users.v1",
            "orgs": [
                {
                    "id": int(org.id),
                    "name": str(org.name),
                    "suspended": False,
                    "users": [
                        {
                            "user_id": int(row.id),
                            "email": str(row.email),
                            "name": row.name,
                            "role": str(row.role),
                            "created_at": _iso(row.created_at),
                        }
                        for row in rows
                    ],
                    "pending_invites": [
                        {
                            "email": str(row.email),
                            "org_id": int(row.org_id),
                            "role": str(row.role),
                            "expires_at": _iso(row.expires_at),
                        }
                        for row in invited
                        if str(row.email) not in members
                    ],
                }
            ],
            "capabilities": {
                "assignable_roles": ["owner", "member"],
                "invite_ttl_days": 7,
                "magic_link_ttl_minutes": 30,
            },
        }

    def update_role(
        self,
        *,
        actor: dict[str, Any],
        user_id: int,
        role: str,
        requested_org_id: int | None = None,
    ) -> dict[str, Any]:
        if requested_org_id is not None:
            self.require_org(requested_org_id)
        if role not in {"owner", "member"}:
            raise AdminMembershipInvalid("role must be owner or member")
        with locked_transaction(
            self._engine, lock_scope=("org-membership", self.org_id)
        ) as cx:
            self._require_owner_in_transaction(cx, actor)
            current = cx.execute(
                sa.select(memberships.c.role).where(
                    memberships.c.org_id == self.org_id,
                    memberships.c.user_id == user_id,
                )
            ).scalar_one_or_none()
            if current is None:
                raise AdminMembershipNotFound("user is not a member")
            owners_count = cx.execute(
                sa.select(sa.func.count())
                .select_from(memberships)
                .where(
                    memberships.c.org_id == self.org_id,
                    memberships.c.role == "owner",
                )
            ).scalar_one()
            if current == "owner" and role != "owner" and owners_count <= 1:
                raise AdminMembershipConflict("cannot demote the last owner")
            cx.execute(
                memberships.update()
                .where(
                    memberships.c.org_id == self.org_id,
                    memberships.c.user_id == user_id,
                )
                .values(role=role)
            )
            self._audit(
                cx,
                actor=actor,
                action="org_member_role_set",
                target=f"{user_id}:{role}",
            )
        return {
            "user_id": user_id,
            "role": role,
            "self": int(actor["id"]) == user_id,
        }

    def remove_user(
        self,
        *,
        actor: dict[str, Any],
        user_ref: str,
        requested_org_id: int | None = None,
    ) -> dict[str, Any]:
        if requested_org_id is not None:
            self.require_org(requested_org_id)
        clean_ref = user_ref.lower().strip()
        with locked_transaction(
            self._engine, lock_scope=("org-membership", self.org_id)
        ) as cx:
            self._require_owner_in_transaction(cx, actor)
            invite_revoked = False
            email: str | None = clean_ref if "@" in clean_ref else None
            if email is not None:
                user_id = cx.execute(
                    sa.select(users.c.id).where(users.c.email == email)
                ).scalar_one_or_none()
                revoked = cx.execute(
                    pending_invites.delete().where(
                        pending_invites.c.org_id == self.org_id,
                        pending_invites.c.email == email,
                    )
                )
                if revoked.rowcount:
                    invite_revoked = True
                    self._audit(
                        cx,
                        actor=actor,
                        action="org_invite_revoked",
                        target=email,
                    )
            else:
                try:
                    user_id = int(clean_ref)
                except ValueError:
                    raise AdminMembershipNotFound("user is not a member") from None
                if user_id <= 0:
                    raise AdminMembershipNotFound("user is not a member")
            role = (
                None
                if user_id is None
                else cx.execute(
                    sa.select(memberships.c.role).where(
                        memberships.c.org_id == self.org_id,
                        memberships.c.user_id == user_id,
                    )
                ).scalar_one_or_none()
            )
            if role is None:
                if invite_revoked:
                    return {
                        "user_id": None,
                        "email": email,
                        "membership_removed": False,
                        "invite_revoked": True,
                        "self": False,
                    }
                raise AdminMembershipNotFound("user is not a member")
            owners_count = cx.execute(
                sa.select(sa.func.count())
                .select_from(memberships)
                .where(
                    memberships.c.org_id == self.org_id,
                    memberships.c.role == "owner",
                )
            ).scalar_one()
            if role == "owner" and owners_count <= 1:
                raise AdminMembershipConflict("cannot remove the last owner")
            cx.execute(
                project_roles.delete().where(
                    project_roles.c.org_id == self.org_id,
                    project_roles.c.user_id == user_id,
                )
            )
            cx.execute(
                api_tokens.update()
                .where(
                    api_tokens.c.org_id == self.org_id,
                    api_tokens.c.user_id == user_id,
                    api_tokens.c.revoked_at.is_(None),
                )
                .values(revoked_at=datetime.now(UTC))
            )
            cx.execute(
                memberships.delete().where(
                    memberships.c.org_id == self.org_id,
                    memberships.c.user_id == user_id,
                )
            )
            cx.execute(sessions.delete().where(sessions.c.user_id == user_id))
            self._audit(
                cx,
                actor=actor,
                action="org_member_removed",
                target=str(user_id),
            )
        return {
            "user_id": int(user_id),
            "email": email,
            "membership_removed": True,
            "invite_revoked": invite_revoked,
            "self": int(actor["id"]) == user_id,
        }


__all__ = [
    "AdminMembershipConflict",
    "AdminMembershipError",
    "AdminMembershipForbidden",
    "AdminMembershipInvalid",
    "AdminMembershipNotFound",
    "AdminMembershipService",
]
