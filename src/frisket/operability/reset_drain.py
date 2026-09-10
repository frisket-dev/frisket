"""Read-only PC-9 reset-drain inspection for the public/base stores.

The deployment must already have stopped every producer and worker. This
module observes; it does not cancel, reconcile, migrate, or delete anything.
A downstream composition adds its own reservation/settlement component
around this base report after the public-wheel re-pin.
"""

from __future__ import annotations

import os
import sqlite3
import stat
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import quote

import sqlalchemy as sa

from frisket.execution.attempt import TERMINAL_ATTEMPT_STATES


RESET_DRAIN_REPORT_SCHEMA_VERSION = "frisket.reset_drain_report.v1"


def _empty_report() -> dict:
    return {
        "schema_version": RESET_DRAIN_REPORT_SCHEMA_VERSION,
        "scope": "base",
        "ready": False,
        "hit_count": 0,
        "queue": {
            "jobs": {"total": 0, "by_status": {}},
            "handler_authorities": 0,
        },
        "bundles": {
            "scanned": 0,
            "output_claims": 0,
            "effect_checkpoints": 0,
            "nonterminal_execution_attempts": 0,
            "items": [],
        },
        "errors": [],
    }


def configuration_error_report(*codes: str) -> dict:
    """Machine-readable refusal for a command missing required locators."""

    report = _empty_report()
    report["errors"] = [
        {"scope": "configuration", "code": code} for code in sorted(set(codes))
    ]
    return report


def _database_url(locator: str | Path) -> sa.engine.URL:
    raw = str(locator)
    if "://" not in raw:
        path = Path(raw).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError("run queue SQLite database does not exist")
        return _immutable_sqlite_url(path)
    url = sa.engine.make_url(raw)
    if url.get_backend_name() == "sqlite":
        database = url.database
        if not database or database == ":memory:":
            raise ValueError("reset drain requires a durable run queue")
        path = Path(database).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError("run queue SQLite database does not exist")
        return _immutable_sqlite_url(path)
    if url.drivername in {"postgresql", "postgresql+psycopg"}:
        return url.set(drivername="postgresql+psycopg")
    raise ValueError("reset drain supports SQLite or PostgreSQL run queues")


def _assert_frozen_sqlite(path: Path) -> None:
    path = path.resolve()
    if any(
        os.path.lexists(f"{path}{suffix}") for suffix in ("-journal", "-wal", "-shm")
    ):
        raise ValueError("SQLite database has live journal sidecars")


def _immutable_sqlite_url(path: Path) -> sa.engine.URL:
    _assert_frozen_sqlite(path)
    return sa.URL.create(
        "sqlite+pysqlite",
        database=f"file:{path}",
        query={"immutable": "1", "mode": "ro", "uri": "true"},
    )


def _queue_counts(locator: str | Path) -> tuple[dict[str, int], int]:
    engine = sa.create_engine(_database_url(locator), future=True)
    try:
        with engine.connect() as connection, connection.begin():
            if engine.dialect.name == "postgresql":
                connection.exec_driver_sql("SET TRANSACTION READ ONLY")
            status_rows = connection.execute(
                sa.text(
                    "SELECT status, COUNT(*) AS row_count "
                    "FROM jobs GROUP BY status ORDER BY status"
                )
            ).all()
            authorities = connection.execute(
                sa.text("SELECT COUNT(*) FROM job_handler_authorities")
            ).scalar_one()
    finally:
        engine.dispose()
    return (
        {str(status): int(count) for status, count in status_rows},
        int(authorities),
    )


