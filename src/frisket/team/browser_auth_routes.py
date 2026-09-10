"""Shared password, magic-link, and OIDC browser route registration."""

from __future__ import annotations

import logging
import secrets
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import sqlalchemy as sa
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse

from frisket.contracts.http.browser_auth import (
    BrowserAuthCompletePasswordRequest,
    BrowserAuthCompletePasswordResponse,
    BrowserAuthPasswordLoginRequest,
    BrowserAuthPasswordLoginResponse,
    BrowserAuthRequestLinkRequest,
    BrowserAuthRequestLinkResponse,
)
from frisket.server.route_errors import http_error_responses
from frisket.team.auth_challenges import AuthChallengeStore, TooManyActiveOidcSignIns
from frisket.team.auth_limits import (
    auth_budget,
    clear,
    reserve_many,
    trusted_client_ip,
    trusted_proxy_networks,
)
from frisket.team.config import OIDCProviderConfig
from frisket.team.db import locked_transaction
from frisket.team.identity_service import IdentityAuthService, OidcAdmissionRequired
from frisket.team.local_auth import InvalidCredentials, SetupError

_LOG = logging.getLogger("frisket.team.browser_auth")

SendMagicEmail = Callable[[str, str], Awaitable[bool]]
OIDCExchange = Callable[[str, str, str, str], Awaitable[dict[str, Any]]]
ResolveRequest = Callable[[Request], Mapping[str, Any] | None]
# Invoked when a NEW verified OIDC identity is not admissible. Given the
# provider name and the verified claims (`email`, optional `name`, ...), a
# composition may return a Response to redirect the identity into its own
# access-request funnel instead of the default 403. Returning None keeps the
# default deny.
OidcAdmissionDenied = Callable[[str, Mapping[str, Any]], Response | None]


_PASSWORD_SOURCE_LIMIT = 30
_PASSWORD_SOURCE_WINDOW_SECONDS = 5 * 60
_PASSWORD_EMAIL_LIMIT = 10
_PASSWORD_EMAIL_WINDOW_SECONDS = 15 * 60
_MAGIC_SOURCE_LIMIT = 20
_MAGIC_SOURCE_WINDOW_SECONDS = 10 * 60
_MAGIC_EMAIL_LIMIT = 3
_MAGIC_EMAIL_WINDOW_SECONDS = 30 * 60
_OIDC_SOURCE_LIMIT = 20
_OIDC_SOURCE_WINDOW_SECONDS = 10 * 60
_OIDC_MAX_ACTIVE_STATES = 1_000


def _set_cookie(response: Response, *, name: str, value: str, secure: bool) -> None:
    response.set_cookie(
        name,
        value,
        httponly=True,
        secure=secure,
        samesite="lax",
        max_age=30 * 86400,
    )


