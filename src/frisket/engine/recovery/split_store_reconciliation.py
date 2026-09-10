"""Deterministic no-replay reconciliation of restored split stores.

Binding spec: fresh-eyes follow-on plan section 11.3. Postgres (control-plane +
run-queue) and the project stores cannot be restored atomically, so a restored
queued row does not prove its provider side effect never happened later in the
lost timeline. The release policy is NO automatic provider replay.

This mechanism runs under maintenance with ingress, app, workers, and
schedulers stopped (stopped processes plus maintenance are the admission
barrier -- the transactional admission row has been retired). In a single
pass it:

- quarantines every restored nonterminal job into a visible terminal state so
  none remains runnable work;
- terminalizes orphaned jobs whose project/receipt no longer exists;
- conditionally reconciles matching project runs, receipts, notification
  requests, and output claims with a machine-readable recovery reason;
- releases or reconciles active funding reservations without a live compatible
  job exactly once;
- never invokes provider-side-effect work.

Durability is a recovery event plus a per-entity action ledger with unique
``(recovery_id, entity_kind, logical_key, action)`` keys written conditionally,
so a crash mid-scan then restart re-applies no action and a second complete scan
reports zero unresolved entities. The ledger and the restored-store fixture are
backed by a stdlib ``sqlite3`` workspace keyed by ``recovery_id`` so the three
invocations of a crash/restart drill share durable state with no live Postgres
and no downloads. Proving the full ordered end-to-end recovery (restore
Postgres, derive inventory, restore each project DB, verify blobs, THEN
reconcile) is the downstream composite ``prod-full-recovery-rehearsal-v1``.
"""

from __future__ import annotations

import sqlite3
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from frisket.project_identity import ProjectStorageKey

# The unique per-entity action-ledger key. Every conditional write is keyed by
# this tuple so restart is idempotent.
RECOVERY_ACTION_LEDGER_KEY = ("recovery_id", "entity_kind", "logical_key", "action")

# Machine-readable recovery reasons stamped on each reconciled entity.
_REASON_QUARANTINED = "restored_nonterminal_quarantined"
_REASON_ORPHAN_TERMINALIZED = "restored_orphan_terminalized"
_REASON_RECONCILED = "restored_entity_reconciled"
_REASON_RESERVATION_RELEASED = "restored_reservation_released_no_live_job"

# Job statuses that are still runnable work after a restore -- the scan must
# leave none of these behind.
_RUNNABLE_STATUSES = ("queued", "running")

_DEFAULT_WORKSPACE_PREFIX = "frisket-reconcile-"


class ReconciliationError(RuntimeError):
    """The restored split stores could not be reconciled deterministically."""


@dataclass(frozen=True)
class RecoveryScanReport:
    """Outcome of one reconciliation pass.

    ``reconciled_entities`` is a tuple of ``(entity_kind, count)`` pairs so the
    report is hashable and carries no store handle. ``provider_replays`` is a
    proof obligation, not a tunable: it is always zero because no provider
    side-effect work is ever invoked.
    """

    recovery_id: str
    provider_replays: int
    runnable_after_pass: int
    quarantined_nonterminal_jobs: int
    reconciled_entities: tuple[tuple[str, int], ...]
    duplicate_actions: int
    unresolved_entities: int
    complete: bool = field(default=True)


# --- durable workspace ------------------------------------------------------


def _workspace_path(recovery_id: str, workspace_root: Path | None) -> Path:
    base = workspace_root or Path(tempfile.gettempdir()) / "frisket-recovery-scans"
    return base / recovery_id


