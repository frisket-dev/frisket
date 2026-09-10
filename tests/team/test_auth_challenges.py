from __future__ import annotations

import concurrent.futures
import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa

from frisket.team.auth_challenges import (
    CONNECTED_ACCOUNT_OAUTH,
    MAGIC_LINK,
    OIDC_SIGN_IN,
    AuthChallengeStore,
    TooManyActiveOidcSignIns,
)
from frisket.team.schema import auth_challenges, metadata, orgs, users


def _engine(tmp_path: Path) -> tuple[sa.Engine, int]:
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'control.db'}", future=True)
    metadata.create_all(engine)
    with engine.begin() as cx:
        org_id = int(
            cx.execute(orgs.insert().values(name="Desk")).inserted_primary_key[0]
        )
        user_id = int(
            cx.execute(
                users.insert().values(email="owner@example.test", default_org_id=org_id)
            ).inserted_primary_key[0]
        )
    return engine, user_id


def test_challenge_kinds_are_isolated_and_browser_secrets_are_hashed(
    tmp_path: Path,
) -> None:
    engine, user_id = _engine(tmp_path)
    now = datetime.now(UTC)
    expires_at = now + timedelta(minutes=10)
    with engine.begin() as cx:
        store = AuthChallengeStore(cx)
        magic = store.issue_magic_link(
            email="owner@example.test", expires_at=expires_at, now=now
        )
        oidc = store.issue_oidc_sign_in(
            provider="google", nonce="provider-nonce", expires_at=expires_at, now=now
        )
        connected = store.issue_connected_account_oauth(
            user_id=user_id, provider="google", expires_at=expires_at, now=now
        )

    with engine.connect() as cx:
        rows = {row.kind: row for row in cx.execute(sa.select(auth_challenges)).all()}
    assert set(rows) == {MAGIC_LINK, OIDC_SIGN_IN, CONNECTED_ACCOUNT_OAUTH}
    for kind, raw in (
        (MAGIC_LINK, magic),
        (OIDC_SIGN_IN, oidc),
        (CONNECTED_ACCOUNT_OAUTH, connected),
    ):
        assert rows[kind].secret_hash == hashlib.sha256(raw.encode()).hexdigest()
        assert raw not in repr(rows[kind]._mapping)
    assert rows[MAGIC_LINK].subject_email == "owner@example.test"
    assert rows[OIDC_SIGN_IN].provider == "google"
    assert rows[OIDC_SIGN_IN].nonce == "provider-nonce"
    assert rows[CONNECTED_ACCOUNT_OAUTH].subject_user_id == user_id

    with engine.begin() as cx:
        store = AuthChallengeStore(cx)
        # A digest leaked from the database is not itself a redeemable secret.
        assert store.consume_magic_link(rows[MAGIC_LINK].secret_hash, now=now) is None
        # Cross-kind and wrong-subject/provider attempts neither match nor spend.
        assert store.consume_magic_link(oidc, now=now) is None
        assert store.consume_oidc_sign_in(connected, provider="google", now=now) is None
        assert not store.consume_connected_account_oauth(
            magic, user_id=user_id, provider="google", now=now
        )
        assert store.consume_oidc_sign_in(oidc, provider="github", now=now) is None
        assert not store.consume_connected_account_oauth(
            connected, user_id=user_id + 1, provider="google", now=now
        )
        assert store.consume_magic_link(magic, now=now) == "owner@example.test"
        assert (
            store.consume_oidc_sign_in(oidc, provider="google", now=now)
            == "provider-nonce"
        )
        assert store.consume_connected_account_oauth(
            connected, user_id=user_id, provider="google", now=now
        )


def test_concurrent_challenge_consumption_has_one_winner(tmp_path: Path) -> None:
    engine, _user_id = _engine(tmp_path)
    now = datetime.now(UTC)
    with engine.begin() as cx:
        token = AuthChallengeStore(cx).issue_magic_link(
            email="owner@example.test",
            expires_at=now + timedelta(minutes=10),
            now=now,
        )

    def consume() -> str | None:
        with engine.begin() as cx:
            return AuthChallengeStore(cx).consume_magic_link(token, now=now)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _index: consume(), range(2)))
    assert outcomes.count("owner@example.test") == 1
    assert outcomes.count(None) == 1


def test_oidc_issuance_replaces_matching_browser_state_before_global_cap(
    tmp_path: Path,
) -> None:
    engine, _user_id = _engine(tmp_path)
    now = datetime.now(UTC)
    expires_at = now + timedelta(minutes=10)
    with engine.begin() as cx:
        store = AuthChallengeStore(cx)
        first = store.issue_oidc_sign_in(
            provider="google", nonce="first", expires_at=expires_at, now=now
        )
        other = store.issue_oidc_sign_in(
            provider="github", nonce="other", expires_at=expires_at, now=now
        )
        replacement = store.issue_oidc_sign_in(
            provider="google",
            nonce="replacement",
            expires_at=expires_at,
            now=now,
            max_active=2,
            replace_secret=first,
        )
        with pytest.raises(TooManyActiveOidcSignIns):
            store.issue_oidc_sign_in(
                provider="google",
                nonce="over-cap",
                expires_at=expires_at,
                now=now,
                max_active=2,
            )
        assert store.consume_oidc_sign_in(first, provider="google", now=now) is None
        assert (
            store.consume_oidc_sign_in(replacement, provider="google", now=now)
            == "replacement"
        )
        assert store.consume_oidc_sign_in(other, provider="github", now=now) == "other"


@pytest.mark.parametrize("issuer", [OIDC_SIGN_IN, CONNECTED_ACCOUNT_OAUTH])
def test_each_non_magic_issuer_purges_expired_challenges(
    tmp_path: Path, issuer: str
) -> None:
    engine, user_id = _engine(tmp_path)
    issued_at = datetime.now(UTC)
    expires_at = issued_at + timedelta(minutes=1)
    with engine.begin() as cx:
        store = AuthChallengeStore(cx)
        consumed = store.issue_magic_link(
            email="consumed@example.test",
            expires_at=expires_at,
            now=issued_at,
        )
        pending = store.issue_magic_link(
            email="pending@example.test",
            expires_at=expires_at,
            now=issued_at,
        )
        assert store.consume_magic_link(consumed, now=issued_at) is not None

    later = expires_at + timedelta(seconds=1)
    with engine.begin() as cx:
        store = AuthChallengeStore(cx)
        if issuer == OIDC_SIGN_IN:
            store.issue_oidc_sign_in(
                provider="google",
                nonce="next-nonce",
                expires_at=later + timedelta(minutes=10),
                now=later,
            )
        else:
            store.issue_connected_account_oauth(
                user_id=user_id,
                provider="google",
                expires_at=later + timedelta(minutes=10),
                now=later,
            )
    with engine.connect() as cx:
        remaining = set(cx.execute(sa.select(auth_challenges.c.secret_hash)).scalars())
    assert hashlib.sha256(consumed.encode()).hexdigest() not in remaining
    assert hashlib.sha256(pending.encode()).hexdigest() not in remaining
