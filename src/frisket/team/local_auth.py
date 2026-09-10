"""Local owner credentials and first-owner setup operations.

The operations deliberately accept an existing SQLAlchemy engine so HTTP and a
future operator CLI share exactly the same transaction and password rules.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from frisket.team.db import locked_transaction
from frisket.team.schema import (
    PASSWORD_RESET_PREFIX,
    audit_log,
    memberships,
    orgs,
    password_resets,
    sessions,
    users,
)

_HASHER = PasswordHasher()
_DUMMY_HASH = _HASHER.hash("frisket-local-login-dummy")


class SetupError(ValueError):
    pass


class SetupUnavailable(SetupError):
    pass


class InvalidCredentials(SetupError):
    pass


def hash_password(password: str) -> str:
    if len(password) < 12:
        raise SetupError("password must be at least 12 characters")
    if len(password) > 1024:
        raise SetupError("password is too long")
    return _HASHER.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    # Always invoke Argon2, including for an unknown/non-local identity.
    candidate = password_hash or _DUMMY_HASH
    try:
        return bool(password_hash) and _HASHER.verify(candidate, password)
    except (VerificationError, InvalidHashError):
        return False


def _identity_fields(
    *, workspace_name: str, owner_name: str, email: str
) -> tuple[str, str, str]:
    workspace = workspace_name.strip()
    name = owner_name.strip()
    clean_email = email.lower().strip()
    local, separator, domain = clean_email.partition("@")
    if (
        not workspace
        or len(workspace) > 200
        or not name
        or len(name) > 200
        or not separator
        or not local
        or not domain
        or "@" in domain
        or any(character.isspace() for character in clean_email)
        or len(clean_email) > 320
    ):
        raise SetupError("workspace name, owner name, and a valid email are required")
    return workspace, name, clean_email


def owner_count(engine: sa.Engine, *, org_id: int) -> int:
    with engine.connect() as cx:
        return int(
            cx.execute(
                sa.select(sa.func.count())
                .select_from(memberships)
                .where(
                    memberships.c.org_id == org_id,
                    memberships.c.role == "owner",
                )
            ).scalar_one()
        )


def claim_first_owner(
    engine: sa.Engine,
    *,
    org_id: int,
    claim_token: str,
    expected_claim_token: str | None,
    workspace_name: str,
    owner_name: str,
    email: str,
    password: str,
) -> dict[str, Any]:
    if (
        not expected_claim_token
        or len(claim_token) > 512
        or not secrets.compare_digest(claim_token, expected_claim_token)
    ):
        raise InvalidCredentials("invalid setup code")
    workspace, name, clean_email = _identity_fields(
        workspace_name=workspace_name,
        owner_name=owner_name,
        email=email,
    )
    password_hash = hash_password(password)
    with locked_transaction(engine, lock_scope=("first-owner", org_id)) as cx:
        count = int(
            cx.execute(
                sa.select(sa.func.count())
                .select_from(memberships)
                .where(
                    memberships.c.org_id == org_id,
                    memberships.c.role == "owner",
                )
            ).scalar_one()
        )
        if count:
            raise SetupUnavailable("setup is no longer available")
        existing = cx.execute(
            sa.select(users.c.id).where(users.c.email == clean_email)
        ).scalar_one_or_none()
        if existing is not None:
            raise SetupError("owner email is already in use")
        user_id = int(
            cx.execute(
                users.insert().values(
                    email=clean_email,
                    name=name,
                    password_hash=password_hash,
                    default_org_id=org_id,
                )
            ).inserted_primary_key[0]
        )
        cx.execute(
            memberships.insert().values(
                user_id=user_id,
                org_id=org_id,
                role="owner",
            )
        )
        cx.execute(
            orgs.update()
            .where(orgs.c.id == org_id)
            .values(name=workspace, display_name=workspace)
        )
        now = datetime.now(UTC)
        session = secrets.token_urlsafe(32)
        cx.execute(
            sessions.insert().values(
                token=session,
                user_id=user_id,
                created_at=now,
                expires_at=now + timedelta(days=30),
            )
        )
        cx.execute(
            audit_log.insert().values(
                user_id=user_id,
                org_id=org_id,
                action="first_owner_claimed",
                detail=clean_email,
            )
        )
    return {
        "id": user_id,
        "email": clean_email,
        "name": name,
        "org_id": org_id,
        "session": session,
    }


def authenticate_local_password(
    engine: sa.Engine, *, email: str, password: str
) -> dict[str, Any]:
    clean_email = email.lower().strip()
    lookup_email = clean_email if len(clean_email) <= 320 else "invalid-local-login"
    bounded_password = password if len(password) <= 1024 else ""
    with engine.begin() as cx:
        row = cx.execute(sa.select(users).where(users.c.email == lookup_email)).first()
        verified = verify_password(
            None if row is None else row.password_hash,
            bounded_password,
        )
        if row is None or not verified or len(password) > 1024:
            raise InvalidCredentials("invalid email or password")
        org_id = cx.execute(
            sa.select(memberships.c.org_id)
            .where(memberships.c.user_id == row.id)
            .order_by(memberships.c.org_id)
            .limit(1)
        ).scalar_one_or_none()
        if org_id is None:
            raise InvalidCredentials("invalid email or password")
        now = datetime.now(UTC)
        session = secrets.token_urlsafe(32)
        cx.execute(
            sessions.insert().values(
                token=session,
                user_id=row.id,
                created_at=now,
                expires_at=now + timedelta(days=30),
            )
        )
        cx.execute(
            audit_log.insert().values(
                user_id=row.id,
                org_id=org_id,
                action="local_password_login",
            )
        )
    return {
        "id": int(row.id),
        "email": str(row.email),
        "name": row.name,
        "org_id": int(org_id),
        "session": session,
    }


PASSWORD_RESET_TTL_MINUTES = 60


class ResetTokenInvalid(SetupError):
    """The reset token is unknown, expired, or already used."""


def create_password_reset(
    engine: sa.Engine,
    *,
    org_id: int,
    email: str,
    actor_user_id: int,
    now: datetime | None = None,
) -> str:
    """Mint a single-use password-reset token for an existing account.

    Follows the project-invite hash-at-rest pattern: the raw token is
    returned exactly once and only its sha256 digest is stored. Older
    outstanding resets for the same user are consumed so exactly one link
    is live per account.
    """
    clean_email = email.lower().strip()
    stamp = now or datetime.now(UTC)
    raw = PASSWORD_RESET_PREFIX + secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw.encode()).hexdigest()
    with locked_transaction(engine, lock_scope=("password-reset", clean_email)) as cx:
        user_id = cx.execute(
            sa.select(users.c.id).where(users.c.email == clean_email)
        ).scalar_one_or_none()
        if user_id is None:
            raise SetupError("no account exists for that email")
        cx.execute(
            password_resets.update()
            .where(
                password_resets.c.user_id == user_id,
                password_resets.c.used_at.is_(None),
            )
            .values(used_at=stamp)
        )
        cx.execute(
            password_resets.insert().values(
                user_id=user_id,
                token_hash=token_hash,
                created_at=stamp,
                expires_at=stamp + timedelta(minutes=PASSWORD_RESET_TTL_MINUTES),
            )
        )
        cx.execute(
            audit_log.insert().values(
                user_id=actor_user_id,
                org_id=org_id,
                action="password_reset_link_created",
                detail=clean_email,
            )
        )
    return raw


def _live_reset_row(cx: sa.Connection, token: str, *, now: datetime) -> Any | None:
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    row = cx.execute(
        sa.select(password_resets, users.c.email)
        .join(users, users.c.id == password_resets.c.user_id)
        .where(password_resets.c.token_hash == token_hash)
    ).first()
    if row is None or row.used_at is not None:
        return None
    expires = row.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    return None if expires < now else row


def password_reset_is_live(engine: sa.Engine, token: str) -> bool:
    with engine.connect() as cx:
        return _live_reset_row(cx, token, now=datetime.now(UTC)) is not None


def redeem_password_reset(
    engine: sa.Engine, *, org_id: int, token: str, password: str
) -> str:
    """Set a new password through a live reset token; returns the email.

    Single-use: the token is consumed in the same transaction that writes the
    hash, and every existing session for the account is revoked.
    """
    password_hash = hash_password(password)
    now = datetime.now(UTC)
    with locked_transaction(engine, lock_scope=("password-reset-redeem",)) as cx:
        row = _live_reset_row(cx, token, now=now)
        if row is None:
            raise ResetTokenInvalid("reset link is invalid, expired, or already used")
        consumed = cx.execute(
            password_resets.update()
            .where(
                password_resets.c.id == row.id,
                password_resets.c.used_at.is_(None),
            )
            .values(used_at=now)
        )
        if consumed.rowcount != 1:
            raise ResetTokenInvalid("reset link is invalid, expired, or already used")
        cx.execute(
            users.update()
            .where(users.c.id == row.user_id)
            .values(password_hash=password_hash)
        )
        cx.execute(sessions.delete().where(sessions.c.user_id == row.user_id))
        cx.execute(
            audit_log.insert().values(
                user_id=row.user_id,
                org_id=org_id,
                action="password_reset_completed",
                detail=str(row.email),
            )
        )
    return str(row.email)


def reset_sole_owner_password(engine: sa.Engine, *, password: str) -> str:
    password_hash = hash_password(password)
    with locked_transaction(engine, lock_scope=("sole-owner-reset",)) as cx:
        rows = cx.execute(
            sa.select(users.c.id, users.c.email, memberships.c.org_id)
            .join(memberships, memberships.c.user_id == users.c.id)
            .where(memberships.c.role == "owner")
        ).all()
        if len(rows) != 1:
            raise SetupUnavailable("password reset requires exactly one owner")
        row = rows[0]
        cx.execute(
            users.update()
            .where(users.c.id == row.id)
            .values(password_hash=password_hash)
        )
        cx.execute(sessions.delete().where(sessions.c.user_id == row.id))
        cx.execute(
            audit_log.insert().values(
                user_id=row.id,
                org_id=row.org_id,
                action="sole_owner_password_reset",
            )
        )
    return str(row.email)
