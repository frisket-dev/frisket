"""The OIDC callback exposes a funnel seam for a new, not-yet-admissible identity.

A composition (the hosted service) turns the default 403 into an access-request
flow by passing ``on_oidc_admission_denied`` to ``register_browser_auth_routes``.
The hook receives the verified claims (email plus the optional provider ``name``)
and may return a Response to redirect the browser; returning nothing keeps the
default deny. These tests lock that seam without any cloud dependency.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import sqlalchemy as sa
from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.testclient import TestClient

from frisket.team.browser_auth_routes import register_browser_auth_routes
from frisket.team.config import OIDCProviderConfig
from frisket.team.identity_service import OidcAdmissionRequired
from frisket.team.schema import metadata


_PROVIDER = OIDCProviderConfig(
    issuer="https://id.test",
    client_id="client",
    client_secret="secret",
    authorization_endpoint="https://id.test/authorize",
    token_endpoint="https://id.test/token",
    jwks_uri="https://id.test/jwks",
)


class _AdmissionDenyingAuth:
    """Every new OIDC identity is inadmissible, as for a not-yet-invited user."""

    def sign_in_oidc(self, *, issuer: str, subject: str, email: str) -> Any:
        raise OidcAdmissionRequired("account admission requires an invite", email=email)


def _exchange_returning(claims: dict[str, Any]):
    async def exchange(
        _provider: str, _code: str, _redirect_uri: str, expected_nonce: str
    ) -> dict[str, Any]:
        return {**claims, "nonce": expected_nonce}

    return exchange


def _app(tmp_path: Path, *, claims: dict[str, Any], hook) -> FastAPI:
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'control.db'}")
    metadata.create_all(engine)
    app = FastAPI()
    register_browser_auth_routes(
        app,
        engine=engine,
        auth=_AdmissionDenyingAuth(),
        base_url="http://testserver",
        session_cookie="frisket_session",
        secure_cookies=False,
        magic_link_enabled=False,
        oidc_providers={"work": _PROVIDER},
        send_magic_email=None,
        oidc_exchange=_exchange_returning(claims),
        resolve_request=lambda _request: None,
        on_oidc_admission_denied=hook,
    )
    return app


def _callback(client: TestClient) -> Any:
    start = client.get("/auth/oidc/work", follow_redirects=False)
    assert start.status_code == 302, start.text
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    return client.get(
        f"/auth/oidc/work/callback?code=valid&state={state}",
        follow_redirects=False,
    )


def test_admission_hook_redirects_a_new_identity_and_receives_verified_name(
    tmp_path: Path,
) -> None:
    seen: list[tuple[str, dict[str, Any]]] = []

    def hook(provider: str, claims):  # noqa: ANN001
        seen.append((provider, dict(claims)))
        return RedirectResponse("/sign-up?requested=work", status_code=302)

    claims = {
        "email": "newcomer@example.com",
        "email_verified": True,
        "subject": "subject-1",
        "name": "Ada Newcomer",
    }
    client = TestClient(_app(tmp_path, claims=claims, hook=hook))

    callback = _callback(client)

    assert callback.status_code == 302
    assert callback.headers["location"] == "/sign-up?requested=work"
    # No session was minted: admission is still gated.
    assert client.cookies.get("frisket_session") is None
    assert len(seen) == 1
    provider, funnel_claims = seen[0]
    assert provider == "work"
    assert funnel_claims["email"] == "newcomer@example.com"
    # The provider `name` claim reaches the funnel (profile scope is requested).
    assert funnel_claims["name"] == "Ada Newcomer"


def test_without_a_hook_a_new_identity_still_gets_the_default_403(
    tmp_path: Path,
) -> None:
    claims = {
        "email": "newcomer@example.com",
        "email_verified": True,
        "subject": "subject-1",
        "name": "Ada Newcomer",
    }
    client = TestClient(_app(tmp_path, claims=claims, hook=None))

    callback = _callback(client)

    assert callback.status_code == 403
    assert callback.json()["detail"] == "account access is not available"
    assert client.cookies.get("frisket_session") is None


def test_a_hook_that_declines_falls_back_to_the_default_403(tmp_path: Path) -> None:
    claims = {
        "email": "newcomer@example.com",
        "email_verified": True,
        "subject": "subject-1",
    }
    client = TestClient(_app(tmp_path, claims=claims, hook=lambda _p, _c: None))

    callback = _callback(client)

    assert callback.status_code == 403


def test_oidc_admission_required_is_a_permission_error_carrying_the_email() -> None:
    exc = OidcAdmissionRequired("nope", email="who@example.com")
    assert isinstance(exc, PermissionError)
    assert exc.email == "who@example.com"
