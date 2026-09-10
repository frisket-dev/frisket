"""Provisioning for the dedicated Postgres run queue.

Two environment-only principals own this contract:

* ``FRISKET_DATABASE_ADMIN_URL`` — the maintenance-database owner that runs
  ``frisket queue provision`` and ``frisket queue migrate``.
* ``FRISKET_RUN_QUEUE_DATABASE_URL`` — the runtime locator that the app and
  worker use; it names the fixed ``frisket_run_queue_runtime`` login role.
"""

from __future__ import annotations

from dataclasses import dataclass

import psycopg
from psycopg import sql
from sqlalchemy.engine import URL, make_url


RUN_QUEUE_DATABASE_NAME = "frisket_run_queue"
RUN_QUEUE_MAINTENANCE_DATABASE_NAME = "postgres"
RUN_QUEUE_RUNTIME_ROLE = "frisket_run_queue_runtime"
QUEUE_DATABASE_PROVISION_LOCK_ID = 7_154_819_250_847_113_101


class QueueProvisionError(RuntimeError):
    """A sanitized run-queue provisioning contract failure."""


@dataclass(frozen=True)
class _PostgresEndpoint:
    host: str
    port: int


def validate_queue_database_url(raw_url: str) -> URL:
    """Return a parsed fixed-name queue URL without connecting."""
    if not raw_url:
        raise QueueProvisionError("run-queue database locator is required")
    try:
        url = make_url(raw_url)
        port = url.port or 5432
    except (TypeError, ValueError) as exc:
        raise QueueProvisionError("run-queue database locator is invalid") from exc
    if url.drivername not in {"postgresql", "postgresql+psycopg"}:
        raise QueueProvisionError("run-queue database locator must use Postgres")
    if url.database != RUN_QUEUE_DATABASE_NAME:
        raise QueueProvisionError(
            f"run-queue database must be named {RUN_QUEUE_DATABASE_NAME}"
        )
    if not url.host or port <= 0:
        raise QueueProvisionError("run-queue database locator has no valid server")
    return url.set(drivername="postgresql+psycopg")


def validate_queue_role_url(raw_url: str, *, required_role: str) -> URL:
    url = validate_queue_database_url(raw_url)
    if url.username != required_role:
        raise QueueProvisionError(
            f"run-queue locator must use the fixed {required_role} role"
        )
    if not url.password:
        raise QueueProvisionError(f"run-queue {required_role} password is required")
    return url


def _validate_admin_database_url(raw_url: str) -> URL:
    if not raw_url:
        raise QueueProvisionError("database admin locator is required")
    try:
        url = make_url(raw_url)
        port = url.port or 5432
    except (TypeError, ValueError) as exc:
        raise QueueProvisionError("database admin locator is invalid") from exc
    if url.drivername not in {"postgresql", "postgresql+psycopg"}:
        raise QueueProvisionError("database admin locator must use Postgres")
    if not url.host or not url.database or port <= 0:
        raise QueueProvisionError("database admin locator is incomplete")
    if url.database != RUN_QUEUE_MAINTENANCE_DATABASE_NAME:
        raise QueueProvisionError(
            "database admin locator must use the fixed "
            f"{RUN_QUEUE_MAINTENANCE_DATABASE_NAME} maintenance DB"
        )
    return url.set(drivername="postgresql+psycopg")


def _endpoint(url: URL) -> _PostgresEndpoint:
    return _PostgresEndpoint(host=(url.host or "").lower(), port=url.port or 5432)


def _psycopg_dsn(url: URL) -> str:
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


def _ensure_login_role(cursor: psycopg.Cursor, *, role: str, password: str) -> None:
    cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
    action = "ALTER ROLE" if cursor.fetchone() is not None else "CREATE ROLE"
    # The password is a bound SQL literal, never interpolated into CLI output.
    cursor.execute(
        sql.SQL(f"{action} {{}} WITH LOGIN PASSWORD {{}}").format(
            sql.Identifier(role), sql.Literal(password)
        )
    )


def provision_run_queue_database(*, admin_url: str, runtime_url: str) -> None:
    """Create the fixed queue database and runtime role under one advisory lock."""
    admin = _validate_admin_database_url(admin_url)
    runtime = validate_queue_role_url(runtime_url, required_role=RUN_QUEUE_RUNTIME_ROLE)
    if _endpoint(admin) != _endpoint(runtime):
        raise QueueProvisionError(
            "database admin and run-queue locators identify different servers"
        )

    with psycopg.connect(_psycopg_dsn(admin), autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_lock(%s)",
                (QUEUE_DATABASE_PROVISION_LOCK_ID,),
            )
            try:
                _ensure_login_role(
                    cursor,
                    role=RUN_QUEUE_RUNTIME_ROLE,
                    password=str(runtime.password),
                )
                cursor.execute(
                    "SELECT 1 FROM pg_database WHERE datname = %s",
                    (RUN_QUEUE_DATABASE_NAME,),
                )
                if cursor.fetchone() is None:
                    cursor.execute(
                        sql.SQL("CREATE DATABASE {}").format(
                            sql.Identifier(RUN_QUEUE_DATABASE_NAME)
                        )
                    )
                cursor.execute(
                    sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                        sql.Identifier(RUN_QUEUE_DATABASE_NAME),
                        sql.Identifier(RUN_QUEUE_RUNTIME_ROLE),
                    )
                )
            finally:
                cursor.execute(
                    "SELECT pg_advisory_unlock(%s)",
                    (QUEUE_DATABASE_PROVISION_LOCK_ID,),
                )