def _read_only_bundle_connection(db_path: Path) -> sqlite3.Connection:
    _assert_frozen_sqlite(db_path)
    encoded = quote(str(db_path.resolve()), safe="/")
    connection = sqlite3.connect(f"file:{encoded}?mode=ro&immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("BEGIN")
    return connection


def _grouped_counts(
    connection: sqlite3.Connection,
    *,
    table: str,
    column: str,
    where: str = "",
    params: Iterable[str] = (),
) -> dict[str, int]:
    rows = connection.execute(
        f"SELECT {column}, COUNT(*) AS row_count FROM {table} "
        f"{where} GROUP BY {column} ORDER BY {column}",
        tuple(params),
    ).fetchall()
    return {
        "<null>" if row[column] is None else str(row[column]): int(row["row_count"])
        for row in rows
    }


def _inspect_bundle(db_path: Path, *, projects_root: Path) -> dict:
    connection = _read_only_bundle_connection(db_path)
    try:
        required_tables = {
            "output_column_claims",
            "effect_checkpoints",
            "execution_attempts",
        }
        present_tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        missing = sorted(required_tables - present_tables)
        if missing:
            raise ValueError("bundle is missing reset-drain authority tables")

        claims_by_status = _grouped_counts(
            connection,
            table="output_column_claims",
            column="status",
        )
        effects_by_state = _grouped_counts(
            connection,
            table="effect_checkpoints",
            column="state",
        )
        terminal_placeholders = ",".join("?" for _ in TERMINAL_ATTEMPT_STATES)
        attempts_by_state = _grouped_counts(
            connection,
            table="execution_attempts",
            column="state",
            where=(f"WHERE state IS NULL OR state NOT IN ({terminal_placeholders})"),
            params=TERMINAL_ATTEMPT_STATES,
        )
    finally:
        connection.close()

    return {
        "bundle": db_path.parent.relative_to(projects_root).as_posix(),
        "output_claims": {
            "total": sum(claims_by_status.values()),
            "by_status": claims_by_status,
        },
        "effect_checkpoints": {
            "total": sum(effects_by_state.values()),
            "by_state": effects_by_state,
        },
        "nonterminal_execution_attempts": {
            "total": sum(attempts_by_state.values()),
            "by_state": attempts_by_state,
        },
    }


def _discover_bundle_databases(root: Path) -> tuple[list[Path], list[str]]:
    """Enumerate every bundle directory and surface incomplete traversal."""

    databases: list[Path] = []
    failed_paths: list[str] = []

    def _record_walk_error(error: OSError) -> None:
        filename = Path(error.filename) if error.filename else root
        try:
            relative = filename.relative_to(root).as_posix()
        except ValueError:
            relative = "."
        failed_paths.append(relative)

    for current, directory_names, file_names in os.walk(
        root, topdown=True, onerror=_record_walk_error, followlinks=False
    ):
        directory_names.sort()
        current_path = Path(current)
        bundle_names = [name for name in directory_names if name.endswith(".frisket")]
        databases.extend(current_path / name / "project.db" for name in bundle_names)
        # ``os.walk(..., followlinks=False)`` silently declines to traverse a
        # directory symlink. A deployment may use one as a projects mount, so
        # treating it as an empty subtree could certify a hidden live bundle.
        # Direct ``*.frisket`` symlinks are still inspected through their
        # explicit ``project.db`` path; any other linked subtree blocks the
        # proof and must be supplied as an explicit projects root instead.
        failed_paths.extend(
            (current_path / name).relative_to(root).as_posix()
            for name in directory_names
            if name not in bundle_names and (current_path / name).is_symlink()
        )
        for name in file_names:
            candidate = current_path / name
            if not candidate.is_symlink():
                continue
            try:
                target_is_directory = stat.S_ISDIR(candidate.stat().st_mode)
            except OSError:
                # ``os.walk`` classifies a link whose target cannot be stated
                # as a file. It may still be a projects subtree behind an
                # unreadable mount, so omission cannot certify the epoch.
                target_is_directory = True
            if target_is_directory:
                failed_paths.append(candidate.relative_to(root).as_posix())
        directory_names[:] = [
            name for name in directory_names if not name.endswith(".frisket")
        ]
    return sorted(databases), sorted(set(failed_paths))


def inspect_base_reset_drain(
    *,
    queue_locator: str | Path,
    projects_root: str | Path,
) -> dict:
    """Return the base component of the versioned reset-drain report.

    A hit is any queue job (including terminal history), any durable handler
    authority regardless of its job's status, any output claim, any
    effect checkpoint, or any nonterminal execution attempt. Inspection
    errors also refuse readiness because an unreadable store cannot prove a
    drain.
    """

    report = _empty_report()
    try:
        jobs_by_status, authorities = _queue_counts(queue_locator)
    except (OSError, RuntimeError, ValueError, sa.exc.SQLAlchemyError):
        report["errors"].append({"scope": "queue", "code": "queue_inspection_failed"})
    else:
        report["queue"] = {
            "jobs": {
                "total": sum(jobs_by_status.values()),
                "by_status": jobs_by_status,
            },
            "handler_authorities": authorities,
        }

    try:
        root = Path(projects_root).expanduser().resolve()
    except (OSError, RuntimeError):
        root = Path(projects_root)
        root_is_directory = False
    else:
        root_is_directory = root.is_dir()
    if not root_is_directory:
        report["errors"].append({"scope": "bundles", "code": "projects_root_not_found"})
        bundle_databases: list[Path] = []
    else:
        bundle_databases, traversal_failures = _discover_bundle_databases(root)
        report["errors"].extend(
            {
                "scope": "bundles",
                "code": "bundle_traversal_failed",
                "path": path,
            }
            for path in traversal_failures
        )

    report["bundles"]["scanned"] = len(bundle_databases)
    for db_path in bundle_databases:
        try:
            item = _inspect_bundle(db_path, projects_root=root)
        except (OSError, RuntimeError, ValueError, sqlite3.Error):
            report["errors"].append(
                {
                    "scope": "bundle",
                    "code": "bundle_inspection_failed",
                    "bundle": db_path.parent.relative_to(root).as_posix(),
                }
            )
            continue
        report["bundles"]["items"].append(item)
        report["bundles"]["output_claims"] += item["output_claims"]["total"]
        report["bundles"]["effect_checkpoints"] += item["effect_checkpoints"]["total"]
        report["bundles"]["nonterminal_execution_attempts"] += item[
            "nonterminal_execution_attempts"
        ]["total"]

    report["hit_count"] = (
        report["queue"]["jobs"]["total"]
        + report["queue"]["handler_authorities"]
        + report["bundles"]["output_claims"]
        + report["bundles"]["effect_checkpoints"]
        + report["bundles"]["nonterminal_execution_attempts"]
    )
    report["ready"] = report["hit_count"] == 0 and not report["errors"]
    return report
