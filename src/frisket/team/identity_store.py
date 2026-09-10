"""Open identity persistence: users, orgs, sessions, magic links, invites.

This is the OPEN half of what used to be a single hosted auth store. It knows
how to sign someone in — issue and redeem a magic link, ensure the user and
their first org exist, mint a session, resolve a session back to a user — and
nothing else. It never reads or writes commerce state; a sign-in here completes
against a database that has no commerce tables at all
(the public package boundary).

Editions extend it through two seams:

* `_create_org` — an external edition overrides it to attach its own commerce
  companion row to the new org (its `AuthIdentityStore` subclass).
* `user_payload` — an external edition overrides it to add its own fields to
  the identity payload.

Both seams are ordinary subclass overrides, so the open sign-in path calls them
polymorphically without knowing an edition exists.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
import secrets
from typing import Any

import sqlalchemy as sa

from frisket.team.auth_challenges import AuthChallengeStore
from frisket.team.schema import (
    audit_log,
    memberships,
    oidc_identities,
    orgs,
    pending_invites,
    project_roles,
    projects,
    sessions,
    users,
)


class IdentityStore:
    def __init__(
        self,
        cx: sa.Connection,
        *,
        provision_org_id: int | None = None,
    ):
        self.cx = cx
        self._provision_org_id = provision_org_id

    def _create_org(self, name: str) -> int:
        """Create an identity org. Editions override to attach their own state."""
        org_id = self.cx.execute(orgs.insert().values(name=name)).inserted_primary_key[
            0
        ]
        return int(org_id)

    @staticmethod
    def _email(email: str) -> str:
        return email.lower().strip()

    @staticmethod
    def _aware(value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value

    def create_magic_link(
        self,
        *,
        email: str,
        expires_at: datetime,
        now: datetime,
        max_active: int | None = None,
    ) -> str:
        return AuthChallengeStore(self.cx).issue_magic_link(
            email=self._email(email),
            expires_at=expires_at,
            now=now,
            max_active=max_active,
        )

    def redeem_magic_link(self, token: str, *, now: datetime) -> str | None:
        return AuthChallengeStore(self.cx).consume_magic_link(token, now=now)

    def email_for_token(self, token: str) -> str | None:
        """Look up a magic link's email regardless of expiry/used state.

        Read-only (does not consume the token) — used only to classify an
        already-failed redemption (expired or already used) so the caller
        can tell an invited user apart from a plain expired sign-in link
        (onboard-instance-identity-v1).
        """
        return AuthChallengeStore(self.cx).magic_link_email(token)

    def pending_magic_link_email(self, token: str, *, now: datetime) -> str | None:
        return AuthChallengeStore(self.cx).pending_magic_link_email(token, now=now)

    def email_is_admissible(self, email: str, *, now: datetime) -> bool:
        if (
            self.user_for_local_login(email) is not None
            or self._provision_org_id is None
        ):
            return True
        return self.has_valid_pending_invite(email, now=now)

    def has_valid_pending_invite(self, email: str, *, now: datetime) -> bool:
        invite = self.cx.execute(
            sa.select(pending_invites.c.expires_at).where(
                pending_invites.c.email == self._email(email)
            )
        ).first()
        if invite is None:
            return False
        return self._aware(invite.expires_at) >= now

    def has_project_access_for_email(self, email: str) -> bool:
        rows = self.cx.execute(
            sa.select(project_roles.c.org_id, project_roles.c.slug)
            .select_from(
                project_roles.join(users, users.c.id == project_roles.c.user_id)
            )
            .where(users.c.email == self._email(email))
        ).fetchall()
        return any(
            self.project_exists(org_id=row.org_id, slug=row.slug) for row in rows
        )

    def project_exists(self, *, org_id: int, slug: str) -> bool:
        return (
            self.cx.execute(
                sa.select(projects.c.id).where(
                    projects.c.org_id == org_id,
                    projects.c.slug == slug,
                )
            ).scalar_one_or_none()
            is not None
        )

    def ensure_user_for_login(self, email: str) -> dict[str, Any]:
        clean_email = self._email(email)
        row = self.cx.execute(
            sa.select(users).where(users.c.email == clean_email)
        ).first()
        now = datetime.now(UTC)
        # Claim the live invite in the same statement that removes it.  A
        # revoke racing a redemption now has one database winner, and either a
        # fresh or a previously removed identity receives the granted
        # membership only after that winner is known.
        invite = self.cx.execute(
            pending_invites.delete()
            .where(
                pending_invites.c.email == clean_email,
                pending_invites.c.expires_at >= now,
            )
            .returning(pending_invites.c.org_id, pending_invites.c.role)
        ).first()
        if invite is not None:
            if row is None:
                user_id = self.cx.execute(
                    users.insert().values(email=clean_email)
                ).inserted_primary_key[0]
                row = self.cx.execute(
                    sa.select(users).where(users.c.id == user_id)
                ).first()
                new = True
            else:
                user_id = int(row.id)
                new = False
            org_id = int(invite.org_id)
            self.cx.execute(
                memberships.insert().values(
                    user_id=user_id,
                    org_id=org_id,
                    role=str(invite.role or "member"),
                )
            )
            self.cx.execute(
                users.update()
                .where(users.c.id == user_id)
                .values(default_org_id=org_id)
            )
            self.cx.execute(
                audit_log.insert().values(
                    user_id=user_id,
                    org_id=org_id,
                    action="joined_via_invite",
                    detail=clean_email,
                )
            )
            row = self.cx.execute(sa.select(users).where(users.c.id == user_id)).first()
            return self.user_payload(row, new=new, org_id=org_id)
        if row is None:
            user_id = self.cx.execute(
                users.insert().values(email=clean_email)
            ).inserted_primary_key[0]
            if self._provision_org_id is not None:
                # A server does not expose public account registration. New
                # identities must arrive through an invite; first ownership is
                # created only by the claim operation.
                self.cx.execute(users.delete().where(users.c.id == user_id))
                raise PermissionError("account admission requires an invite")
            else:
                org_id = self._create_org(clean_email.split("@")[0])
                self.cx.execute(
                    memberships.insert().values(
                        user_id=user_id,
                        org_id=org_id,
                        role="owner",
                    )
                )
                self.cx.execute(
                    users.update()
                    .where(users.c.id == user_id)
                    .values(default_org_id=org_id)
                )
                self.cx.execute(
                    audit_log.insert().values(
                        user_id=user_id,
                        org_id=org_id,
                        action="signup",
                        detail=clean_email,
                    )
                )
            row = self.cx.execute(sa.select(users).where(users.c.id == user_id)).first()
            return self.user_payload(row, new=True, org_id=org_id)
        return self.user_payload(row, new=False)

    def create_session(
        self,
        *,
        user_id: int,
        created_at: datetime,
        expires_at: datetime,
    ) -> str:
        token = secrets.token_urlsafe(32)
        self.cx.execute(
            sessions.insert().values(
                token=token,
                user_id=user_id,
                created_at=created_at,
                expires_at=expires_at,
            )
        )
        return token

    def revoke_sessions(self, *, user_id: int) -> None:
        self.cx.execute(sessions.delete().where(sessions.c.user_id == user_id))

    def revoke_session(self, token: str) -> None:
        self.cx.execute(sessions.delete().where(sessions.c.token == token))

    def set_password_hash(self, *, user_id: int, password_hash: str) -> None:
        self.cx.execute(
            users.update()
            .where(users.c.id == user_id)
            .values(password_hash=password_hash)
        )

    def user_for_local_login(self, email: str) -> Any | None:
        return self.cx.execute(
            sa.select(users).where(users.c.email == self._email(email))
        ).first()

    def user_for_oidc(self, *, issuer: str, subject: str) -> dict[str, Any] | None:
        user_id = self.cx.execute(
            sa.select(oidc_identities.c.user_id).where(
                oidc_identities.c.issuer == issuer,
                oidc_identities.c.subject == subject,
            )
        ).scalar_one_or_none()
        return None if user_id is None else self.user_by_id(int(user_id))

    def bind_oidc(self, *, issuer: str, subject: str, user_id: int, email: str) -> None:
        self.cx.execute(
            oidc_identities.insert().values(
                issuer=issuer,
                subject=subject,
                user_id=user_id,
                email_at_link=self._email(email),
            )
        )

    def oidc_subject_for_user(self, *, issuer: str, user_id: int) -> str | None:
        subject = self.cx.execute(
            sa.select(oidc_identities.c.subject).where(
                oidc_identities.c.issuer == issuer,
                oidc_identities.c.user_id == user_id,
            )
        ).scalar_one_or_none()
        return None if subject is None else str(subject)

    def session_user(self, token: str, *, now: datetime) -> dict[str, Any] | None:
        row = self.cx.execute(
            sa.select(
                sessions.c.user_id,
                sessions.c.expires_at,
                users.c.id,
                users.c.email,
                users.c.name,
                users.c.cost_preapproval_usd,
            )
            .select_from(sessions.join(users, users.c.id == sessions.c.user_id))
            .where(sessions.c.token == token)
        ).first()
        if row is None:
            return None
        expires = row.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        if expires < now:
            return None
        payload = self.user_payload(row, new=False)
        return payload if payload["org_id"] is not None else None

    def user_payload(
        self,
        row: Any,
        *,
        new: bool,
        org_id: int | None = None,
    ) -> dict[str, Any]:
        """The open identity payload. Editions override to add their own fields."""
        effective_org_id = org_id
        if effective_org_id is None:
            effective_org_id = self._default_org_id_for_user(int(row.id))
        return {
            "id": row.id,
            "email": row.email,
            "name": row.name,
            "cost_preapproval_usd": (
                None
                if row.cost_preapproval_usd is None
                else str(row.cost_preapproval_usd)
            ),
            "org_id": effective_org_id,
            "new": new,
        }

    def update_user_name(self, *, user_id: int, name: str | None) -> dict[str, Any]:
        self.cx.execute(users.update().where(users.c.id == user_id).values(name=name))
        row = self.cx.execute(sa.select(users).where(users.c.id == user_id)).first()
        return self.user_payload(row, new=False)

    def update_cost_preapproval(
        self, *, user_id: int, threshold_usd: Decimal
    ) -> dict[str, Any]:
        self.cx.execute(
            users.update()
            .where(users.c.id == user_id)
            .values(cost_preapproval_usd=threshold_usd)
        )
        row = self.cx.execute(sa.select(users).where(users.c.id == user_id)).first()
        return self.user_payload(row, new=False)

    def user_by_id(self, user_id: int) -> dict[str, Any] | None:
        row = self.cx.execute(sa.select(users).where(users.c.id == user_id)).first()
        return None if row is None else self.user_payload(row, new=False)

    def identity_for_org(self, org_id: int) -> dict[str, Any] | None:
        """The org's own branding, for `/api/me`."""
        row = self.cx.execute(
            sa.select(
                orgs.c.display_name,
                orgs.c.welcome_message,
                orgs.c.support_contact,
            ).where(orgs.c.id == org_id)
        ).first()
        if row is None:
            return None
        return {
            "display_name": row.display_name,
            "welcome_message": row.welcome_message,
            "support_contact": row.support_contact,
        }

    def first_instance_identity(self) -> dict[str, Any]:
        """Identity for the first configured org (lowest id).

        Used to brand the pre-auth sign-in surface, where there is no signed
        -in user's org to read yet. A third-party operator's single org is,
        by definition, the first one — our own hosted org is not special-
        cased, it is simply whichever org happens to be first.
        """
        row = self.cx.execute(
            sa.select(
                orgs.c.display_name,
                orgs.c.welcome_message,
                orgs.c.support_contact,
            )
            .order_by(orgs.c.id)
            .limit(1)
        ).first()
        if row is None:
            return {
                "display_name": None,
                "welcome_message": None,
                "support_contact": None,
            }
        return {
            "display_name": row.display_name,
            "welcome_message": row.welcome_message,
            "support_contact": row.support_contact,
        }

    def _default_org_id_for_user(self, user_id: int) -> int | None:
        default_org_id = self.cx.execute(
            sa.select(users.c.default_org_id).where(users.c.id == user_id)
        ).scalar_one_or_none()
        if default_org_id is not None:
            still_member = self.cx.execute(
                sa.select(memberships.c.org_id).where(
                    memberships.c.user_id == user_id,
                    memberships.c.org_id == default_org_id,
                )
            ).scalar_one_or_none()
            if still_member is not None:
                return int(default_org_id)
        owner_org_id = self.cx.execute(
            sa.select(memberships.c.org_id)
            .where(memberships.c.user_id == user_id, memberships.c.role == "owner")
            .order_by(memberships.c.org_id)
            .limit(1)
        ).scalar_one_or_none()
        if owner_org_id is not None:
            return int(owner_org_id)
        org_id = self.cx.execute(
            sa.select(memberships.c.org_id)
            .where(memberships.c.user_id == user_id)
            .order_by(memberships.c.org_id)
            .limit(1)
        ).scalar_one_or_none()
        if org_id is not None:
            return int(org_id)
        # A project-only collaborator intentionally has no memberships row.
        # Resolve their session to the deterministic first live project org so
        # identity/profile paths retain their normal org-scoped shape, while
        # project_roles remains the only authorization grant.
        project_org_id = self.cx.execute(
            sa.select(project_roles.c.org_id)
            .select_from(
                project_roles.join(
                    projects,
                    sa.and_(
                        projects.c.org_id == project_roles.c.org_id,
                        projects.c.slug == project_roles.c.slug,
                    ),
                )
            )
            .where(project_roles.c.user_id == user_id)
            .order_by(project_roles.c.org_id, project_roles.c.slug)
            .limit(1)
        ).scalar_one_or_none()
        return None if project_org_id is None else int(project_org_id)
