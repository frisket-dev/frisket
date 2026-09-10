"""Open, single-organization invitation workflows.

Project invite secrets are mail-only: the control plane retains a random
non-secret marker and a SHA-256 digest, never the raw acceptance token.  A
project invitee receives only an explicit project role; accepting one does not
mint an organization membership or any funding/personal-account state.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa

from frisket.team.db import atomic_upsert, locked_transaction
from frisket.team.enforcement import ProjectAccess
from frisket.team.identity_service import SESSION_TTL_DAYS
from frisket.team.local_auth import hash_password
from frisket.team.schema import (
    audit_log,
    memberships,
    pending_invites,
    project_invites,
    project_roles,
    projects,
    sessions,
    users,
)


class InviteForbidden(PermissionError):
    """The caller has no authority for the requested invitation action."""


class InviteNotFound(LookupError):
    """The target project or invitation does not exist."""


class InviteConflict(ValueError):
    """The invitation is expired, consumed, revoked, or otherwise stale."""

    def __init__(self, message: str, *, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


class InviteService:
    PROJECT_ROLES = ("viewer", "reviewer", "editor", "owner")
    PROJECT_INVITE_ROLES = ("viewer", "editor")
    ORG_ROLES = ("member", "owner")

    def __init__(self, engine: sa.Engine, *, org_id: int, access: ProjectAccess):
        self._engine = engine
        self._org_id = org_id
        self._access = access

    @staticmethod
    def _email(email: str) -> str:
        return email.lower().strip()

    @staticmethod
    def _aware(value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value

    @staticmethod
    def _invite_payload(row: Any) -> dict[str, Any]:
        """Safe wire payload: deliberately excludes marker and token digest."""
        return {
            "id": int(row.id),
            "slug": str(row.slug),
            "email": str(row.email),
            "role": str(row.role),
            "created_at": row.created_at,
            "expires_at": row.expires_at,
            "accepted_at": row.accepted_at,
            "revoked_at": row.revoked_at,
        }

    def _require_editor(self, cx: sa.Connection, *, user_id: int, slug: str) -> None:
        exists = cx.execute(
            sa.select(projects.c.id).where(
                projects.c.org_id == self._org_id, projects.c.slug == slug
            )
        ).scalar_one_or_none()
        if exists is None:
            raise InviteNotFound("project not found")
        # The cited proof guards load-bearing delegation to the injected access
        # port on this invite gate (see
        # tests/team/test_team_enforcement_access_proofs.py). Global resolver
        # closure is an accepted coverage narrowing recorded on card PRE-H2.
        if not self._access.can_on_project(self._org_id, slug, user_id, "editor"):
            raise InviteForbidden("project editor required")

    def create_org_invite(
        self,
        *,
        email: str,
        actor_user_id: int,
        role: str | None = None,
        operator_actor: bool = False,
    ) -> dict[str, Any]:
        """Create or re-issue the single pending org invite for ``email``.

        ``role=None`` means "no opinion": a fresh invite defaults to member
        and a re-issue keeps the stored role. A re-issue never silently
        changes an existing invite's role — a conflicting explicit ``role``
        is refused (409) so the caller changes it deliberately through the
        role-change surface. Inviting an already-active member is likewise
        refused rather than parking a shadow invite row.
        """
        clean_email = self._email(email)
        if "@" not in clean_email:
            raise ValueError("valid email required")
        clean_role: str | None = None
        if role is not None:
            clean_role = role.lower().strip() or "member"
            if clean_role not in self.ORG_ROLES:
                raise ValueError("org invite role must be member or owner")
        now = datetime.now(UTC)
        with locked_transaction(
            self._engine, lock_scope=("org-membership", self._org_id)
        ) as cx:
            if not operator_actor:
                # An operator-token actor has no membership row by design (see
                # frisket.team.operator_service): its authority was already
                # proven by the bearer digest, so the caller asserts it
                # explicitly instead of this owner-membership lookup.
                owner = cx.execute(
                    sa.select(memberships.c.user_id).where(
                        memberships.c.org_id == self._org_id,
                        memberships.c.user_id == actor_user_id,
                        memberships.c.role == "owner",
                    )
                ).scalar_one_or_none()
                if owner is None:
                    raise InviteForbidden("organization owner required")
            member_role = cx.execute(
                sa.select(memberships.c.role)
                .select_from(
                    memberships.join(users, users.c.id == memberships.c.user_id)
                )
                .where(
                    memberships.c.org_id == self._org_id,
                    users.c.email == clean_email,
                )
            ).scalar_one_or_none()
            if member_role is not None:
                raise InviteConflict(
                    f"{clean_email} is already an active {member_role}; "
                    "change their role instead of re-inviting them",
                    status_code=409,
                )
            existing_role = cx.execute(
                sa.select(pending_invites.c.role).where(
                    pending_invites.c.org_id == self._org_id,
                    pending_invites.c.email == clean_email,
                )
            ).scalar_one_or_none()
            reissued = existing_role is not None
            if reissued and clean_role is not None and clean_role != str(existing_role):
                raise InviteConflict(
                    f"a pending invite for {clean_email} already grants "
                    f"'{existing_role}'; re-issue without a role to keep it, "
                    "or change it first with the role-change command",
                    status_code=409,
                )
            effective_role = clean_role or (
                str(existing_role) if reissued else "member"
            )
            values = {
                "org_id": self._org_id,
                "role": effective_role,
                "expires_at": now + timedelta(days=7),
            }
            atomic_upsert(
                cx,
                table=pending_invites,
                values={"email": clean_email, **values},
                conflict_columns=("email",),
                update_values=values,
            )
            # Identity redemption removes the pending row and grants membership
            # in its own transaction. Recheck after the upsert so a redemption
            # which commits between the first lookup and this write cannot leave
            # an active member with a new, shadow pending invite.
            member_role = cx.execute(
                sa.select(memberships.c.role)
                .select_from(
                    memberships.join(users, users.c.id == memberships.c.user_id)
                )
                .where(
                    memberships.c.org_id == self._org_id,
                    users.c.email == clean_email,
                )
            ).scalar_one_or_none()
            if member_role is not None:
                raise InviteConflict(
                    f"{clean_email} is already an active {member_role}; "
                    "change their role instead of re-inviting them",
                    status_code=409,
                )
            cx.execute(
                audit_log.insert().values(
                    user_id=actor_user_id,
                    org_id=self._org_id,
                    action="org_invite_set",
                    detail=clean_email,
                )
            )
        return {
            "email": clean_email,
            "org_id": self._org_id,
            "role": effective_role,
            "reissued": reissued,
        }

    def revoke_org_invite(
        self,
        *,
        email: str,
        actor_user_id: int,
        operator_actor: bool = False,
    ) -> bool:
        clean_email = self._email(email)
        with locked_transaction(
            self._engine, lock_scope=("org-membership", self._org_id)
        ) as cx:
            if not operator_actor:
                owner = cx.execute(
                    sa.select(memberships.c.user_id).where(
                        memberships.c.org_id == self._org_id,
                        memberships.c.user_id == actor_user_id,
                        memberships.c.role == "owner",
                    )
                ).scalar_one_or_none()
                if owner is None:
                    raise InviteForbidden("organization owner required")
            result = cx.execute(
                pending_invites.delete().where(
                    pending_invites.c.email == clean_email,
                    pending_invites.c.org_id == self._org_id,
                )
            )
            if result.rowcount:
                cx.execute(
                    audit_log.insert().values(
                        user_id=actor_user_id,
                        org_id=self._org_id,
                        action="org_invite_revoked",
                        detail=clean_email,
                    )
                )
            return bool(result.rowcount)

    def create_project_invite(
        self, *, actor_user_id: int, slug: str, email: str, role: str
    ) -> tuple[dict[str, Any], str]:
        clean_email = self._email(email)
        clean_role = role.lower().strip()
        if "@" not in clean_email:
            raise ValueError("valid email required")
        if clean_role not in self.PROJECT_INVITE_ROLES:
            raise ValueError("project invite role must be viewer or editor")
        raw_token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        # The fresh schema's display field holds an opaque marker, never a
        # bearer secret.
        token_marker = f"stored:{secrets.token_urlsafe(16)}"
        now = datetime.now(UTC)
        with locked_transaction(
            self._engine, lock_scope=("project-invite", self._org_id, slug, clean_email)
        ) as cx:
            self._require_editor(cx, user_id=actor_user_id, slug=slug)
            # Refreshing an address invalidates older mail links deterministically.
            cx.execute(
                project_invites.update()
                .where(
                    project_invites.c.org_id == self._org_id,
                    project_invites.c.slug == slug,
                    project_invites.c.email == clean_email,
                    project_invites.c.accepted_at.is_(None),
                    project_invites.c.revoked_at.is_(None),
                )
                .values(revoked_at=now)
            )
            invite_id = cx.execute(
                project_invites.insert().values(
                    org_id=self._org_id,
                    slug=slug,
                    email=clean_email,
                    role=clean_role,
                    token=token_marker,
                    token_hash=token_hash,
                    invited_by_user_id=actor_user_id,
                    created_at=now,
                    expires_at=now + timedelta(days=14),
                )
            ).inserted_primary_key[0]
            row = cx.execute(
                sa.select(project_invites).where(project_invites.c.id == invite_id)
            ).one()
            cx.execute(
                audit_log.insert().values(
                    user_id=actor_user_id,
                    org_id=self._org_id,
                    action="project_invited",
                    detail=f"{slug}:{clean_email}:{clean_role}",
                )
            )
        return self._invite_payload(row), raw_token

    def list_project_invites(
        self, *, actor_user_id: int, slug: str
    ) -> list[dict[str, Any]]:
        with self._engine.connect() as cx:
            self._require_editor(cx, user_id=actor_user_id, slug=slug)
            rows = cx.execute(
                sa.select(project_invites)
                .where(
                    project_invites.c.org_id == self._org_id,
                    project_invites.c.slug == slug,
                )
                .order_by(project_invites.c.id)
            ).all()
        return [self._invite_payload(row) for row in rows]

    def revoke_project_invite(
        self, *, actor_user_id: int, slug: str, invite_id: int
    ) -> bool:
        now = datetime.now(UTC)
        with locked_transaction(
            self._engine, lock_scope=("project-invite", self._org_id, slug, invite_id)
        ) as cx:
            self._require_editor(cx, user_id=actor_user_id, slug=slug)
            result = cx.execute(
                project_invites.update()
                .where(
                    project_invites.c.id == invite_id,
                    project_invites.c.org_id == self._org_id,
                    project_invites.c.slug == slug,
                    project_invites.c.accepted_at.is_(None),
                    project_invites.c.revoked_at.is_(None),
                )
                .values(revoked_at=now)
            )
            if result.rowcount:
                cx.execute(
                    audit_log.insert().values(
                        user_id=actor_user_id,
                        org_id=self._org_id,
                        action="project_invite_revoked",
                        detail=f"{slug}:{invite_id}",
                    )
                )
            return bool(result.rowcount)

    def project_invite_requires_password(self, token: str) -> bool | None:
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        now = datetime.now(UTC)
        with self._engine.connect() as cx:
            invite = cx.execute(
                sa.select(project_invites.c.email).where(
                    project_invites.c.token_hash == token_hash,
                    project_invites.c.accepted_at.is_(None),
                    project_invites.c.revoked_at.is_(None),
                    project_invites.c.expires_at >= now,
                )
            ).first()
            if invite is None:
                return None
            password_hash = cx.execute(
                sa.select(users.c.password_hash).where(users.c.email == invite.email)
            ).scalar_one_or_none()
            return not password_hash

    def accept_project_invite(
        self, token: str, *, password: str | None = None
    ) -> dict[str, Any] | None:
        now = datetime.now(UTC)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        password_hash = hash_password(password) if password is not None else None
        with locked_transaction(
            self._engine, lock_scope=("project-invite-token", token_hash)
        ) as cx:
            invite = cx.execute(
                sa.select(project_invites).where(
                    project_invites.c.token_hash == token_hash
                )
            ).first()
            if invite is None:
                return None
            if (
                invite.revoked_at is not None
                or invite.accepted_at is not None
                or self._aware(invite.expires_at) < now
            ):
                raise InviteConflict(
                    "project invite is no longer available", status_code=410
                )
            self._require_editor(
                cx, user_id=int(invite.invited_by_user_id), slug=str(invite.slug)
            )
            result = cx.execute(
                project_invites.update()
                .where(
                    project_invites.c.id == invite.id,
                    project_invites.c.accepted_at.is_(None),
                    project_invites.c.revoked_at.is_(None),
                    project_invites.c.expires_at >= now,
                )
                .values(accepted_at=now)
            )
            if result.rowcount != 1:
                raise InviteConflict(
                    "project invite is no longer available", status_code=410
                )
            user = cx.execute(
                sa.select(users).where(users.c.email == str(invite.email))
            ).first()
            if user is None:
                if password_hash is None:
                    raise InviteConflict("create a password to accept this invite")
                user_id = cx.execute(
                    users.insert().values(
                        email=str(invite.email), password_hash=password_hash
                    )
                ).inserted_primary_key[0]
                user = cx.execute(sa.select(users).where(users.c.id == user_id)).one()
            elif not user.password_hash:
                if password_hash is None:
                    raise InviteConflict("create a password to accept this invite")
                cx.execute(
                    users.update()
                    .where(users.c.id == user.id)
                    .values(password_hash=password_hash)
                )
            existing = cx.execute(
                sa.select(project_roles.c.role).where(
                    project_roles.c.org_id == self._org_id,
                    project_roles.c.slug == str(invite.slug),
                    project_roles.c.user_id == user.id,
                )
            ).scalar_one_or_none()
            if existing is None:
                cx.execute(
                    project_roles.insert().values(
                        org_id=self._org_id,
                        slug=str(invite.slug),
                        user_id=user.id,
                        role=str(invite.role),
                    )
                )
            elif self.PROJECT_ROLES.index(str(invite.role)) > self.PROJECT_ROLES.index(
                str(existing)
            ):
                cx.execute(
                    project_roles.update()
                    .where(
                        project_roles.c.org_id == self._org_id,
                        project_roles.c.slug == str(invite.slug),
                        project_roles.c.user_id == user.id,
                    )
                    .values(role=str(invite.role))
                )
            cx.execute(
                audit_log.insert().values(
                    user_id=user.id,
                    org_id=self._org_id,
                    action="project_invite_accepted",
                    detail=f"{invite.slug}:{invite.email}:{invite.role}",
                )
            )
            # Keep the one-time invite claim and the usable login inseparable:
            # a failed session write must roll back the accepted marker, role,
            # user creation, and audit row so the same mail token remains
            # retryable.  Session tokens are bearer values, like the normal
            # IdentityStore session flow, and are deliberately never returned
            # in the JSON payload.
            session = secrets.token_urlsafe(32)
            cx.execute(
                sessions.insert().values(
                    token=session,
                    user_id=user.id,
                    created_at=now,
                    expires_at=now + timedelta(days=SESSION_TTL_DAYS),
                )
            )
            return {
                "invite": self._invite_payload(invite),
                "session": session,
                "user": {
                    "id": int(user.id),
                    "email": str(user.email),
                    "name": user.name,
                    # Project-only access resolves to its one live project
                    # organization without creating an org membership.
                    "org_id": self._org_id,
                    "new": False,
                },
            }
