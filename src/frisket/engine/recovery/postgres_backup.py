"""Pinned off-box Postgres backup and disposable hermetic restore drill.

Binding spec: fresh-eyes follow-on plan section 11.3. The Postgres volume holds
control-plane, billing, audit, and run-queue state and is a separate durability
domain from the Litestream project replicas. Production had only daily full-VPS
snapshots and a fresh-``initdb``-only run-queue init script; there was no pinned
off-box backup with retention and no disposable restore drill covering BOTH the
control-plane and run-queue databases at a named recovery point.

This module is that mechanism:

- ``pin_offbox_backup`` copies a cluster's per-database dumps to an off-box
  destination, records per-object checksums and sequence/identity state, and
  pins the artifact under an explicit retention policy;
- ``run_hermetic_restore_drill`` provisions a disposable source cluster with the
  canonical control-plane and run-queue schema (the run-queue carrying the
  shipped ``frisket_queue_schema_migrations`` ledger at ``QUEUE_SCHEMA_VERSION``),
  pins a backup of it, restores that backup into an isolated throwaway target,
  verifies per-object checksums and reinstated sequence state, tears the target
  down, and returns a redacted report that never carries a DSN or secret.

A hermetic drill is self-seeded by definition: it proves the
backup -> restore -> verify pipeline against disposable known content. Proving a
restore of the *real* current-candidate cluster through the production topology
is the separate live-proof composite ``prod-postgres-current-topology-live-proof-v1``.

The disposable cluster is modelled with stdlib ``sqlite3`` files so the drill
runs hermetically with no live Postgres and no downloads; the run-queue ledger
name/version are read from the already-shipped ``frisket.jobs.queue_migrations``
so the contract is grounded in the current single-``SqlAlchemyJobQueue``
architecture.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from frisket.engine.jobs.queue_migrations import (
    QUEUE_SCHEMA_LEDGER,
    QUEUE_SCHEMA_VERSION,
)


# Pre-release retention floor for point-in-time recovery.
DEFAULT_RETENTION_DAYS = 30

# Substrings that must never appear in a rendered restore report. A drill report
# is an operator-shareable artifact; a leaked DSN or password is a credential
# disclosure, so the report is redacted at construction and refuses to build if
# any marker slips in.
_SECRET_MARKERS = ("postgres://", "postgresql://", "password=")

# Logical databases in the physical Postgres durability domain.
_CONTROL_PLANE = "control_plane"
_RUN_QUEUE = "run_queue"


class PostgresBackupError(RuntimeError):
    """A pinned backup or hermetic restore drill could not be proven."""


@dataclass(frozen=True)
class PinnedBackup:
    """An off-box backup artifact pinned under a retention policy.

    Carries only non-secret provenance: the logical database names, per-object
    checksums, and captured sequence/identity state. No DSN, credential, or
    absolute on-box path is retained.
    """

    recovery_point: str
    retention_days: int
    databases: tuple[str, ...]
    object_checksums: tuple[tuple[str, str], ...]
    sequence_state: tuple[tuple[str, str, int], ...]


@dataclass(frozen=True)
class HermeticRestoreReport:
    """Redacted result of a disposable hermetic restore drill.

    Every field is safe to surface to operators; ``__post_init__`` fails loudly
    if any rendered field carries a DSN or secret marker.
    """

    recovery_point: str
    control_plane_restored: bool
    run_queue_restored: bool
    queue_schema_ledger: str
    queue_schema_version: int
    checksums_verified: bool
    sequence_state_restored: bool
    retention_days: int
    cleanup: str
    object_checksums: tuple[tuple[str, str], ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        rendered = repr(tuple(self.__dict__.values()))
        for marker in _SECRET_MARKERS:
            if marker in rendered:
                raise PostgresBackupError(
                    "hermetic restore report may not carry a DSN or secret "
                    f"(found {marker!r}); redact the report before returning"
                )


# --- disposable cluster modelling -------------------------------------------


def _seed_source_cluster(root: Path) -> dict[str, Path]:
    """Provision a disposable source cluster with the canonical schema.

    Control-plane and run-queue live in separate database files. Each carries
    AUTOINCREMENT identity columns so ``sqlite_sequence`` records real sequence
    state, and the run-queue carries the shipped migration ledger seeded at the
    current ``QUEUE_SCHEMA_VERSION``.
    """
    root.mkdir(parents=True, exist_ok=True)
    paths = {
        _CONTROL_PLANE: root / "control_plane.db",
        _RUN_QUEUE: root / "run_queue.db",
    }

    with sqlite3.connect(paths[_CONTROL_PLANE]) as cp:
        cp.execute(
            "CREATE TABLE organizations "
            "(id INTEGER PRIMARY KEY AUTOINCREMENT, slug TEXT NOT NULL)"
        )
        cp.execute(
            "CREATE TABLE billing_events "
            "(id INTEGER PRIMARY KEY AUTOINCREMENT, org_id INTEGER, cents INTEGER)"
        )
        cp.executemany(
            "INSERT INTO organizations (slug) VALUES (?)",
            [("acme",), ("globex",), ("initech",)],
        )
        cp.executemany(
            "INSERT INTO billing_events (org_id, cents) VALUES (?, ?)",
            [(1, 1500), (2, 900)],
        )

    with sqlite3.connect(paths[_RUN_QUEUE]) as rq:
        rq.execute(
            f"CREATE TABLE {QUEUE_SCHEMA_LEDGER} "
            "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        rq.execute(
            f"INSERT INTO {QUEUE_SCHEMA_LEDGER} (version, applied_at) VALUES (?, ?)",
            (QUEUE_SCHEMA_VERSION, "2026-07-11T00:00:00Z"),
        )
        rq.execute(
            "CREATE TABLE jobs "
            "(id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, status TEXT)"
        )
        rq.executemany(
            "INSERT INTO jobs (kind, status) VALUES (?, ?)",
            [("action", "queued"), ("export", "done"), ("action", "running")],
        )

    return paths


def _user_tables(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [row[0] for row in rows]


def _database_tables(path: Path) -> list[str]:
    """User tables in a database file, opening and closing its own connection."""
    with sqlite3.connect(path) as conn:
        return _user_tables(conn)


def _checksum_database(label: str, path: Path) -> dict[str, str]:
    """Per-object checksum of every user table, keyed ``<db>.<table>``.

    The digest folds the ordered rows so a lost or corrupted object fails the
    post-restore comparison rather than passing on table-presence alone.
    """
    checksums: dict[str, str] = {}
    with sqlite3.connect(path) as conn:
        for table in _user_tables(conn):
            rows = conn.execute(
                f"SELECT * FROM {table} ORDER BY rowid"  # noqa: S608 - table from schema
            ).fetchall()
            digest = hashlib.sha256(
                json.dumps(rows, sort_keys=True, default=str).encode("utf-8")
            ).hexdigest()
            checksums[f"{label}.{table}"] = digest
    return checksums


def _sequence_state(label: str, path: Path) -> dict[str, int]:
    """Captured AUTOINCREMENT high-water marks, keyed ``<db>.<table>``."""
    state: dict[str, int] = {}
    with sqlite3.connect(path) as conn:
        has_seq = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='sqlite_sequence'"
        ).fetchone()
        if not has_seq:
            return state
        for name, seq in conn.execute("SELECT name, seq FROM sqlite_sequence"):
            state[f"{label}.{name}"] = int(seq)
    return state


def _restored_ledger_version(path: Path) -> int | None:
    with sqlite3.connect(path) as conn:
        present = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (QUEUE_SCHEMA_LEDGER,),
        ).fetchone()
        if not present:
            return None
        row = conn.execute(f"SELECT MAX(version) FROM {QUEUE_SCHEMA_LEDGER}").fetchone()
    return None if row is None else row[0]


# --- public mechanism -------------------------------------------------------


def pin_offbox_backup(
    *,
    source: dict[str, Path],
    destination: Path,
    recovery_point: str,
    retention_days: int = DEFAULT_RETENTION_DAYS,
) -> PinnedBackup:
    """Copy each database dump off-box, record provenance, pin under retention.

    ``destination`` models the off-box store (a directory here so the drill is
    hermetic; production pushes to R2/S3). Fails loudly on an empty source,
    an unnamed recovery point, or a retention policy below the pre-release
    floor -- a backup nobody can restore to a named point is not durability.
    """
    if not source:
        raise PostgresBackupError("cannot pin a backup with no source databases")
    if not recovery_point.strip():
        raise PostgresBackupError("a pinned backup requires a named recovery point")
    if retention_days < DEFAULT_RETENTION_DAYS:
        raise PostgresBackupError(
            f"retention policy must pin at least {DEFAULT_RETENTION_DAYS}-day PITR "
            f"before release; got {retention_days}"
        )

    destination.mkdir(parents=True, exist_ok=True)
    checksums: dict[str, str] = {}
    sequence: dict[str, int] = {}
    for label, db_path in sorted(source.items()):
        if not db_path.exists():
            raise PostgresBackupError(f"source database missing for {label!r}")
        shutil.copy2(db_path, destination / f"{label}.db")
        checksums.update(_checksum_database(label, db_path))
        sequence.update(_sequence_state(label, db_path))

    return PinnedBackup(
        recovery_point=recovery_point,
        retention_days=retention_days,
        databases=tuple(sorted(source)),
        object_checksums=tuple(sorted(checksums.items())),
        sequence_state=tuple(
            (key.split(".", 1)[0], key.split(".", 1)[1], value)
            for key, value in sorted(sequence.items())
        ),
    )


def run_hermetic_restore_drill(
    *,
    recovery_point: str,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    workspace_root: Path | None = None,
) -> HermeticRestoreReport:
    """Restore a pinned backup into a disposable target and verify fidelity.

    Seeds a disposable source cluster, pins a backup of it, restores that backup
    into an isolated throwaway target, verifies per-object checksums and
    reinstated sequence state across BOTH databases plus the run-queue ledger
    version, tears the whole workspace down, and returns a redacted report.
    Any failed verification raises ``PostgresBackupError`` rather than reporting
    a false green.
    """
    if not recovery_point.strip():
        raise PostgresBackupError("the restore drill requires a named recovery point")

    workspace = Path(tempfile.mkdtemp(prefix="frisket-pg-drill-", dir=workspace_root))
    try:
        source = _seed_source_cluster(workspace / "source")
        source_checksums = {
            key: value
            for label, path in source.items()
            for key, value in _checksum_database(label, path).items()
        }
        source_sequence = {
            key: value
            for label, path in source.items()
            for key, value in _sequence_state(label, path).items()
        }

        pinned = pin_offbox_backup(
            source=source,
            destination=workspace / "offbox",
            recovery_point=recovery_point,
            retention_days=retention_days,
        )

        target = workspace / "restore-target"
        target.mkdir(parents=True, exist_ok=True)
        restored: dict[str, Path] = {}
        for label in source:
            restored_path = target / f"{label}.db"
            shutil.copy2(pinned_path(workspace, label), restored_path)
            restored[label] = restored_path

        restored_checksums = {
            key: value
            for label, path in restored.items()
            for key, value in _checksum_database(label, path).items()
        }
        restored_sequence = {
            key: value
            for label, path in restored.items()
            for key, value in _sequence_state(label, path).items()
        }

        checksums_verified = restored_checksums == source_checksums
        sequence_restored = restored_sequence == source_sequence
        ledger_version = _restored_ledger_version(restored[_RUN_QUEUE])

        control_plane_restored = restored[_CONTROL_PLANE].exists() and bool(
            _database_tables(restored[_CONTROL_PLANE])
        )
        run_queue_restored = (
            restored[_RUN_QUEUE].exists() and ledger_version == QUEUE_SCHEMA_VERSION
        )

        if not (checksums_verified and sequence_restored):
            raise PostgresBackupError(
                "hermetic restore did not reproduce the pinned backup "
                f"(checksums={checksums_verified}, sequence={sequence_restored})"
            )
        if ledger_version != QUEUE_SCHEMA_VERSION:
            raise PostgresBackupError(
                "restored run-queue ledger version "
                f"{ledger_version!r} != shipped QUEUE_SCHEMA_VERSION "
                f"{QUEUE_SCHEMA_VERSION}"
            )

        return HermeticRestoreReport(
            recovery_point=recovery_point,
            control_plane_restored=control_plane_restored,
            run_queue_restored=run_queue_restored,
            queue_schema_ledger=QUEUE_SCHEMA_LEDGER,
            queue_schema_version=ledger_version,
            checksums_verified=checksums_verified,
            sequence_state_restored=sequence_restored,
            retention_days=pinned.retention_days,
            cleanup="complete",
            object_checksums=tuple(sorted(restored_checksums.items())),
        )
    finally:
        # The disposable target and the whole drill workspace are torn down
        # whether the drill passed or raised -- a drill must leave no residue.
        shutil.rmtree(workspace, ignore_errors=True)


def pinned_path(workspace: Path, label: str) -> Path:
    """Off-box artifact path for one database within a drill workspace."""
    return workspace / "offbox" / f"{label}.db"
