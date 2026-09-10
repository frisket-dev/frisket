"""Open session resolution and instance/org identity.

Resolving who is calling (session cookie or bearer token), enforcing that they
are signed in / an admin / on a browser session, reading the instance's branding
for the pre-auth sign-in page, and editing a display name are all IDENTITY
concerns. None of them reads commerce state: the pre-sign-in `/api/instance`
path in particular no longer touches a funding store — the branding it serves
(display_name / welcome_message / support_contact) lives on the open `orgs`
table (frisket.team.schema), so it is an identity read
(the public package boundary).

The token service and unit of work are duck-typed (`Any`): open code must never
import an external edition (scripts/ci/import_boundaries.json).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any


class SessionRequired(PermissionError):
    """Raised when no valid auth channel resolves to a user."""


class SessionForbidden(PermissionError):
    """Raised when a valid auth channel cannot access a route."""


class IdentitySessionService:
    def __init__(
        self,
        uow_factory: Callable[[], Any],
        *,
        token_service: Any,
        admin_emails: Callable[[], set[str]] | None = None,
        is_org_owner: Callable[[int, int], bool] | None = None,
    ):
        self._uow_factory = uow_factory
        self._tokens = token_service
        self._admin_emails = admin_emails or (lambda: set())
        self._is_org_owner = is_org_owner

    def resolve_user(
        self,
        *,
        authorization: str,
        session_token: str,
    ) -> dict[str, Any] | None:
        if authorization.lower().startswith("bearer "):
            return self._tokens.resolve_user(authorization[7:].strip())
        if not session_token:
            return None
        with self._uow_factory() as uow:
            return uow.auth.session_user(session_token, now=datetime.now(UTC))

    def require_user(
        self,
        *,
        authorization: str,
        session_token: str,
    ) -> dict[str, Any]:
        user = self.resolve_user(
            authorization=authorization,
            session_token=session_token,
        )
        if user is None:
            raise SessionRequired("not signed in")
        return user

    def require_admin(
        self,
        *,
        authorization: str,
        session_token: str,
    ) -> dict[str, Any]:
        user = self.require_user(
            authorization=authorization,
            session_token=session_token,
        )
        if user.get("auth") == "pat":
            raise SessionForbidden("admin routes require a browser session")
        if self._is_org_owner is not None:
            allowed = self._is_org_owner(int(user["id"]), int(user["org_id"]))
        else:
            allowed = user["email"] in self._admin_emails()
        if not allowed:
            raise SessionForbidden("not an admin")
        return user

    def require_browser_user(
        self,
        *,
        authorization: str,
        session_token: str,
        detail: str = "browser session required",
    ) -> dict[str, Any]:
        user = self.require_user(
            authorization=authorization,
            session_token=session_token,
        )
        if user.get("auth") == "pat":
            raise SessionForbidden(detail)
        return user

    def revoke_browser_session(self, session_token: str) -> None:
        if not session_token:
            return
        with self._uow_factory() as uow:
            uow.auth.revoke_session(session_token)

    def instance_identity(self) -> dict[str, Any]:
        """Public, unauthenticated branding for the pre-auth sign-in page.

        Sourced from the first configured org (lowest id) — our own hosted
        deployment is not special-cased, it is just whichever org happens to
        be first when there is no signed-in user's org to read yet.
        """
        with self._uow_factory() as uow:
            identity = uow.auth.first_instance_identity()
        return {
            "display_name": identity["display_name"] or "frisket",
            "support_contact": identity["support_contact"],
        }

    def org_identity(self, user: dict[str, Any]) -> dict[str, Any]:
        """The signed-in user's own org identity, for `/api/me`."""
        with self._uow_factory() as uow:
            identity = uow.auth.identity_for_org(int(user["org_id"]))
        if identity is None:
            identity = {
                "display_name": None,
                "welcome_message": None,
                "support_contact": None,
            }
        return {
            "display_name": identity["display_name"] or "frisket",
            "welcome_message": identity["welcome_message"],
            "support_contact": identity["support_contact"],
        }

    def me_contributions(self, user: dict[str, Any]) -> dict[str, Any]:
        """Extension point: an edition's extra `/api/me` fields.

        Open identity contributes none. An external managed composition
        overrides this to add its customer's balance over the open identity
        payload.
        """
        return {}

    def update_profile(
        self,
        user: dict[str, Any],
        *,
        display_name: str | None,
        update_display_name: bool = True,
        cost_preapproval_usd: str | None = None,
    ) -> dict[str, Any]:
        clean_name = (display_name or "").strip() or None
        if clean_name is not None and len(clean_name) > 200:
            raise ValueError("display name must be 200 characters or fewer")
        threshold: Decimal | None = None
        if cost_preapproval_usd is not None:
            try:
                threshold = Decimal(cost_preapproval_usd.strip())
            except (InvalidOperation, ValueError) as exc:
                raise ValueError("pre-approval amount must be a decimal") from exc
            if not threshold.is_finite() or threshold < 0:
                raise ValueError("pre-approval amount must be non-negative")
            if threshold.as_tuple().exponent < -6 or threshold >= Decimal(
                "1000000000000"
            ):
                raise ValueError(
                    "pre-approval amount must fit in 12 whole and 6 decimal digits"
                )
        with self._uow_factory() as uow:
            if update_display_name:
                updated = uow.auth.update_user_name(
                    user_id=int(user["id"]),
                    name=clean_name,
                )
            else:
                updated = uow.auth.user_by_id(int(user["id"]))
                if updated is None:
                    raise ValueError("signed-in user no longer exists")
            if threshold is None:
                return updated
            return uow.auth.update_cost_preapproval(
                user_id=int(user["id"]), threshold_usd=threshold
            )
