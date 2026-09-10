"""Open identity sign-in: magic links, user-ensure, session issuance.

The open sign-in path is CREDIT-FREE. Signing in means: redeem the link,
ensure the user and their first org exist, mint a session, append an audit row.
No commerce state is read or written, so this path completes unchanged against a
deployment whose database has no commerce tables.

An edition layers its own work over sign-in by overriding
`_contribute_to_sign_in`, which runs inside the SAME unit of work as the session
write — so an edition's contribution and the sign-in commit or roll back
together. An external managed composition uses that seam to grant trial
credits (its `HostedAuthService` contribution).

The unit of work is duck-typed (`Any`): open code must never import
an external edition (scripts/ci/import_boundaries.json).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from frisket.team.local_auth import InvalidCredentials, hash_password, verify_password


SESSION_TTL_DAYS = 30


class OidcAdmissionRequired(PermissionError):
    """A verified OIDC identity is new and not yet admissible.

    Raised by `sign_in_oidc` when a provider identity authenticates with a
    verified email that maps to no local user and passes no admission rule.
    It subclasses `PermissionError` so existing callers that convert admission
    failures to a 403 keep working unchanged; a composition that wants to route
    the identity into its own access-request funnel catches this narrower type
    and reads `email`/`claims` off it.
    """

    def __init__(self, message: str, *, email: str):
        super().__init__(message)
        self.email = email


@dataclass(frozen=True)
class TeamSignIn:
    user: dict[str, Any]
    session: str


class IdentityAuthService:
    def __init__(self, uow_factory: Callable[[], Any]):
        self._uow_factory = uow_factory

    @staticmethod
    def _email(email: str) -> str:
        return email.lower().strip()

    def _contribute_to_sign_in(
        self,
        uow: Any,
        user: dict[str, Any],
        *,
        reason: str | None = None,
    ) -> None:
        """Extension point: an edition's own work on sign-in, in the same uow.

        Open identity contributes nothing. Overridden by an external edition.
        """

    def _email_is_admissible(self, uow: Any, email: str, *, now: datetime) -> bool:
        """Return whether a new identity may be created in this composition."""

        return bool(uow.auth.email_is_admissible(email, now=now))

    def create_magic_link(
        self,
        email: str,
        ttl_minutes: int = 30,
        *,
        max_active: int | None = None,
    ) -> str:
        now = datetime.now(UTC)
        with self._uow_factory() as uow:
            return uow.auth.create_magic_link(
                email=email,
                expires_at=now + timedelta(minutes=ttl_minutes),
                now=now,
                max_active=max_active,
            )

    def pending_email_for_token(self, token: str) -> str | None:
        """Peek a magic link's target email WITHOUT consuming it.

        Read-only: opens a unit of work and reads the token's email so a
        caller can decide whether redeeming would switch an already-signed-in
        browser to a different identity. Never marks the token used.
        """
        with self._uow_factory() as uow:
            return uow.auth.email_for_token(token)

    def is_invited_via_token(self, token: str) -> bool:
        """Was this (now-invalid) token's email invited into an org?

        Used only on a failed redemption to decide whether to send the user
        back to sign-in with an `invited=1` marker (so the request-access
        form stays hidden — they are already invited, they just need a
        fresh link) instead of the generic dead-end page.
        """
        now = datetime.now(UTC)
        with self._uow_factory() as uow:
            email = uow.auth.email_for_token(token)
            if email is None:
                return False
            return uow.auth.has_valid_pending_invite(email, now=now)

    def ensure_user_for_login(self, email: str) -> dict[str, Any]:
        with self._uow_factory() as uow:
            return uow.auth.ensure_user_for_login(email)

    def redeem_magic_link_and_sign_in(self, token: str) -> TeamSignIn | None:
        with self._uow_factory() as uow:
            now = datetime.now(UTC)
            email = uow.auth.pending_magic_link_email(token, now=now)
            if email is None:
                return None
            if not self._email_is_admissible(uow, email, now=now):
                raise PermissionError("account admission requires an invite")
            email = uow.auth.redeem_magic_link(token, now=now)
            if email is None:
                return None
            return self._sign_in_email_in_uow(uow, email, audit_action="login")

    def magic_link_requires_password(self, token: str) -> bool | None:
        """Return whether a live link targets an identity without a password."""

        with self._uow_factory() as uow:
            now = datetime.now(UTC)
            email = uow.auth.pending_magic_link_email(token, now=now)
            if email is None:
                return None
            if not self._email_is_admissible(uow, email, now=now):
                raise PermissionError("account admission requires an invite")
            row = uow.auth.user_for_local_login(email)
            return row is None or not row.password_hash

    def complete_magic_link_with_password(
        self, token: str, password: str
    ) -> TeamSignIn | None:
        password_hash = hash_password(password)
        with self._uow_factory() as uow:
            now = datetime.now(UTC)
            email = uow.auth.pending_magic_link_email(token, now=now)
            if email is None:
                return None
            if not self._email_is_admissible(uow, email, now=now):
                raise PermissionError("account admission requires an invite")
            email = uow.auth.redeem_magic_link(token, now=now)
            if email is None:
                return None
            user = uow.auth.ensure_user_for_login(email)
            row = uow.auth.user_for_local_login(email)
            if row is None or row.password_hash:
                raise ValueError("this account already has a password")
            uow.auth.set_password_hash(
                user_id=int(user["id"]), password_hash=password_hash
            )
            return self._sign_in_user_in_uow(uow, user, audit_action="login")

    def sign_in_password(self, *, email: str, password: str) -> TeamSignIn:
        """Verify a local password and issue its session in the shared UOW."""

        clean_email = self._email(email)
        lookup_email = clean_email if len(clean_email) <= 320 else "invalid-local-login"
        bounded_password = password if len(password) <= 1024 else ""
        with self._uow_factory() as uow:
            row = uow.auth.user_for_local_login(lookup_email)
            verified = verify_password(
                None if row is None else row.password_hash,
                bounded_password,
            )
            if row is None or not verified or len(password) > 1024:
                raise InvalidCredentials("invalid email or password")
            user = uow.auth.user_by_id(int(row.id))
            if user is None:
                raise InvalidCredentials("invalid email or password")
            return self._sign_in_user_in_uow(
                uow, user, audit_action="local_password_login"
            )

    def sign_in_email(self, email: str, *, audit_action: str) -> TeamSignIn:
        with self._uow_factory() as uow:
            return self._sign_in_email_in_uow(uow, email, audit_action=audit_action)

    def sign_in_user(
        self,
        user: dict[str, Any],
        *,
        audit_action: str | None = None,
        reason: str | None = None,
    ) -> TeamSignIn:
        with self._uow_factory() as uow:
            return self._sign_in_user_in_uow(
                uow,
                user,
                audit_action=audit_action,
                reason=reason,
            )

    def sign_in_oidc(self, *, issuer: str, subject: str, email: str) -> TeamSignIn:
        """Bind a verified provider identity and issue its session atomically."""

        with self._uow_factory() as uow:
            user = uow.auth.user_for_oidc(issuer=issuer, subject=subject)
            if user is None:
                existing = uow.auth.user_for_local_login(email)
                if existing is None and not self._email_is_admissible(
                    uow, email, now=datetime.now(UTC)
                ):
                    raise OidcAdmissionRequired(
                        "account admission requires an invite", email=email
                    )
                user = (
                    uow.auth.user_by_id(int(existing.id))
                    if existing is not None
                    else uow.auth.ensure_user_for_login(email)
                )
                if user is None:
                    raise PermissionError("account access is not available")
                bound_subject = uow.auth.oidc_subject_for_user(
                    issuer=issuer, user_id=int(user["id"])
                )
                if bound_subject is not None:
                    raise ValueError(
                        "this account is already linked to another provider identity"
                    )
                uow.auth.bind_oidc(
                    issuer=issuer,
                    subject=subject,
                    user_id=int(user["id"]),
                    email=email,
                )
            return self._sign_in_user_in_uow(uow, user, audit_action="login")

    def _sign_in_email_in_uow(
        self,
        uow: Any,
        email: str,
        *,
        audit_action: str,
    ) -> TeamSignIn:
        user = uow.auth.ensure_user_for_login(email)
        return self._sign_in_user_in_uow(uow, user, audit_action=audit_action)

    def _sign_in_user_in_uow(
        self,
        uow: Any,
        user: dict[str, Any],
        *,
        audit_action: str | None,
        reason: str | None = None,
    ) -> TeamSignIn:
        self._contribute_to_sign_in(uow, user, reason=reason)
        now = datetime.now(UTC)
        session = uow.auth.create_session(
            user_id=int(user["id"]),
            created_at=now,
            expires_at=now + timedelta(days=SESSION_TTL_DAYS),
        )
        if audit_action is not None:
            uow.audit.append(
                user_id=int(user["id"]),
                org_id=int(user["org_id"]),
                action=audit_action,
            )
        return TeamSignIn(user=user, session=session)
