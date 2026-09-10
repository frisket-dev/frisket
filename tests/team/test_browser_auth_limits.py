from __future__ import annotations

import concurrent.futures
import hashlib
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import sqlalchemy as sa
from fastapi import Request
from fastapi.testclient import TestClient

import frisket.team.browser_auth_routes as browser_auth_routes
from frisket.team.app import TeamConfig, create_team_app
from frisket.team.auth_challenges import MAGIC_LINK, OIDC_SIGN_IN, AuthChallengeStore
from frisket.team.auth_limits import (
    auth_budget,
    trusted_client_ip,
    trusted_proxy_networks,
)
from frisket.team.identity_service import IdentityAuthService
from frisket.team.local_auth import InvalidCredentials
from frisket.team.schema import auth_attempt_buckets, auth_challenges
from tests.team_setup_helpers import claim_server


def _config(tmp_path: Path, *, database_name: str = "control.db") -> TeamConfig:
    return TeamConfig(
        database_url=f"sqlite:///{tmp_path / database_name}",
        run_queue_database_url=f"sqlite:///{tmp_path / 'queue.db'}",
        data_dir=tmp_path / "data",
        base_url="http://testserver",
        organization_name="Auth limits",
        magic_link_enabled=True,
        oidc_providers={
            "work": {
                "issuer": "https://id.test",
                "client_id": "client",
                "client_secret": "secret",
                "authorization_endpoint": "https://id.test/authorize",
                "token_endpoint": "https://id.test/token",
                "jwks_uri": "https://id.test/jwks",
            }
        },
    )


async def _mail(_email: str, _link: str) -> bool:
    return True


async def _oidc(_provider: str, _code: str, _redirect: str, nonce: str) -> dict:
    return {
        "email": "oidc@example.test",
        "email_verified": True,
        "subject": "subject",
        "nonce": nonce,
    }


def _app(config: TeamConfig, *, mail=_mail):
    return create_team_app(
        config,
        send_magic_email=mail,
        oidc_exchange=_oidc,
        product_telemetry_destination=None,
    )


def _password_attempt(client: TestClient, email: str, password: str = "wrong"):
    return client.post(
        "/auth/password-login",
        json={"email": email, "password": password},
        headers={"Origin": "http://testserver"},
    )


def test_password_limits_hide_identity_and_survive_app_reconstruction(
    tmp_path: Path,
) -> None:
    sequences = []
    for index, email in enumerate(("owner@example.com", "missing@example.com")):
        config = _config(tmp_path, database_name=f"password-{index}.db")
        app = _app(config)
        client = claim_server(app)
        client.cookies.clear()
        statuses = [_password_attempt(client, email).status_code for _ in range(10)]

        # Rebuilding the app over the same database must not reset the bucket.
        rebuilt = _app(config)
        statuses.append(_password_attempt(TestClient(rebuilt), email).status_code)
        sequences.append(statuses)

    assert sequences[0] == sequences[1] == [401] * 10 + [429]


