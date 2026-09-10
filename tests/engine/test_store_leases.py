"""Unit tests for the shared claim and lease helpers.

Two column-pair leases used to check expiry differently: embeddings
parsed datetimes; sheet.refresh compared raw ISO strings
(``expires > now.isoformat()``). The string compare is only correct while
every writer uses the exact same format: SQLite's ``datetime('now')``
("YYYY-MM-DD HH:MM:SS", space separator) sorts lexicographically BEFORE any
"T"-separated isoformat of the same instant, so a live lease stored in that
format would read as expired — a stuck-forever/double-run seam.
store/leases.py owns the PARSED, format-tolerant comparison.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from frisket.engine.store.leases import (
    acquire_lease,
    lease_expired,
    lease_expiry,
    lease_held,
    new_lease_token,
    release_lease,
)

NOW = datetime(2026, 7, 7, 10, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------- expiry


def test_lease_expired_parses_isoformat() -> None:
    future = (NOW + timedelta(hours=1)).isoformat(timespec="microseconds")
    past = (NOW - timedelta(hours=1)).isoformat(timespec="microseconds")
    assert lease_expired(future, NOW) is False
    assert lease_expired(past, NOW) is True
    # boundary: exactly-now is expired (embeddings semantic: parsed <= now)
    assert lease_expired(NOW.isoformat(), NOW) is True


def test_lease_expired_is_format_tolerant_iso_skew_edge() -> None:
    # THE drift edge: SQLite datetime('now')-style space-separated UTC value.
    # A raw string compare against now.isoformat() calls this LIVE lease
    # expired (" " < "T"), enabling a concurrent double-run. The parsed
    # comparison must see it as held.
    sqlite_style_future = "2026-07-07 12:00:00"  # naive, space separator
    assert sqlite_style_future <= NOW.isoformat()  # proves the string compare lies
    assert lease_expired(sqlite_style_future, NOW) is False
    # and the reverse skew: an expired space-separated value stays expired
    assert lease_expired("2026-07-07 09:00:00", NOW) is True
    # Zulu-suffixed and offset forms parse too
    assert lease_expired("2026-07-07T12:00:00Z", NOW) is False
    assert lease_expired("2026-07-07T12:00:00+02:00", NOW) is True  # == 10:00Z


def test_lease_expired_treats_garbage_and_missing_as_expired() -> None:
    assert lease_expired(None, NOW) is True
    assert lease_expired("", NOW) is True
    assert lease_expired("not-a-date", NOW) is True


def test_lease_held_requires_token_and_unexpired() -> None:
    future = (NOW + timedelta(minutes=5)).isoformat()
    assert lease_held("tok", future, NOW) is True
    assert lease_held(None, future, NOW) is False
    assert lease_held("tok", None, NOW) is False
    assert lease_held("tok", (NOW - timedelta(minutes=5)).isoformat(), NOW) is False
    # the skew edge again, through the held-check the executor uses
    assert lease_held("tok", "2026-07-07 12:00:00", NOW) is True


def test_lease_expiry_duration_semantics_preserved() -> None:
    # 3600s default lives at the call sites; here: the formatter clamps to
    # >=1s and emits microsecond-precision isoformat (the embeddings format).
    value = lease_expiry(NOW, 3600)
    parsed = datetime.fromisoformat(value)
    assert parsed - NOW == timedelta(seconds=3600)
    assert lease_expiry(NOW, 0) == lease_expiry(NOW, 1)  # max(1, ...) clamp


def test_new_lease_token_is_prefixed_and_unique() -> None:
    a = new_lease_token("embrefresh")
    b = new_lease_token("embrefresh")
    assert a.startswith("embrefresh:") and b.startswith("embrefresh:")
    assert a != b


# ---------------------------------------------------------------- acquire/release


def _db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute(
        "CREATE TABLE things ("
        "id TEXT PRIMARY KEY, status TEXT, "
        "refresh_claim_token TEXT, refresh_lease_expires_at TEXT)"
    )
    db.execute("INSERT INTO things (id, status) VALUES ('t1', 'ready')")
    db.commit()
    return db


def test_acquire_release_round_trip() -> None:
    db = _db()
    token = acquire_lease(
        db,
        table="things",
        id_value="t1",
        token_prefix="test",
        lease_seconds=60,
        acquire_set={"status": "refreshing"},
    )
    assert token is not None and token.startswith("test:")
    row = db.execute("SELECT * FROM things WHERE id='t1'").fetchone()
    assert row["refresh_claim_token"] == token
    assert row["status"] == "refreshing"
    assert (
        lease_expired(row["refresh_lease_expires_at"], datetime.now(timezone.utc))
        is False
    )

    # a second acquire while held fails
    assert acquire_lease(db, table="things", id_value="t1", token_prefix="test") is None

    release_lease(
        db,
        table="things",
        id_value="t1",
        token=token,
        release_set={"status": "ready"},
    )
    row = db.execute("SELECT * FROM things WHERE id='t1'").fetchone()
    assert row["refresh_claim_token"] is None
    assert row["refresh_lease_expires_at"] is None
    assert row["status"] == "ready"


def test_acquire_reclaims_expired_lease_but_not_live_one() -> None:
    db = _db()
    db.execute(
        "UPDATE things SET refresh_claim_token='crashed', "
        "refresh_lease_expires_at=? WHERE id='t1'",
        ((datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),),
    )
    db.commit()
    token = acquire_lease(db, table="things", id_value="t1", token_prefix="test")
    assert token is not None  # crashed (expired) lease is reclaimable


def test_acquire_respects_held_requires_predicate() -> None:
    # embeddings semantic: held only while status='refreshing'; a token left
    # behind in another status is not a live hold.
    db = _db()
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    db.execute(
        "UPDATE things SET status='ready', refresh_claim_token='stale', "
        "refresh_lease_expires_at=? WHERE id='t1'",
        (future,),
    )
    db.commit()
    token = acquire_lease(
        db,
        table="things",
        id_value="t1",
        token_prefix="test",
        held_requires=lambda row: row["status"] == "refreshing",
    )
    assert token is not None


def test_release_with_wrong_token_is_a_noop() -> None:
    db = _db()
    token = acquire_lease(db, table="things", id_value="t1", token_prefix="test")
    assert token is not None
    release_lease(db, table="things", id_value="t1", token="someone-else")
    row = db.execute("SELECT * FROM things WHERE id='t1'").fetchone()
    assert row["refresh_claim_token"] == token  # still held


def test_acquire_missing_row_returns_none() -> None:
    db = _db()
    assert (
        acquire_lease(db, table="things", id_value="nope", token_prefix="test") is None
    )
