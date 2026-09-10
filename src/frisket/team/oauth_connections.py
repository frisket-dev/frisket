"""Open-team Google connected-account OAuth routes and persistence.

This module intentionally owns only a user's Google Sheets connection.  It is
not an authentication provider: ordinary browser identity continues through
the team's magic-link/OIDC services.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from html import escape as html_escape
from typing import Any
from urllib.parse import urlencode

import httpx
import sqlalchemy as sa
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from frisket.contracts.http.oauth_connections import OAuthConnectionList
from frisket.server.route_errors import http_error_responses
from frisket.team.auth_challenges import AuthChallengeStore
from frisket.team.security.secrets import key_hint
from frisket.team.config import TeamConfig
from frisket.team.schema import audit_log, oauth_connections
from frisket.team.secret_box import TeamSecretBox

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
OAUTH_CONNECTION_STATE_COOKIE = "frisket_oauth_connection_state"
GOOGLE_SHEETS_CONNECTION_SCOPES = (
    "openid",
    "email",
    "profile",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
)
_LOG = logging.getLogger("frisket.team.oauth")

RequireBrowserUser = Callable[[Request], dict[str, Any]]
GoogleConnectionExchange = Callable[..., Awaitable[dict[str, Any] | None]]


class TeamOAuthConnectionService:
    """Single-org-safe connection store. `list_connections`/`_metadata` stay
    token-free (browser-facing); `connection(..., include_refresh_token=True)`
    is the one path that decrypts a stored refresh token, and it exists for
    exactly one caller: the `connected_account_resolver` this module's app
    composition wires into `ExecutorDeps` (frisket.team.app)."""

    def __init__(self, engine: sa.Engine, *, secret_box: TeamSecretBox):
        self._engine = engine
        self._secret_box = secret_box

    @staticmethod
    def google_connection_id(external_subject: str) -> str:
        subject = external_subject.strip()
        if not subject:
            raise ValueError("external_subject is required")
        return f"google_{hashlib.sha256(subject.encode()).hexdigest()[:16]}"

    def connect_google_account(
        self,
        *,
        org_id: int,
        user_id: int,
        external_subject: str,
        refresh_token: str,
        external_email: str | None = None,
        scopes: list[str] | None = None,
        token_type: str = "Bearer",
    ) -> dict[str, Any]:
        subject = external_subject.strip()
        token = refresh_token.strip()
        if not subject or not token:
            raise ValueError("external_subject and refresh_token are required")
        connection_id = self.google_connection_id(subject)
        now = datetime.now(UTC)
        values = {
            "external_subject": subject,
            "external_email": external_email.strip() if external_email else None,
            "scopes_json": self._scopes_json(scopes),
            "encrypted_refresh_token": self._secret_box.encrypt(token),
            "refresh_token_hint": key_hint(token),
            "token_type": token_type.strip() or "Bearer",
            "updated_at": now,
            "revoked_at": None,
        }
        with self._engine.begin() as cx:
            existing = cx.execute(
                sa.select(oauth_connections.c.id).where(
                    oauth_connections.c.org_id == org_id,
                    oauth_connections.c.provider == "google",
                    oauth_connections.c.connection_id == connection_id,
                )
            ).scalar_one_or_none()
            if existing is None:
                cx.execute(
                    oauth_connections.insert().values(
                        org_id=org_id,
                        provider="google",
                        connection_id=connection_id,
                        created_at=now,
                        **values,
                    )
                )
            else:
                cx.execute(
                    oauth_connections.update()
                    .where(oauth_connections.c.id == existing)
                    .values(**values)
                )
            cx.execute(
                audit_log.insert().values(
                    user_id=user_id, org_id=org_id, action="oauth_connection_google"
                )
            )
            row = cx.execute(
                sa.select(oauth_connections).where(
                    oauth_connections.c.org_id == org_id,
                    oauth_connections.c.provider == "google",
                    oauth_connections.c.connection_id == connection_id,
                )
            ).one()
        return self._metadata(row)

    def list_connections(
        self, org_id: int, *, provider: str | None = None
    ) -> list[dict[str, Any]]:
        query = sa.select(oauth_connections).where(
            oauth_connections.c.org_id == org_id,
            oauth_connections.c.revoked_at.is_(None),
        )
        if provider:
            query = query.where(
                oauth_connections.c.provider == provider.strip().lower()
            )
        with self._engine.connect() as cx:
            rows = cx.execute(
                query.order_by(oauth_connections.c.provider, oauth_connections.c.id)
            ).all()
        return [self._metadata(row) for row in rows]

    def connection(
        self,
        org_id: int,
        provider: str,
        connection_id: str,
        *,
        include_refresh_token: bool = False,
    ) -> dict[str, Any] | None:
        """Single-row lookup for `ExecutorDeps.connected_account_resolver`
        (engine/executor/action_families/exports.py) — the shape mirrors an
        external composition's `OAuthConnectionService.connection()` (the
        reference implementation this edition wires up to). Returns None for
        an unresolvable/missing/revoked connection; `refresh_token` is only
        decrypted and attached when explicitly requested, keeping the plain
        `list_connections`/browser-facing path token-free."""
        provider = provider.strip().lower()
        connection_id = connection_id.strip()
        with self._engine.connect() as cx:
            row = cx.execute(
                sa.select(oauth_connections).where(
                    oauth_connections.c.org_id == org_id,
                    oauth_connections.c.provider == provider,
                    oauth_connections.c.connection_id == connection_id,
                    oauth_connections.c.revoked_at.is_(None),
                )
            ).one_or_none()
        if row is None:
            return None
        out = self._metadata(row)
        if include_refresh_token:
            out["refresh_token"] = self._secret_box.decrypt(row.encrypted_refresh_token)
        return out

    def revoke_connection(
        self, org_id: int, provider: str, connection_id: str, *, user_id: int
    ) -> bool:
        provider = provider.strip().lower()
        connection_id = connection_id.strip()
        if not provider or not connection_id:
            return False
        with self._engine.begin() as cx:
            result = cx.execute(
                oauth_connections.update()
                .where(
                    oauth_connections.c.org_id == org_id,
                    oauth_connections.c.provider == provider,
                    oauth_connections.c.connection_id == connection_id,
                    oauth_connections.c.revoked_at.is_(None),
                )
                .values(revoked_at=datetime.now(UTC))
            )
            revoked = result.rowcount > 0
            if revoked:
                cx.execute(
                    audit_log.insert().values(
                        user_id=user_id,
                        org_id=org_id,
                        action=f"oauth_connection_revoked:{provider}",
                    )
                )
        return revoked

    @staticmethod
    def _scopes_json(scopes: list[str] | None) -> str:
        return json.dumps(
            sorted({item.strip() for item in scopes or [] if item.strip()}),
            separators=(",", ":"),
        )

    @staticmethod
    def _metadata(row: Any) -> dict[str, Any]:
        try:
            scopes = json.loads(row.scopes_json or "[]")
        except (TypeError, json.JSONDecodeError):
            scopes = []
        return {
            "id": row.connection_id,
            "connection_id": row.connection_id,
            "provider": row.provider,
            "external_subject": row.external_subject,
            "external_email": row.external_email,
            "scopes": scopes if isinstance(scopes, list) else [],
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            "revoked_at": row.revoked_at.isoformat() if row.revoked_at else None,
        }


def register_oauth_connection_routes(
    app: FastAPI,
    *,
    config: TeamConfig,
    engine: sa.Engine,
    secret_box: TeamSecretBox,
    require_browser_user: RequireBrowserUser,
    google_connection_exchange: GoogleConnectionExchange | None = None,
    service: TeamOAuthConnectionService | None = None,
) -> TeamOAuthConnectionService:
    """Register the four catalog routes before mounting the tenant core app.

    `service` lets a caller that already built a `TeamOAuthConnectionService`
    (frisket.team.app, to wire the SAME instance's `.connection()` into
    `ExecutorDeps.connected_account_resolver` before `create_app()` runs)
    hand it in instead of getting a second, independent-but-equivalent
    instance — both wrap the same stateless `engine`/`secret_box` pair, so
    either is correct; passing one in just avoids the duplicate object."""
    service = service or TeamOAuthConnectionService(engine, secret_box=secret_box)
    exchange = google_connection_exchange or production_google_connection_exchange(
        config
    )
    injected = google_connection_exchange is not None

    @app.get(
        "/api/org/oauth/connections",
        response_model=OAuthConnectionList,
        response_model_exclude_unset=True,
        responses=http_error_responses(401, 403, 500),
    )
    def list_oauth_connections(
        request: Request, provider: str | None = None
    ) -> dict[str, Any]:
        user = require_browser_user(request)
        return {
            "connections": service.list_connections(user["org_id"], provider=provider)
        }

    @app.delete("/api/org/oauth/connections/{provider}/{connection_id}")
    def revoke_oauth_connection(
        request: Request, provider: str, connection_id: str
    ) -> dict[str, bool]:
        user = require_browser_user(request)
        return {
            "revoked": service.revoke_connection(
                user["org_id"], provider, connection_id, user_id=user["id"]
            )
        }

    @app.get("/api/org/oauth/google/start")
    def google_connection_start(request: Request) -> Response:
        user = require_browser_user(request)
        if not _configured(config, require_secret=False, injected=injected):
            raise HTTPException(503, "Google OAuth is not configured")
        now = datetime.now(UTC)
        with engine.begin() as cx:
            state = AuthChallengeStore(cx).issue_connected_account_oauth(
                user_id=int(user["id"]),
                provider="google",
                expires_at=now + timedelta(minutes=10),
                now=now,
            )
        client_id = config.google_client_id or "injected-google-connection"
        params = urlencode(
            {
                "client_id": client_id,
                "redirect_uri": config.google_connection_redirect_uri,
                "response_type": "code",
                "scope": " ".join(GOOGLE_SHEETS_CONNECTION_SCOPES),
                "state": state,
                "access_type": "offline",
                "prompt": "consent select_account",
                "include_granted_scopes": "true",
            }
        )
        response = RedirectResponse(f"{GOOGLE_AUTH_URL}?{params}", status_code=302)
        response.set_cookie(
            OAUTH_CONNECTION_STATE_COOKIE,
            state,
            httponly=True,
            secure=config.secure_cookies,
            samesite="lax",
            path="/api/org/oauth",
            max_age=600,
        )
        return response

    @app.get("/api/org/oauth/google/callback")
    async def google_connection_callback(
        request: Request, code: str = "", state: str = "", error: str = ""
    ) -> Response:
        try:
            user = require_browser_user(request)
        except HTTPException as exc:
            return _callback_http_error(exc)
        if error:
            _consume_state_if_cookie_matches(request, engine, user["id"], state)
            return _error_response(
                f"<h3>Google connection failed: {html_escape(error)}</h3>"
            )
        if not _configured(config, require_secret=True, injected=injected):
            return _callback_json_error(503, "Google OAuth is not configured")
        expected = request.cookies.get(OAUTH_CONNECTION_STATE_COOKIE, "")
        if not state or not expected or not secrets.compare_digest(state, expected):
            return _error_response(
                "<h3>Google connection could not be verified (state mismatch).</h3>"
            )
        if not _consume_state(engine, user_id=user["id"], state=state):
            return _error_response(
                "<h3>Google connection could not be verified (state mismatch).</h3>"
            )
        if not code:
            return _error_response(
                "<h3>Google connection could not be verified (state mismatch).</h3>"
            )
        connected = await exchange(code, config=config)
        if not isinstance(connected, dict):
            return _error_response(
                "<h3>Google did not return a usable connected account.</h3>"
            )
        refresh_token = str(connected.get("refresh_token") or "").strip()
        subject = str(connected.get("external_subject") or "").strip()
        if not refresh_token or not subject:
            return _error_response(
                "<h3>Google did not return offline account access.</h3>"
            )
        scopes = [
            value.strip()
            for value in connected.get("scopes", [])
            if isinstance(value, str) and value.strip()
        ]
        service.connect_google_account(
            org_id=user["org_id"],
            user_id=user["id"],
            external_subject=subject,
            external_email=str(connected.get("external_email") or "").strip() or None,
            refresh_token=refresh_token,
            scopes=scopes,
            token_type=str(connected.get("token_type") or "Bearer"),
        )
        response = RedirectResponse("/?connected=google_sheets", status_code=302)
        response.delete_cookie(OAUTH_CONNECTION_STATE_COOKIE, path="/api/org/oauth")
        return response

    return service


def _configured(config: TeamConfig, *, require_secret: bool, injected: bool) -> bool:
    if injected:
        return True
    if not config.google_client_id or not config.google_connection_redirect_uri:
        return False
    return bool(config.google_client_secret) if require_secret else True


def _error_response(body: str) -> HTMLResponse:
    response = HTMLResponse(body, status_code=400)
    response.delete_cookie(OAUTH_CONNECTION_STATE_COOKIE, path="/api/org/oauth")
    return response


def _callback_json_error(status_code: int, detail: str) -> JSONResponse:
    response = JSONResponse({"detail": detail}, status_code=status_code)
    response.delete_cookie(OAUTH_CONNECTION_STATE_COOKIE, path="/api/org/oauth")
    return response


def _callback_http_error(exc: HTTPException) -> JSONResponse:
    response = JSONResponse(
        {"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers
    )
    response.delete_cookie(OAUTH_CONNECTION_STATE_COOKIE, path="/api/org/oauth")
    return response


def _consume_state_if_cookie_matches(
    request: Request, engine: sa.Engine, user_id: int, state: str
) -> bool:
    expected = request.cookies.get(OAUTH_CONNECTION_STATE_COOKIE, "")
    if not state or not expected or not secrets.compare_digest(state, expected):
        return False
    return _consume_state(engine, user_id=user_id, state=state)


def _consume_state(engine: sa.Engine, *, user_id: int, state: str) -> bool:
    """Atomically spend one browser-bound OAuth state; replay fails closed."""
    with engine.begin() as cx:
        return AuthChallengeStore(cx).consume_connected_account_oauth(
            state,
            user_id=user_id,
            provider="google",
            now=datetime.now(UTC),
        )


def production_google_connection_exchange(
    config: TeamConfig,
) -> GoogleConnectionExchange:
    async def exchange(code: str, **_kwargs: Any) -> dict[str, Any] | None:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                token_response = await client.post(
                    GOOGLE_TOKEN_URL,
                    data={
                        "code": code,
                        "client_id": config.google_client_id,
                        "client_secret": config.google_client_secret,
                        "redirect_uri": config.google_connection_redirect_uri,
                        "grant_type": "authorization_code",
                    },
                )
                if token_response.status_code != 200:
                    return None
                token_data = token_response.json()
                access_token = token_data.get("access_token")
                if not access_token:
                    return None
                info_response = await client.get(
                    GOOGLE_USERINFO_URL,
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                if info_response.status_code != 200:
                    return None
                profile = info_response.json()
        except Exception as exc:  # OAuth callback fails closed; do not leak details.
            _LOG.warning(
                "google_oauth_connection_exchange_failed",
                extra={
                    "event": "google_oauth_connection_exchange_failed",
                    "exception_type": type(exc).__name__,
                },
            )
            return None
        if profile.get("email_verified") not in (True, "true") or not profile.get(
            "sub"
        ):
            return None
        scope = token_data.get("scope", "")
        scopes = scope.split() if isinstance(scope, str) else []
        return {
            "refresh_token": token_data.get("refresh_token"),
            "external_subject": profile.get("sub"),
            "external_email": profile.get("email"),
            "scopes": scopes,
            "token_type": token_data.get("token_type") or "Bearer",
        }

    return exchange


__all__ = [
    "GOOGLE_SHEETS_CONNECTION_SCOPES",
    "GoogleConnectionExchange",
    "TeamOAuthConnectionService",
    "production_google_connection_exchange",
    "register_oauth_connection_routes",
]