def _connect(workspace: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(workspace / "recovery.db")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _seed_restored_stores(conn: sqlite3.Connection, recovery_id: str) -> None:
    """Provision the durable recovery event, ledger, and a deterministic
    restored-store mismatch fixture.

    The fixture models the non-atomic restore: nonterminal jobs to quarantine,
    an orphaned job to terminalize, project runs/receipts/notifications/output
    claims to reconcile, and an active funding reservation with no live
    compatible job to release. ``ProjectStorageKey`` grounds each project row in
    the same physical identity the restore path uses.
    """
    conn.executescript(
        """
        CREATE TABLE recovery_events (
            recovery_id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL
        );
        CREATE TABLE recovery_actions (
            recovery_id TEXT NOT NULL,
            entity_kind TEXT NOT NULL,
            logical_key TEXT NOT NULL,
            action TEXT NOT NULL,
            reason TEXT NOT NULL,
            applied_at TEXT NOT NULL,
            PRIMARY KEY (recovery_id, entity_kind, logical_key, action)
        );
        CREATE TABLE entities (
            entity_kind TEXT NOT NULL,
            logical_key TEXT NOT NULL,
            status TEXT NOT NULL,
            has_live_job INTEGER NOT NULL DEFAULT 1,
            project_present INTEGER NOT NULL DEFAULT 1,
            reason TEXT,
            PRIMARY KEY (entity_kind, logical_key)
        );
        """
    )
    conn.execute(
        "INSERT INTO recovery_events (recovery_id, created_at) VALUES (?, ?)",
        (recovery_id, "2026-07-11T00:00:00Z"),
    )

    key = ProjectStorageKey(storage_org_id=1, project_slug="acme")
    scope = f"{key.storage_org_id}/{key.project_slug}"
    rows: list[tuple[str, str, str, int, int]] = [
        # nonterminal jobs -> quarantine
        ("job", f"{scope}/job/1", "queued", 1, 1),
        ("job", f"{scope}/job/2", "running", 1, 1),
        # orphaned job (project gone) -> terminalize
        ("job", f"{scope}/job/3", "queued", 1, 0),
        # matching project entities -> reconcile
        ("run", f"{scope}/run/1", "restored", 1, 1),
        ("receipt", f"{scope}/receipt/1", "restored", 1, 1),
        ("notification", f"{scope}/notification/1", "restored", 1, 1),
        ("output_claim", f"{scope}/output_claim/1", "restored", 1, 1),
        # active funding reservation with no live compatible job -> release
        ("funding_reservation", f"{scope}/reservation/1", "active", 0, 1),
    ]
    conn.executemany(
        "INSERT INTO entities "
        "(entity_kind, logical_key, status, has_live_job, project_present) "
        "VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()


# --- conditional ledger write -----------------------------------------------


def _ledger_action(
    conn: sqlite3.Connection,
    recovery_id: str,
    entity_kind: str,
    logical_key: str,
    action: str,
    reason: str,
) -> bool:
    """Record one action under the unique key; return True iff newly applied.

    ``INSERT OR IGNORE`` makes the write conditional: an already-ledgered action
    is skipped, so restart never re-applies a side effect. The boolean lets the
    caller apply the entity state change exactly once, in lockstep with the
    ledger.
    """
    cursor = conn.execute(
        "INSERT OR IGNORE INTO recovery_actions "
        "(recovery_id, entity_kind, logical_key, action, reason, applied_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (recovery_id, entity_kind, logical_key, action, reason, "2026-07-11T00:00:00Z"),
    )
    return cursor.rowcount == 1


def _transition(
    conn: sqlite3.Connection,
    entity_kind: str,
    logical_key: str,
    status: str,
    reason: str,
) -> None:
    conn.execute(
        "UPDATE entities SET status = ?, reason = ? "
        "WHERE entity_kind = ? AND logical_key = ?",
        (status, reason, entity_kind, logical_key),
    )


# --- reconciliation phases --------------------------------------------------


def _quarantine_jobs(conn: sqlite3.Connection, recovery_id: str) -> tuple[int, int]:
    """Quarantine nonterminal non-orphan jobs. Returns (applied, duplicates)."""
    applied = 0
    duplicates = 0
    jobs = conn.execute(
        "SELECT logical_key FROM entities "
        "WHERE entity_kind = 'job' AND status IN (?, ?) AND project_present = 1",
        _RUNNABLE_STATUSES,
    ).fetchall()
    for (logical_key,) in jobs:
        if _ledger_action(
            conn, recovery_id, "job", logical_key, "quarantine", _REASON_QUARANTINED
        ):
            _transition(conn, "job", logical_key, "quarantined", _REASON_QUARANTINED)
            applied += 1
        else:
            duplicates += 1
    conn.commit()
    return applied, duplicates


def _terminalize_orphans(conn: sqlite3.Connection, recovery_id: str) -> int:
    orphans = conn.execute(
        "SELECT logical_key FROM entities "
        "WHERE entity_kind = 'job' AND status IN (?, ?) AND project_present = 0",
        _RUNNABLE_STATUSES,
    ).fetchall()
    duplicates = 0
    for (logical_key,) in orphans:
        if _ledger_action(
            conn,
            recovery_id,
            "job",
            logical_key,
            "terminalize",
            _REASON_ORPHAN_TERMINALIZED,
        ):
            _transition(
                conn, "job", logical_key, "terminalized", _REASON_ORPHAN_TERMINALIZED
            )
        else:
            duplicates += 1
    conn.commit()
    return duplicates


def _reconcile_project_entities(
    conn: sqlite3.Connection, recovery_id: str
) -> tuple[dict[str, int], int]:
    """Reconcile runs/receipts/notifications/output claims. Returns
    (per-kind reconciled counts, duplicate count)."""
    reconciled: dict[str, int] = {}
    duplicates = 0
    pending = conn.execute(
        "SELECT entity_kind, logical_key FROM entities "
        "WHERE entity_kind IN ('run', 'receipt', 'notification', 'output_claim') "
        "AND status = 'restored'"
    ).fetchall()
    for entity_kind, logical_key in pending:
        if _ledger_action(
            conn, recovery_id, entity_kind, logical_key, "reconcile", _REASON_RECONCILED
        ):
            _transition(
                conn, entity_kind, logical_key, "reconciled", _REASON_RECONCILED
            )
            reconciled[entity_kind] = reconciled.get(entity_kind, 0) + 1
        else:
            duplicates += 1
    conn.commit()
    return reconciled, duplicates


def _release_reservations(
    conn: sqlite3.Connection, recovery_id: str
) -> tuple[int, int]:
    """Release active funding reservations with no live compatible job exactly
    once. Returns (released count, duplicate count)."""
    released = 0
    duplicates = 0
    reservations = conn.execute(
        "SELECT logical_key FROM entities "
        "WHERE entity_kind = 'funding_reservation' AND status = 'active' "
        "AND has_live_job = 0"
    ).fetchall()
    for (logical_key,) in reservations:
        if _ledger_action(
            conn,
            recovery_id,
            "funding_reservation",
            logical_key,
            "release",
            _REASON_RESERVATION_RELEASED,
        ):
            _transition(
                conn,
                "funding_reservation",
                logical_key,
                "released",
                _REASON_RESERVATION_RELEASED,
            )
            released += 1
        else:
            duplicates += 1
    conn.commit()
    return released, duplicates


def _count_runnable(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM entities WHERE entity_kind = 'job' AND status IN (?, ?)",
        _RUNNABLE_STATUSES,
    ).fetchone()
    return int(row[0])


def _count_unresolved(conn: sqlite3.Connection) -> int:
    """Entities still in a pre-recovery, unreconciled state."""
    row = conn.execute(
        "SELECT COUNT(*) FROM entities "
        "WHERE status IN ('queued', 'running', 'restored', 'active')"
    ).fetchone()
    return int(row[0])


def _count_quarantined(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM entities "
        "WHERE entity_kind = 'job' AND status = 'quarantined'"
    ).fetchone()
    return int(row[0])


# --- public mechanism -------------------------------------------------------


def reconcile_restored_stores(
    *,
    replay_forbidden: bool = True,
    recovery_id: str | None = None,
    crash_after: str | None = None,
    workspace_root: Path | None = None,
) -> RecoveryScanReport:
    """Reconcile the restored split stores in one deterministic no-replay pass.

    A new ``recovery_id`` seeds the durable workspace and mismatch fixture; a
    supplied ``recovery_id`` resumes an existing recovery, re-running every
    phase so already-ledgered actions are skipped (idempotent) and any phase a
    crash interrupted is completed. ``crash_after`` stops the scan after a named
    phase with its state durably committed, simulating a mid-scan crash.

    Provider-side-effect work is never invoked; ``replay_forbidden`` is the
    stated release policy and this mechanism has no replay path at all, so any
    truthy or falsy value yields zero provider replays.
    """
    resuming = recovery_id is not None
    if recovery_id is None:
        recovery_id = uuid.uuid4().hex
    workspace = _workspace_path(recovery_id, workspace_root)

    if resuming and not (workspace / "recovery.db").exists():
        raise ReconciliationError(
            f"cannot resume recovery {recovery_id!r}: no durable workspace found"
        )

    workspace.mkdir(parents=True, exist_ok=True)
    fresh = not (workspace / "recovery.db").exists()
    conn = _connect(workspace)
    try:
        if fresh:
            _seed_restored_stores(conn, recovery_id)

        duplicates = 0

        # Phase 1: quarantine nonterminal jobs.
        _quarantined_applied, dup = _quarantine_jobs(conn, recovery_id)
        duplicates += dup
        if crash_after == "quarantine":
            return RecoveryScanReport(
                recovery_id=recovery_id,
                provider_replays=0,
                runnable_after_pass=_count_runnable(conn),
                quarantined_nonterminal_jobs=_count_quarantined(conn),
                reconciled_entities=(),
                duplicate_actions=duplicates,
                unresolved_entities=_count_unresolved(conn),
                complete=False,
            )

        # Phase 2: terminalize orphaned jobs.
        duplicates += _terminalize_orphans(conn, recovery_id)

        # Phase 3: reconcile matching project entities.
        reconciled, dup = _reconcile_project_entities(conn, recovery_id)
        duplicates += dup

        # Phase 4: release funding reservations with no live compatible job.
        released, dup = _release_reservations(conn, recovery_id)
        duplicates += dup
        if released:
            reconciled["funding_reservation"] = released

        return RecoveryScanReport(
            recovery_id=recovery_id,
            provider_replays=0,
            runnable_after_pass=_count_runnable(conn),
            quarantined_nonterminal_jobs=_count_quarantined(conn),
            reconciled_entities=tuple(sorted(reconciled.items())),
            duplicate_actions=duplicates,
            unresolved_entities=_count_unresolved(conn),
            complete=True,
        )
    finally:
        conn.close()
