"""Host-owned rate limiting for queued external-service work."""

from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any


RATE_LIMIT_DB_NAME = ".plugin_rate_limits.db"


@dataclass(frozen=True)
class RateLimitPolicy:
    max_concurrency: int = 1
    min_interval_ms: int = 0
    burst: int = 1
    retry_after_cap_ms: int = 60_000
    max_attempts: int = 1
    lease_ms: int = 120_000


def sqlite_rate_limit_db(workspace_root: str | Path) -> Path:
    return Path(workspace_root) / RATE_LIMIT_DB_NAME


class SystemClock:
    def now_ms(self) -> int:
        return int(time.monotonic() * 1000)


class SystemSleeper:
    def sleep_ms(self, ms: int) -> None:
        if ms > 0:
            time.sleep(ms / 1000)


class RateLimitCancelled(RuntimeError):
    """Raised when a queued caller cancels while waiting for a rate lease."""


class SqliteRateLimiter:
    """A small cross-worker limiter backed by a shared SQLite database."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        clock: Any | None = None,
        sleeper: Any | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.clock = clock or SystemClock()
        self.sleeper = sleeper or SystemSleeper()
        self._held_lock = threading.Lock()
        self._held_leases: dict[str, list[str]] = {}
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS plugin_rate_limits (
                    rate_key TEXT PRIMARY KEY,
                    next_available_ms INTEGER NOT NULL DEFAULT 0,
                    active INTEGER NOT NULL DEFAULT 0,
                    tokens INTEGER NOT NULL DEFAULT 0,
                    last_refill_ms INTEGER NOT NULL DEFAULT 0,
                    updated_at_ms INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS plugin_rate_limit_leases (
                    lease_id TEXT PRIMARY KEY,
                    rate_key TEXT NOT NULL,
                    acquired_at_ms INTEGER NOT NULL,
                    expires_at_ms INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_plugin_rate_limit_leases_key "
                "ON plugin_rate_limit_leases(rate_key, expires_at_ms)"
            )

    @contextmanager
    def lease(
        self,
        rate_key: str,
        policy: RateLimitPolicy,
        *,
        sleeper: Callable[[int], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> Iterator[int]:
        start_ms = self.acquire(
            rate_key,
            policy,
            sleeper=sleeper,
            should_cancel=should_cancel,
        )
        try:
            yield start_ms
        finally:
            self.release(rate_key)

    def acquire(
        self,
        rate_key: str,
        policy: RateLimitPolicy,
        *,
        sleeper: Callable[[int], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> int:
        sleep = sleeper or self.sleeper.sleep_ms
        while True:
            _raise_if_cancelled(should_cancel)
            with self._connect() as conn:
                now_ms = int(self.clock.now_ms())
                conn.execute("BEGIN IMMEDIATE")
                active = self._recover_expired(conn, rate_key, now_ms)
                row = conn.execute(
                    "SELECT * FROM plugin_rate_limits WHERE rate_key=?",
                    (rate_key,),
                ).fetchone()
                if row is None:
                    lease_id = _new_lease_id()
                    tokens = max(0, policy.burst - 1)
                    last_refill_ms = now_ms
                    next_available_ms = now_ms + policy.min_interval_ms
                    conn.execute(
                        "INSERT INTO plugin_rate_limits "
                        "(rate_key, next_available_ms, active, tokens, "
                        "last_refill_ms, updated_at_ms) VALUES (?, ?, 1, ?, ?, ?)",
                        (
                            rate_key,
                            next_available_ms,
                            tokens,
                            last_refill_ms,
                            now_ms,
                        ),
                    )
                    self._insert_lease(conn, rate_key, lease_id, now_ms, policy)
                    conn.commit()
                    self._remember_lease(rate_key, lease_id)
                    return now_ms

                tokens = max(0, int(row["tokens"]))
                last_refill_ms = max(0, int(row["last_refill_ms"]))
                next_available_ms = max(0, int(row["next_available_ms"]))
                if policy.burst > 1 and policy.min_interval_ms > 0:
                    elapsed = max(0, now_ms - last_refill_ms)
                    refill = elapsed // policy.min_interval_ms
                    if refill:
                        tokens = min(policy.burst, tokens + int(refill))
                        last_refill_ms += int(refill) * policy.min_interval_ms

                if active >= policy.max_concurrency:
                    conn.rollback()
                    _sleep_with_cancel(
                        sleep,
                        max(1, min(policy.min_interval_ms or 100, 100)),
                        should_cancel,
                    )
                    continue

                if policy.burst > 1 and tokens > 0:
                    lease_id = _new_lease_id()
                    tokens -= 1
                    conn.execute(
                        "UPDATE plugin_rate_limits SET active=?, tokens=?, "
                        "last_refill_ms=?, updated_at_ms=? WHERE rate_key=?",
                        (active + 1, tokens, last_refill_ms, now_ms, rate_key),
                    )
                    self._insert_lease(conn, rate_key, lease_id, now_ms, policy)
                    conn.commit()
                    self._remember_lease(rate_key, lease_id)
                    return now_ms

                wait_ms = max(0, next_available_ms - now_ms)
                if wait_ms > 0:
                    conn.rollback()
                    _sleep_with_cancel(sleep, wait_ms, should_cancel)
                    continue

                lease_id = _new_lease_id()
                conn.execute(
                    "UPDATE plugin_rate_limits SET active=?, next_available_ms=?, "
                    "tokens=?, last_refill_ms=?, updated_at_ms=? WHERE rate_key=?",
                    (
                        active + 1,
                        now_ms + policy.min_interval_ms,
                        tokens,
                        last_refill_ms or now_ms,
                        now_ms,
                        rate_key,
                    ),
                )
                self._insert_lease(conn, rate_key, lease_id, now_ms, policy)
                conn.commit()
                self._remember_lease(rate_key, lease_id)
                return now_ms

    def release(self, rate_key: str) -> None:
        lease_id = self._pop_lease(rate_key)
        with self._connect() as conn:
            now_ms = int(self.clock.now_ms())
            conn.execute("BEGIN IMMEDIATE")
            if lease_id is not None:
                conn.execute(
                    "DELETE FROM plugin_rate_limit_leases WHERE lease_id=?",
                    (lease_id,),
                )
            else:
                row = conn.execute(
                    "SELECT lease_id FROM plugin_rate_limit_leases "
                    "WHERE rate_key=? ORDER BY acquired_at_ms LIMIT 1",
                    (rate_key,),
                ).fetchone()
                if row is not None:
                    conn.execute(
                        "DELETE FROM plugin_rate_limit_leases WHERE lease_id=?",
                        (str(row["lease_id"]),),
                    )
            active = self._active_count(conn, rate_key)
            conn.execute(
                "UPDATE plugin_rate_limits SET active=?, updated_at_ms=? "
                "WHERE rate_key=?",
                (active, now_ms, rate_key),
            )
            conn.commit()

    def record_backoff(
        self,
        rate_key: str,
        *,
        retry_after_ms: int,
        policy: RateLimitPolicy,
    ) -> int:
        delay_ms = max(0, min(int(retry_after_ms), policy.retry_after_cap_ms))
        with self._connect() as conn:
            now_ms = int(self.clock.now_ms())
            until_ms = now_ms + delay_ms
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM plugin_rate_limits WHERE rate_key=?",
                (rate_key,),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO plugin_rate_limits "
                    "(rate_key, next_available_ms, active, tokens, last_refill_ms, "
                    "updated_at_ms) VALUES (?, ?, 0, 0, ?, ?)",
                    (rate_key, until_ms, now_ms, now_ms),
                )
            else:
                conn.execute(
                    "UPDATE plugin_rate_limits SET next_available_ms=?, tokens=0, "
                    "updated_at_ms=? WHERE rate_key=?",
                    (max(int(row["next_available_ms"]), until_ms), now_ms, rate_key),
                )
            conn.commit()
            return delay_ms

    def _recover_expired(
        self,
        conn: sqlite3.Connection,
        rate_key: str,
        now_ms: int,
    ) -> int:
        conn.execute(
            "DELETE FROM plugin_rate_limit_leases "
            "WHERE rate_key=? AND expires_at_ms<=?",
            (rate_key, now_ms),
        )
        active = self._active_count(conn, rate_key)
        conn.execute(
            "UPDATE plugin_rate_limits SET active=?, updated_at_ms=? WHERE rate_key=?",
            (active, now_ms, rate_key),
        )
        return active

    def _insert_lease(
        self,
        conn: sqlite3.Connection,
        rate_key: str,
        lease_id: str,
        now_ms: int,
        policy: RateLimitPolicy,
    ) -> None:
        conn.execute(
            "INSERT INTO plugin_rate_limit_leases "
            "(lease_id, rate_key, acquired_at_ms, expires_at_ms) "
            "VALUES (?, ?, ?, ?)",
            (lease_id, rate_key, now_ms, now_ms + policy.lease_ms),
        )

    def _active_count(self, conn: sqlite3.Connection, rate_key: str) -> int:
        row = conn.execute(
            "SELECT COUNT(*) AS count FROM plugin_rate_limit_leases WHERE rate_key=?",
            (rate_key,),
        ).fetchone()
        return int(row["count"] if row is not None else 0)

    def _remember_lease(self, rate_key: str, lease_id: str) -> None:
        with self._held_lock:
            self._held_leases.setdefault(rate_key, []).append(lease_id)

    def _pop_lease(self, rate_key: str) -> str | None:
        with self._held_lock:
            leases = self._held_leases.get(rate_key)
            if not leases:
                return None
            lease_id = leases.pop()
            if not leases:
                self._held_leases.pop(rate_key, None)
            return lease_id


def _raise_if_cancelled(should_cancel: Callable[[], bool] | None) -> None:
    if should_cancel is not None and should_cancel():
        raise RateLimitCancelled("queued rate-limited work was cancelled")


def _sleep_with_cancel(
    sleep: Callable[[int], None],
    ms: int,
    should_cancel: Callable[[], bool] | None,
) -> None:
    if ms <= 0:
        return
    if should_cancel is None:
        sleep(ms)
        return
    remaining = int(ms)
    while remaining > 0:
        _raise_if_cancelled(should_cancel)
        chunk = min(remaining, 100)
        sleep(chunk)
        remaining -= chunk
        _raise_if_cancelled(should_cancel)


def _new_lease_id() -> str:
    return f"lease:{uuid.uuid4().hex}"