def _no_store(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store"
    return response


def register_browser_auth_routes(
    app: FastAPI,
    *,
    engine: sa.Engine,
    auth: IdentityAuthService,
    base_url: str,
    session_cookie: str,
    secure_cookies: bool,
    magic_link_enabled: bool,
    oidc_providers: Mapping[str, OIDCProviderConfig],
    send_magic_email: SendMagicEmail | None,
    oidc_exchange: OIDCExchange | None,
    resolve_request: ResolveRequest,
    email_can_request_link: Callable[[str], bool] | None = None,
    on_magic_delivery_failure: Callable[[str], None] | None = None,
    on_oidc_admission_denied: OidcAdmissionDenied | None = None,
    trusted_proxy_cidrs: Sequence[str] = (),
) -> None:
    """Install the browser auth lifecycle for one Team-capable composition."""

    proxy_networks = trusted_proxy_networks(trusted_proxy_cidrs)

    def request_source(request: Request) -> str:
        return trusted_client_ip(request, proxy_networks)

    @app.post(
        "/auth/password-login",
        response_model=BrowserAuthPasswordLoginResponse,
        responses=http_error_responses(401, 403, 422, 429, 500),
    )
    def password_login(
        request: Request,
        body: BrowserAuthPasswordLoginRequest,
    ) -> Response:
        if request.headers.get("origin") != base_url:
            raise HTTPException(
                403,
                "invalid login origin",
                headers={"Cache-Control": "no-store"},
            )
        clean_email = body.email.lower().strip()
        throttle_key = clean_email if len(clean_email) <= 320 else "invalid-local-login"
        email_budget = auth_budget(
            "password_email",
            throttle_key,
            limit=_PASSWORD_EMAIL_LIMIT,
            window_seconds=_PASSWORD_EMAIL_WINDOW_SECONDS,
        )
        source_budget = auth_budget(
            "password_source",
            request_source(request),
            limit=_PASSWORD_SOURCE_LIMIT,
            window_seconds=_PASSWORD_SOURCE_WINDOW_SECONDS,
        )
        with locked_transaction(engine, lock_scope=("browser-auth-limits",)) as cx:
            allowed = reserve_many(
                cx, (source_budget, email_budget), now=datetime.now(UTC)
            )
        if not allowed:
            raise HTTPException(
                429,
                "too many login attempts",
                headers={"Cache-Control": "no-store", "Retry-After": "900"},
            )
        try:
            sign_in = auth.sign_in_password(email=body.email, password=body.password)
        except InvalidCredentials as exc:
            raise HTTPException(
                401,
                "invalid email or password",
                headers={"Cache-Control": "no-store"},
            ) from exc
        with locked_transaction(engine, lock_scope=("browser-auth-limits",)) as cx:
            clear(cx, email_budget)
        response = JSONResponse(BrowserAuthPasswordLoginResponse(ok=True).model_dump())
        _set_cookie(
            response,
            name=session_cookie,
            value=sign_in.session,
            secure=secure_cookies,
        )
        return _no_store(response)

    @app.post(
        "/auth/request-link",
        response_model=BrowserAuthRequestLinkResponse,
        responses=http_error_responses(400, 404, 422, 500, 502),
    )
    async def request_link(
        request: Request, body: BrowserAuthRequestLinkRequest
    ) -> BrowserAuthRequestLinkResponse:
        if not magic_link_enabled or send_magic_email is None:
            raise HTTPException(404, "magic-link auth is disabled")
        email = body.email.lower().strip()
        if "@" not in email:
            raise HTTPException(400, "valid email required")
        source_budget = auth_budget(
            "magic_source",
            request_source(request),
            limit=_MAGIC_SOURCE_LIMIT,
            window_seconds=_MAGIC_SOURCE_WINDOW_SECONDS,
        )
        with locked_transaction(engine, lock_scope=("browser-auth-limits",)) as cx:
            source_allowed = reserve_many(cx, (source_budget,), now=datetime.now(UTC))
        if not source_allowed:
            return BrowserAuthRequestLinkResponse(sent=True)
        if email_can_request_link is not None and not email_can_request_link(email):
            return BrowserAuthRequestLinkResponse(sent=True)
        now = datetime.now(UTC)
        email_budget = auth_budget(
            "magic_email",
            email,
            limit=_MAGIC_EMAIL_LIMIT,
            window_seconds=_MAGIC_EMAIL_WINDOW_SECONDS,
        )
        with locked_transaction(engine, lock_scope=("browser-auth-limits",)) as cx:
            email_allowed = reserve_many(cx, (email_budget,), now=now)
            if email_allowed:
                token = AuthChallengeStore(cx).issue_magic_link(
                    email=email,
                    expires_at=now + timedelta(minutes=30),
                    now=now,
                )
        if not email_allowed:
            return BrowserAuthRequestLinkResponse(sent=True)
        try:
            sent = await send_magic_email(
                email, f"{base_url}/auth/callback?token={token}"
            )
        except Exception as exc:  # noqa: BLE001
            from frisket.team.diagnostics import classify_team_boot_error, sanitize_text

            _LOG.exception(
                "magic_link_send_failed",
                extra={
                    "event": "magic_link_send_failed",
                    "reason": sanitize_text(exc, limit=500),
                },
            )
            remediated = classify_team_boot_error(exc)
            if remediated is None:
                raise
            raise HTTPException(
                502,
                {
                    "message": remediated.message,
                    "code": remediated.code,
                    "route": remediated.details.get("route"),
                },
            ) from exc
        if not sent and on_magic_delivery_failure is not None:
            on_magic_delivery_failure(email)
        return BrowserAuthRequestLinkResponse(sent=True)

    @app.get("/auth/callback")
    def callback(token: str, request: Request) -> Response:
        current = resolve_request(request)
        if current is not None:
            current_email = str(current["email"]).lower().strip()
            target_email = auth.pending_email_for_token(token)
            if (
                target_email is not None
                and target_email.lower().strip() == current_email
            ):
                return RedirectResponse("/", status_code=302)
            raise HTTPException(
                409,
                f"You're already signed in as {current_email}. Sign out (or "
                "open this link in a private window) to accept this invite.",
            )
        try:
            password_required = auth.magic_link_requires_password(token)
        except PermissionError as exc:
            raise HTTPException(403, "account access is not available") from exc
        if password_required is None:
            raise HTTPException(400, "link expired or already used")
        if password_required:
            return RedirectResponse(
                f"/?{urlencode({'auth_kind': 'magic-link', 'auth_token': token})}",
                status_code=302,
            )
        try:
            sign_in = auth.redeem_magic_link_and_sign_in(token)
        except PermissionError as exc:
            raise HTTPException(403, "account access is not available") from exc
        if sign_in is None:
            raise HTTPException(400, "link expired or already used")
        response = RedirectResponse("/", status_code=302)
        _set_cookie(
            response,
            name=session_cookie,
            value=sign_in.session,
            secure=secure_cookies,
        )
        return response

    @app.post(
        "/auth/callback",
        response_model=BrowserAuthCompletePasswordResponse,
        responses=http_error_responses(400, 403, 422, 500),
    )
    def complete_magic_link(
        token: str, body: BrowserAuthCompletePasswordRequest
    ) -> Response:
        try:
            sign_in = auth.complete_magic_link_with_password(token, body.password)
        except PermissionError as exc:
            raise HTTPException(403, "account access is not available") from exc
        except (SetupError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc
        if sign_in is None:
            raise HTTPException(400, "link expired or already used")
        response = JSONResponse(
            BrowserAuthCompletePasswordResponse(ok=True).model_dump()
        )
        _set_cookie(
            response,
            name=session_cookie,
            value=sign_in.session,
            secure=secure_cookies,
        )
        return _no_store(response)

    @app.get("/auth/oidc/{provider}")
    def oidc_start(provider: str, request: Request) -> Response:
        definition = oidc_providers.get(provider)
        if definition is None:
            raise HTTPException(404, "unknown OIDC provider")
        now = datetime.now(UTC)
        nonce = secrets.token_urlsafe(24)
        source_budget = auth_budget(
            "oidc_source",
            request_source(request),
            limit=_OIDC_SOURCE_LIMIT,
            window_seconds=_OIDC_SOURCE_WINDOW_SECONDS,
        )
        state: str | None = None
        with locked_transaction(engine, lock_scope=("browser-auth-limits",)) as cx:
            if reserve_many(cx, (source_budget,), now=now):
                previous_state = request.cookies.get(f"frisket_oidc_{provider}", "")
                try:
                    state = AuthChallengeStore(cx).issue_oidc_sign_in(
                        provider=provider,
                        nonce=nonce,
                        expires_at=now + timedelta(minutes=10),
                        now=now,
                        max_active=_OIDC_MAX_ACTIVE_STATES,
                        replace_secret=previous_state or None,
                    )
                except TooManyActiveOidcSignIns:
                    pass
        if state is None:
            raise HTTPException(
                429,
                "too many OIDC attempts",
                headers={"Retry-After": "600"},
            )
        redirect_uri = f"{base_url}/auth/oidc/{provider}/callback"
        query = urlencode(
            {
                "client_id": definition.client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": " ".join(definition.scopes),
                "state": state,
                "nonce": nonce,
            }
        )
        response = RedirectResponse(
            f"{definition.authorization_endpoint}?{query}", status_code=302
        )
        response.set_cookie(
            f"frisket_oidc_{provider}",
            state,
            httponly=True,
            secure=secure_cookies,
            samesite="lax",
            max_age=600,
        )
        return response

    @app.get("/auth/oidc/{provider}/callback")
    async def oidc_callback(
        provider: str, code: str, state: str, request: Request
    ) -> Response:
        correlation = request.cookies.get(f"frisket_oidc_{provider}")
        if not correlation or not secrets.compare_digest(correlation, state):
            raise HTTPException(400, "OIDC browser correlation failed")
        with engine.begin() as cx:
            nonce = AuthChallengeStore(cx).consume_oidc_sign_in(
                state,
                provider=provider,
                now=datetime.now(UTC),
            )
        if nonce is None:
            raise HTTPException(400, "invalid or expired OIDC state")
        if provider not in oidc_providers or oidc_exchange is None:
            raise HTTPException(400, "OIDC provider is not configured")
        try:
            claims = await oidc_exchange(
                provider,
                code,
                f"{base_url}/auth/oidc/{provider}/callback",
                nonce,
            )
        except Exception as exc:  # noqa: BLE001
            _LOG.warning(
                "oidc_exchange_failed",
                extra={"event": "oidc_exchange_failed", "provider": provider},
            )
            raise HTTPException(502, "OIDC provider exchange failed") from exc
        if not isinstance(claims, Mapping):
            raise HTTPException(502, "OIDC provider returned an invalid response")
        if claims.get("nonce") != nonce:
            raise HTTPException(403, "OIDC nonce mismatch")
        if (
            claims.get("email_verified") is not True
            or not claims.get("email")
            or not claims.get("subject")
        ):
            raise HTTPException(403, "OIDC email must be verified")
        definition = oidc_providers[provider]
        try:
            sign_in = auth.sign_in_oidc(
                issuer=definition.issuer,
                subject=str(claims["subject"]),
                email=str(claims["email"]),
            )
        except OidcAdmissionRequired as exc:
            if on_oidc_admission_denied is not None:
                funnel = on_oidc_admission_denied(provider, claims)
                if funnel is not None:
                    funnel.delete_cookie(f"frisket_oidc_{provider}")
                    return funnel
            raise HTTPException(403, "account access is not available") from exc
        except PermissionError as exc:
            raise HTTPException(403, "account access is not available") from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        response = RedirectResponse("/", status_code=302)
        _set_cookie(
            response,
            name=session_cookie,
            value=sign_in.session,
            secure=secure_cookies,
        )
        response.delete_cookie(f"frisket_oidc_{provider}")
        return response


__all__ = ["register_browser_auth_routes"]
