"""Typed one-time browser challenge persistence for Team authentication."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime

import sqlalchemy as sa

from frisket.team.schema import auth_challenges

MAGIC_LINK = "magic_link"
OIDC_SIGN_IN = "oidc_sign_in"
CONNECTED_ACCOUNT_OAUTH = "connected_account_oauth"


class TooManyActiveMagicLinks(RuntimeError):
    pass


class TooManyActiveOidcSignIns(RuntimeError):
    pass


@dataclass(frozen=True)
class ConsumedAuthChallenge:
    subject_email: str | None
    subject_user_id: int | None
    provider: str | None
    nonce: str | None


def _secret_hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


class AuthChallengeStore:
    """The only SQL authority for the three existing browser challenges."""

    def __init__(self, cx: sa.Connection):
        self.cx = cx

    def issue_magic_link(
        self,
        *,
        email: str,
        expires_at: datetime,
        now: datetime,
        max_active: int | None = None,
    ) -> str:
        self._purge_expired(now)
        if max_active is not None:
            active = self.cx.execute(
                sa.select(sa.func.count())
                .select_from(auth_challenges)
                .where(
                    auth_challenges.c.kind == MAGIC_LINK,
                    auth_challenges.c.subject_email == email,
                    auth_challenges.c.consumed_at.is_(None),
                    auth_challenges.c.expires_at >= now,
                )
            ).scalar_one()
            if active >= max_active:
                raise TooManyActiveMagicLinks
        return self._issue(
            kind=MAGIC_LINK,
            expires_at=expires_at,
            subject_email=email,
        )

    def issue_oidc_sign_in(
        self,
        *,
        provider: str,
        nonce: str,
        expires_at: datetime,
        now: datetime,
        max_active: int | None = None,
        replace_secret: str | None = None,
    ) -> str:
        self._purge_expired(now)
        if replace_secret is not None:
            self.cx.execute(
                auth_challenges.delete().where(
                    auth_challenges.c.secret_hash == _secret_hash(replace_secret),
                    auth_challenges.c.kind == OIDC_SIGN_IN,
                    auth_challenges.c.provider == provider,
                )
            )
        if max_active is not None:
            active = self.cx.execute(
                sa.select(sa.func.count())
                .select_from(auth_challenges)
                .where(
                    auth_challenges.c.kind == OIDC_SIGN_IN,
                    auth_challenges.c.consumed_at.is_(None),
                    auth_challenges.c.expires_at >= now,
                )
            ).scalar_one()
            if active >= max_active:
                raise TooManyActiveOidcSignIns
        return self._issue(
            kind=OIDC_SIGN_IN,
            expires_at=expires_at,
            provider=provider,
            nonce=nonce,
            random_bytes=24,
        )

    def issue_connected_account_oauth(
        self,
        *,
        user_id: int,
        provider: str,
        expires_at: datetime,
        now: datetime,
    ) -> str:
        self._purge_expired(now)
        return self._issue(
            kind=CONNECTED_ACCOUNT_OAUTH,
            expires_at=expires_at,
            subject_user_id=user_id,
            provider=provider,
            random_bytes=24,
        )

    def magic_link_email(self, secret: str) -> str | None:
        """Return the target even for an expired/consumed magic link."""
        return self.cx.execute(
            sa.select(auth_challenges.c.subject_email).where(
                auth_challenges.c.secret_hash == _secret_hash(secret),
                auth_challenges.c.kind == MAGIC_LINK,
            )
        ).scalar_one_or_none()

    def pending_magic_link_email(self, secret: str, *, now: datetime) -> str | None:
        return self.cx.execute(
            sa.select(auth_challenges.c.subject_email).where(
                auth_challenges.c.secret_hash == _secret_hash(secret),
                auth_challenges.c.kind == MAGIC_LINK,
                auth_challenges.c.consumed_at.is_(None),
                auth_challenges.c.expires_at >= now,
            )
        ).scalar_one_or_none()

    def consume_magic_link(self, secret: str, *, now: datetime) -> str | None:
        challenge = self._consume(secret, kind=MAGIC_LINK, now=now)
        return None if challenge is None else challenge.subject_email

    def consume_oidc_sign_in(
        self, secret: str, *, provider: str, now: datetime
    ) -> str | None:
        challenge = self._consume(
            secret,
            kind=OIDC_SIGN_IN,
            now=now,
            provider=provider,
        )
        return None if challenge is None else challenge.nonce

    def consume_connected_account_oauth(
        self,
        secret: str,
        *,
        user_id: int,
        provider: str,
        now: datetime,
    ) -> bool:
        return (
            self._consume(
                secret,
                kind=CONNECTED_ACCOUNT_OAUTH,
                now=now,
                subject_user_id=user_id,
                provider=provider,
            )
            is not None
        )

    def _issue(
        self,
        *,
        kind: str,
        expires_at: datetime,
        subject_email: str | None = None,
        subject_user_id: int | None = None,
        provider: str | None = None,
        nonce: str | None = None,
        random_bytes: int = 32,
    ) -> str:
        secret = secrets.token_urlsafe(random_bytes)
        self.cx.execute(
            auth_challenges.insert().values(
                secret_hash=_secret_hash(secret),
                kind=kind,
                subject_email=subject_email,
                subject_user_id=subject_user_id,
                provider=provider,
                nonce=nonce,
                expires_at=expires_at,
            )
        )
        return secret

    def _consume(
        self,
        secret: str,
        *,
        kind: str,
        now: datetime,
        subject_user_id: int | None = None,
        provider: str | None = None,
    ) -> ConsumedAuthChallenge | None:
        conditions = [
            auth_challenges.c.secret_hash == _secret_hash(secret),
            auth_challenges.c.kind == kind,
            auth_challenges.c.consumed_at.is_(None),
            auth_challenges.c.expires_at >= now,
        ]
        if subject_user_id is not None:
            conditions.append(auth_challenges.c.subject_user_id == subject_user_id)
        if provider is not None:
            conditions.append(auth_challenges.c.provider == provider)
        row = self.cx.execute(
            auth_challenges.update()
            .where(*conditions)
            .values(consumed_at=now)
            .returning(
                auth_challenges.c.subject_email,
                auth_challenges.c.subject_user_id,
                auth_challenges.c.provider,
                auth_challenges.c.nonce,
            )
        ).first()
        if row is None:
            return None
        return ConsumedAuthChallenge(
            subject_email=row.subject_email,
            subject_user_id=row.subject_user_id,
            provider=row.provider,
            nonce=row.nonce,
        )

    def _purge_expired(self, now: datetime) -> None:
        self.cx.execute(
            auth_challenges.delete().where(auth_challenges.c.expires_at < now)
        )
