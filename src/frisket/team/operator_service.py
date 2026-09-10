"""Operator-token authority for remote server administration.

An operator token is minted on the server host (`frisket-control token mint`),
paired with a local CLI (`frisket remote link`), and presented as
``Authorization: Bearer frisket_operator_<rand>`` on the admin API. It is a
deployment credential, not a person: authentication is a constant-time digest
comparison against the stored sha256 hashes, entirely separate from the
session/PAT channels, and it carries org-owner power over admin routes only.

Audit rows need an ``actor`` user id, so operator actions are attributed to a
synthetic reserved identity (`operator@frisket.invalid`). That identity
deliberately has NO membership row: giving it one would count it as an org
owner everywhere (`owner_count`, the setup gate, last-owner guards, sole-owner
password recovery), which would close /setup on a fresh install the moment the
installer mints the initial token. Callers that require owner membership must
therefore accept the operator channel explicitly (see
``InviteService.create_org_invite``'s ``operator_actor``).
"""

from __future__ import annotations

import hashlib
import os
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import sqlalchemy as sa

from frisket.team.db import locked_transaction
from frisket.team.schema import (
    OPERATOR_TOKEN_PREFIX,
    audit_log,
    operator_tokens,
    users,
)

OPERATOR_ACTOR_EMAIL = "operator@frisket.invalid"
OPERATOR_ACTOR_NAME = "Operator token"


class OperatorTokenError(ValueError):
    """A mint/rotate request that cannot be honored."""


def resolve_control_database_url(env: dict[str, str] | None = None) -> str | None:
    """The server's own control-plane locator, resolved the way the on-box
    consoles resolve it (`frisket owner`): the explicit
    FRISKET_TEAM_DATABASE_URL wins, else a standalone data dir's
    ``server.sqlite3`` when one exists. ``None`` means no server database was
    found on this host."""
    values = os.environ if env is None else env
    database_url = (values.get("FRISKET_TEAM_DATABASE_URL") or "").strip()
    if database_url:
        return database_url
    data_dir = Path(values.get("FRISKET_DATA_DIR", "/data")).expanduser().resolve()
    standalone_database = data_dir / "server.sqlite3"
    if standalone_database.is_file():
        return f"sqlite:///{standalone_database}"
    return None


def _digest(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _ensure_operator_actor(cx: sa.Connection) -> int:
    existing = cx.execute(
        sa.select(users.c.id).where(users.c.email == OPERATOR_ACTOR_EMAIL)
    ).scalar_one_or_none()
    if existing is not None:
        return int(existing)
    return int(
        cx.execute(
            users.insert().values(email=OPERATOR_ACTOR_EMAIL, name=OPERATOR_ACTOR_NAME)
        ).inserted_primary_key[0]
    )


def _mint_in_transaction(
    cx: sa.Connection, *, org_id: int, label: str | None
) -> tuple[str, int]:
    raw = OPERATOR_TOKEN_PREFIX + secrets.token_urlsafe(32)
    actor_id = _ensure_operator_actor(cx)
    token_id = int(
        cx.execute(
            operator_tokens.insert().values(
                org_id=org_id,
                label=(label or "").strip() or None,
                token_hash=_digest(raw),
                created_at=datetime.now(UTC),
            )
        ).inserted_primary_key[0]
    )
    cx.execute(
        audit_log.insert().values(
            user_id=actor_id,
            org_id=org_id,
            action="operator_token_minted",
            detail=f"{token_id}:{(label or '').strip()}",
        )
    )
    return raw, token_id


def mint_operator_token(
    engine: sa.Engine, *, org_id: int, label: str | None = None
) -> tuple[str, int]:
    """Mint a token, returning ``(raw, token_id)``. The raw value is never
    persisted; this return is the one chance to show it."""
    if label is not None and len(label) > 200:
        raise OperatorTokenError("label must be 200 characters or fewer")
    with locked_transaction(engine, lock_scope=("operator-token", org_id)) as cx:
        return _mint_in_transaction(cx, org_id=org_id, label=label)


class OperatorTokenService:
    """Bearer authentication and rotation over the ``operator_tokens`` table."""

    def __init__(self, engine: sa.Engine, *, org_id: int):
        self._engine = engine
        self._org_id = org_id

    def authenticate(self, authorization: str) -> dict[str, Any] | None:
        """Resolve ``Authorization: Bearer <raw>`` to an operator actor.

        Comparison is constant-time per candidate digest; a miss and a
        malformed header are indistinguishable to the caller (both None).
        """
        if not authorization.lower().startswith("bearer "):
            return None
        raw = authorization[7:].strip()
        if not raw.startswith(OPERATOR_TOKEN_PREFIX):
            return None
        digest = _digest(raw)
        with self._engine.connect() as cx:
            rows = cx.execute(
                sa.select(
                    operator_tokens.c.id,
                    operator_tokens.c.label,
                    operator_tokens.c.token_hash,
                ).where(
                    operator_tokens.c.org_id == self._org_id,
                    operator_tokens.c.revoked_at.is_(None),
                )
            ).all()
        match = None
        for row in rows:
            if secrets.compare_digest(str(row.token_hash), digest):
                match = row
        if match is None:
            return None
        with locked_transaction(
            self._engine, lock_scope=("operator-token", self._org_id)
        ) as cx:
            actor_id = _ensure_operator_actor(cx)
            cx.execute(
                operator_tokens.update()
                .where(operator_tokens.c.id == int(match.id))
                .values(last_used_at=datetime.now(UTC))
            )
        return {
            "id": actor_id,
            "email": OPERATOR_ACTOR_EMAIL,
            "name": OPERATOR_ACTOR_NAME,
            "org_id": self._org_id,
            "auth": "operator",
            "operator_token_id": int(match.id),
            "operator_label": match.label,
        }

    def rotate(self, *, token_id: int) -> tuple[str, str | None]:
        """Replace the authenticated token: mint a successor carrying the same
        label, tombstone the old one, return ``(raw, label)`` once."""
        with locked_transaction(
            self._engine, lock_scope=("operator-token", self._org_id)
        ) as cx:
            row = cx.execute(
                sa.select(operator_tokens.c.id, operator_tokens.c.label).where(
                    operator_tokens.c.id == token_id,
                    operator_tokens.c.org_id == self._org_id,
                    operator_tokens.c.revoked_at.is_(None),
                )
            ).first()
            if row is None:
                raise OperatorTokenError("operator token is no longer active")
            raw, new_id = _mint_in_transaction(cx, org_id=self._org_id, label=row.label)
            cx.execute(
                operator_tokens.update()
                .where(operator_tokens.c.id == token_id)
                .values(revoked_at=datetime.now(UTC))
            )
            actor_id = _ensure_operator_actor(cx)
            cx.execute(
                audit_log.insert().values(
                    user_id=actor_id,
                    org_id=self._org_id,
                    action="operator_token_rotated",
                    detail=f"{token_id}->{new_id}",
                )
            )
        return raw, row.label


__all__ = [
    "OPERATOR_ACTOR_EMAIL",
    "OperatorTokenError",
    "OperatorTokenService",
    "mint_operator_token",
    "resolve_control_database_url",
]
