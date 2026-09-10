"""Shared row claim/lease helpers.

Two column-pair leases (``embedding_indexes`` and ``sheets``, columns
``refresh_claim_token`` + ``refresh_lease_expires_at``) used to check expiry
differently: embeddings parsed datetimes, sheet.refresh compared raw ISO
strings. The string compare is correct only while every writer emits the
exact same format — SQLite's ``datetime('now')`` space-separated form sorts
before any "T"-separated isoformat of the same instant, turning a live lease
into an "expired" one (double-run) or vice versa (stuck-forever). This module
owns the PARSED, format-tolerant comparison and the acquire/release CAS.

Left alone by design (genuinely different models): OutputColumnClaimStore,
the plugin bootstrap claim, and the declarative IdempotencyPolicy.
"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

_TOKEN_COLUMN = "refresh_claim_token"
_EXPIRES_COLUMN = "refresh_lease_expires_at"


def new_lease_token(prefix: str) -> str:
    return f"{prefix}:{uuid.uuid4().hex}"


def lease_expiry(now: datetime, lease_seconds: int) -> str:
    """Expiry timestamp for a lease taken at ``now`` (clamped to >= 1s)."""
    expiry = now + timedelta(seconds=max(1, int(lease_seconds)))
    return expiry.isoformat(timespec="microseconds")


def lease_expired(value: Any, now: datetime) -> bool:
    """True when a stored expiry is missing, unparseable, or <= now.

    Comparison is on PARSED datetimes so it tolerates format skew between
    writers (isoformat with/without microseconds, space-separated SQLite
    datetimes, ``Z`` suffixes, naive values — naive is assumed UTC).
    """
    if not value:
        return True
    raw = str(value)
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed <= now


def lease_held(token: Any, expires_at: Any, now: datetime) -> bool:
    """True while a claim token exists and its lease has not expired."""
    return bool(token) and not lease_expired(expires_at, now)


def acquire_lease(
    db: sqlite3.Connection,
    *,
    table: str,
    id_value: Any,
    token_prefix: str,
    lease_seconds: int = 3600,
    id_column: str = "id",
    token_column: str = _TOKEN_COLUMN,
    expires_column: str = _EXPIRES_COLUMN,
    held_requires: Callable[[sqlite3.Row], bool] | None = None,
    acquire_set: Mapping[str, Any] | None = None,
    set_sql: tuple[str, ...] = (),
) -> str | None:
    """Compare-and-set a row's lease under one writer (BEGIN IMMEDIATE).

    Returns a claim token, or None when the row is missing or another live
    holder exists. An expired lease is a crashed holder and is reclaimable.
    ``held_requires`` narrows what counts as held (e.g. embeddings' lease is
    live only while ``status='refreshing'``); ``acquire_set`` adds bound
    column assignments to the claiming UPDATE (e.g. ``{"status":
    "refreshing"}``) and ``set_sql`` adds verbatim assignment fragments for
    SQL-expression columns (e.g. ``"updated_at=datetime('now')"``).
    """
    token = new_lease_token(token_prefix)
    now = datetime.now(timezone.utc)
    expiry = lease_expiry(now, lease_seconds)
    try:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            f"SELECT * FROM {table} WHERE {id_column}=?", (id_value,)
        ).fetchone()
        if row is None:
            db.rollback()
            return None
        held = lease_held(row[token_column], row[expires_column], now) and (
            held_requires(row) if held_requires is not None else True
        )
        if held:
            db.rollback()
            return None
        extra = dict(acquire_set or {})
        assignments = ", ".join(
            [f"{token_column}=?", f"{expires_column}=?"]
            + [f"{column}=?" for column in extra]
            + list(set_sql)
        )
        db.execute(
            f"UPDATE {table} SET {assignments} WHERE {id_column}=?",
            (token, expiry, *extra.values(), id_value),
        )
        db.commit()
        return token
    except Exception:
        db.rollback()
        raise


def release_lease(
    db: sqlite3.Connection,
    *,
    table: str,
    id_value: Any,
    token: str,
    id_column: str = "id",
    token_column: str = _TOKEN_COLUMN,
    expires_column: str = _EXPIRES_COLUMN,
    release_set: Mapping[str, Any] | None = None,
    set_sql: tuple[str, ...] = (),
) -> None:
    """Clear a lease if (and only if) ``token`` still holds it."""
    extra = dict(release_set or {})
    assignments = ", ".join(
        [f"{token_column}=NULL", f"{expires_column}=NULL"]
        + [f"{column}=?" for column in extra]
        + list(set_sql)
    )
    db.execute(
        f"UPDATE {table} SET {assignments} WHERE {id_column}=? AND {token_column}=?",
        (*extra.values(), id_value, token),
    )
    db.commit()