def test_password_source_limit_bounds_email_spraying_and_success_clears_email_only(
    tmp_path: Path, monkeypatch
) -> None:
    def sign_in(_self, *, email: str, password: str):
        if password == "accepted":
            return SimpleNamespace(session="session-token")
        raise InvalidCredentials("invalid email or password")

    monkeypatch.setattr(IdentityAuthService, "sign_in_password", sign_in)
    app = _app(_config(tmp_path))
    client = claim_server(app)
    client.cookies.clear()

    assert all(
        _password_attempt(client, f"spray-{index}@example.test").status_code == 401
        for index in range(30)
    )
    assert _password_attempt(client, "one-more@example.test").status_code == 429

    # Use a distinct source by trusting the TestClient peer and forwarding a
    # concrete client address, then prove success resets only that email key.
    config = replace(
        _config(tmp_path, database_name="success.db"),
        trusted_proxy_cidrs=("10.0.0.0/8",),
    )
    success_app = _app(config)
    success_client = TestClient(success_app, client=("10.0.0.5", 50000))
    claim_server(success_app, client=success_client)
    success_client.cookies.clear()
    headers = {
        "Origin": "http://testserver",
        "X-Forwarded-For": "198.51.100.8",
    }
    body = {"email": "owner@example.com", "password": "wrong"}
    for _ in range(9):
        assert (
            success_client.post(
                "/auth/password-login", json=body, headers=headers
            ).status_code
            == 401
        )
    body["password"] = "accepted"
    assert (
        success_client.post(
            "/auth/password-login", json=body, headers=headers
        ).status_code
        == 200
    )
    body["password"] = "wrong"
    assert (
        success_client.post(
            "/auth/password-login", json=body, headers=headers
        ).status_code
        == 401
    )

    with success_app.state.control_engine.connect() as cx:
        counts = dict(
            cx.execute(
                sa.select(
                    auth_attempt_buckets.c.scope,
                    auth_attempt_buckets.c.attempt_count,
                )
            ).all()
        )
    assert counts == {"password_email": 1, "password_source": 11}


def test_magic_delivery_budget_is_atomic_across_apps_and_never_refunded(
    tmp_path: Path,
) -> None:
    deliveries: list[str] = []
    delivery_links: list[str] = []
    delivery_lock = threading.Lock()

    async def failed_mail(email: str, link: str) -> bool:
        with delivery_lock:
            deliveries.append(email)
            delivery_links.append(link)
        return False

    config = _config(tmp_path)
    first = _app(config, mail=failed_mail)
    claim_server(first)
    second = _app(config, mail=failed_mail)
    email = "person@example.test"

    initial = TestClient(first).post("/auth/request-link", json={"email": email})
    assert initial.status_code == 200
    initial_token = parse_qs(urlparse(delivery_links[0]).query)["token"][0]
    with first.state.control_engine.begin() as cx:
        assert (
            AuthChallengeStore(cx).consume_magic_link(
                initial_token, now=datetime.now(UTC)
            )
            == email
        )

    def request(index: int) -> int:
        app = first if index % 2 else second
        return (
            TestClient(app)
            .post("/auth/request-link", json={"email": email})
            .status_code
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=7) as pool:
        statuses = list(pool.map(request, range(7)))

    assert statuses == [200] * 7
    assert deliveries == [email] * 3
    with first.state.control_engine.connect() as cx:
        assert (
            cx.execute(
                sa.select(sa.func.count())
                .select_from(auth_challenges)
                .where(
                    auth_challenges.c.kind == MAGIC_LINK,
                    auth_challenges.c.subject_email == email,
                )
            ).scalar_one()
            == 3
        )


def test_oidc_source_limit_and_global_cap_are_atomic(
    tmp_path: Path, monkeypatch
) -> None:
    config = _config(tmp_path)
    first = _app(config)
    claim_server(first)

    statuses = [
        TestClient(first).get("/auth/oidc/work", follow_redirects=False).status_code
        for _ in range(21)
    ]
    assert statuses == [302] * 20 + [429]

    capped_config = _config(tmp_path, database_name="global.db")
    capped_first = _app(capped_config)
    claim_server(capped_first)
    capped_second = _app(capped_config)
    monkeypatch.setattr(browser_auth_routes, "_OIDC_MAX_ACTIVE_STATES", 3)

    def start(index: int) -> int:
        app = capped_first if index % 2 else capped_second
        return (
            TestClient(app).get("/auth/oidc/work", follow_redirects=False).status_code
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        capped_statuses = sorted(pool.map(start, range(6)))
    assert capped_statuses == [302, 302, 302, 429, 429, 429]
    with capped_first.state.control_engine.connect() as cx:
        assert (
            cx.execute(
                sa.select(sa.func.count())
                .select_from(auth_challenges)
                .where(
                    auth_challenges.c.kind == OIDC_SIGN_IN,
                    auth_challenges.c.consumed_at.is_(None),
                )
            ).scalar_one()
            == 3
        )
        assert (
            cx.execute(
                sa.select(auth_attempt_buckets.c.attempt_count).where(
                    auth_attempt_buckets.c.scope == "oidc_source"
                )
            ).scalar_one()
            == 6
        )


def test_oidc_start_removes_expired_and_browser_superseded_state(
    tmp_path: Path,
) -> None:
    app = _app(_config(tmp_path))
    claim_server(app)
    now = datetime.now(UTC)
    with app.state.control_engine.begin() as cx:
        expired_state = AuthChallengeStore(cx).issue_oidc_sign_in(
            provider="work",
            nonce="expired",
            expires_at=now - timedelta(seconds=1),
            now=now - timedelta(seconds=2),
        )
    client = TestClient(app)
    assert client.get("/auth/oidc/work", follow_redirects=False).status_code == 302
    assert client.get("/auth/oidc/work", follow_redirects=False).status_code == 302
    with app.state.control_engine.connect() as cx:
        rows = (
            cx.execute(
                sa.select(auth_challenges.c.secret_hash).where(
                    auth_challenges.c.kind == OIDC_SIGN_IN
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0] != hashlib.sha256(expired_state.encode()).hexdigest()


def _request(peer: str, forwarded: str | None = None) -> Request:
    headers = [] if forwarded is None else [(b"x-forwarded-for", forwarded.encode())]
    return Request({"type": "http", "client": (peer, 443), "headers": headers})


def test_forwarded_source_requires_explicit_trust_and_fails_closed() -> None:
    networks = trusted_proxy_networks(("10.0.0.0/8", "2001:db8::/32"))
    assert (
        trusted_client_ip(_request("192.0.2.10", "198.51.100.7"), networks)
        == "192.0.2.10"
    )
    assert (
        trusted_client_ip(_request("10.0.0.5", "198.51.100.7, 10.1.1.1"), networks)
        == "198.51.100.7"
    )
    assert trusted_client_ip(_request("10.0.0.5", "not-an-ip"), networks) == "10.0.0.5"


def test_unconfigured_forwarding_cannot_split_a_source_bucket() -> None:
    hashes = {
        auth_budget(
            "password_source",
            trusted_client_ip(_request("127.0.0.1", forwarded), ()),
            limit=30,
            window_seconds=300,
        ).key_hash
        for forwarded in ("198.51.100.7", "203.0.113.9")
    }
    assert len(hashes) == 1


def test_forwarded_source_rejects_ambiguous_or_empty_chains() -> None:
    networks = trusted_proxy_networks(("10.0.0.0/8",))
    for forwarded in (
        "198.51.100.7,",
        ",198.51.100.7",
        "198.51.100.7,,10.1.1.1",
    ):
        assert trusted_client_ip(_request("10.0.0.5", forwarded), networks) == (
            "10.0.0.5"
        )

    duplicate = Request(
        {
            "type": "http",
            "client": ("10.0.0.5", 443),
            "headers": [
                (b"x-forwarded-for", b"198.51.100.7"),
                (b"x-forwarded-for", b"not-an-ip"),
            ],
        }
    )
    assert trusted_client_ip(duplicate, networks) == "10.0.0.5"


def test_trusted_proxy_config_parses_cidrs_and_rejects_invalid_values(
    tmp_path: Path,
) -> None:
    config = replace(
        _config(tmp_path),
        trusted_proxy_cidrs=("10.0.0.1/8",),
    )
    assert config.trusted_proxy_cidrs == ("10.0.0.0/8",)
    try:
        replace(
            _config(tmp_path),
            trusted_proxy_cidrs=("not-a-cidr",),
        )
    except ValueError as exc:
        assert "invalid trusted proxy CIDR" in str(exc)
    else:
        raise AssertionError("invalid proxy CIDR did not fail startup")
