"""Upgrade known project schemas, fence unknown schemas, then reconcile policy.

Each known upgrade and its digest stamp commit atomically, in schema order.
Current bundles still reconcile standing consent and retention policy.
This leaf uses the facade's connection without importing the facade itself.
"""

from __future__ import annotations

import threading
import sqlite3
from pathlib import Path
from typing import Any

from .schema import SCHEMA_DIGEST_META_KEY, require_current_schema

# v0.1.1a62 (907a324c) -> v0.1.1a64's receipt-owned execution attempts.
# Fixed endpoints cannot accidentally stamp a future schema edit as current.
_RECEIPT_ATTEMPTS_FROM_DIGEST = "frisket.schema.v1:c59c7a43f961df588701133210d36503"
_RECEIPT_ATTEMPTS_TO_DIGEST = "frisket.schema.v1:0345f3cf7f8e37534124df43e4e97f05"

# v0.1.1a64 (6602458d) -> the cost-preapproval column. Fixed endpoints ensure
# a future DDL edit cannot accidentally stamp this one-column upgrade as current.
_PREAPPROVAL_FROM_DIGEST = "frisket.schema.v1:0345f3cf7f8e37534124df43e4e97f05"
_PREAPPROVAL_TO_DIGEST = "frisket.schema.v1:9a412a2c9b30c8de69323f477bc7e46e"

# v0.1.1a65 (9556b2ae) -> per-result review decisions and notes. Existing
# review_state values are preserved; the new nullable fields deliberately stay
# NULL because the old state alone cannot tell accept from corrected edit.
_REVIEW_METADATA_FROM_DIGEST = _PREAPPROVAL_TO_DIGEST
_REVIEW_METADATA_TO_DIGEST = "frisket.schema.v1:43e64204b7c8109bc79bfaa1dfdf8b0e"

# v0.1.1a66 -> provenance-owned base writes and the current-cell read model.
# The destination is pinned to this migration's exact fresh-bundle DDL below.
_CURRENT_CELLS_FROM_DIGEST = _REVIEW_METADATA_TO_DIGEST
_CURRENT_CELLS_TO_DIGEST = "frisket.schema.v1:eb7a1342f9f42a48ba6b1ec3863b3a5a"

# The frontend fires hot read endpoints (/sheets, /review/queue) concurrently,
# so two threads can open the same per-project DB at once. Both open-time
# reconciliations below are read-then-write with no CAS: two threads that both
# read "no standing consent for this principal" both mint one, and the bundle
# grows a duplicate policy row per concurrent open. A process-wide per-DB lock
# serializes them. Keyed by resolved db_path.
_OPEN_LOCKS: dict[str, threading.Lock] = {}


_OPEN_LOCKS_GUARD = threading.Lock()


def _open_lock(db_path: Path) -> threading.Lock:
    key = str(Path(db_path).resolve())
    with _OPEN_LOCKS_GUARD:
        lock = _OPEN_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _OPEN_LOCKS[key] = lock
        return lock


def open_bundle(project: Any) -> None:
    """Upgrade the known prior schema, fence, then reconcile policy rows."""
    with _open_lock(project.db_path):
        _migrate_receipt_attempts(project.db)
        _migrate_cost_preapproval(project.db)
        _migrate_review_metadata(project.db)
        _migrate_current_cells(project.db)
        require_current_schema(project.db, bundle_path=project.path)
        _reconcile_open_time_policy(project)


def _migrate_receipt_attempts(db: sqlite3.Connection) -> None:
    query = "SELECT value FROM meta WHERE key=?"
    try:
        row = db.execute(query, (SCHEMA_DIGEST_META_KEY,)).fetchone()
    except sqlite3.DatabaseError:
        return  # The schema fence supplies the ordinary unknown-bundle refusal.
    if row is None or row[0] != _RECEIPT_ATTEMPTS_FROM_DIGEST:
        return
    db.execute("BEGIN IMMEDIATE")
    try:
        # Another process may have completed this upgrade while we waited.
        row = db.execute(query, (SCHEMA_DIGEST_META_KEY,)).fetchone()
        if row is not None and row[0] == _RECEIPT_ATTEMPTS_FROM_DIGEST:
            db.execute(
                "ALTER TABLE execution_attempts ADD COLUMN receipt_id TEXT "
                "REFERENCES receipts(id) ON DELETE SET NULL "
                "CHECK (run_id IS NULL OR receipt_id IS NULL)"
            )
            db.execute(
                "CREATE UNIQUE INDEX uq_receipt_execution_attempts_seq "
                "ON execution_attempts(receipt_id, seq)"
            )
            db.execute(
                "CREATE UNIQUE INDEX uq_receipt_execution_attempts_one_dispatching "
                "ON execution_attempts(receipt_id) WHERE state='dispatching'"
            )
            db.execute(
                "UPDATE meta SET value=? WHERE key=?",
                (_RECEIPT_ATTEMPTS_TO_DIGEST, SCHEMA_DIGEST_META_KEY),
            )
        db.commit()
    except BaseException:
        db.rollback()
        raise


def _migrate_cost_preapproval(db: sqlite3.Connection) -> None:
    query = "SELECT value FROM meta WHERE key=?"
    try:
        row = db.execute(query, (SCHEMA_DIGEST_META_KEY,)).fetchone()
    except sqlite3.DatabaseError:
        return  # The schema fence supplies the ordinary unknown-bundle refusal.
    if row is None or row[0] != _PREAPPROVAL_FROM_DIGEST:
        return
    db.execute("BEGIN IMMEDIATE")
    try:
        # Another process may have completed this upgrade while we waited.
        row = db.execute(query, (SCHEMA_DIGEST_META_KEY,)).fetchone()
        if row is not None and row[0] == _PREAPPROVAL_FROM_DIGEST:
            db.execute("ALTER TABLE runs ADD COLUMN consent_principal TEXT")
            db.execute(
                "UPDATE meta SET value=? WHERE key=?",
                (_PREAPPROVAL_TO_DIGEST, SCHEMA_DIGEST_META_KEY),
            )
        db.commit()
    except BaseException:
        db.rollback()
        raise


def _migrate_review_metadata(db: sqlite3.Connection) -> None:
    query = "SELECT value FROM meta WHERE key=?"
    try:
        row = db.execute(query, (SCHEMA_DIGEST_META_KEY,)).fetchone()
    except sqlite3.DatabaseError:
        return
    if row is None or row[0] != _REVIEW_METADATA_FROM_DIGEST:
        return
    db.execute("BEGIN IMMEDIATE")
    try:
        row = db.execute(query, (SCHEMA_DIGEST_META_KEY,)).fetchone()
        if row is not None and row[0] == _REVIEW_METADATA_FROM_DIGEST:
            db.execute(
                "ALTER TABLE results ADD COLUMN review_decision TEXT "
                "CHECK (review_decision IN "
                "('accept', 'reject', 'reject_clear', 'edit'))"
            )
            db.execute("ALTER TABLE results ADD COLUMN review_note TEXT")
            db.execute(
                "UPDATE meta SET value=? WHERE key=?",
                (_REVIEW_METADATA_TO_DIGEST, SCHEMA_DIGEST_META_KEY),
            )
        db.commit()
    except BaseException:
        db.rollback()
        raise


def _migrate_current_cells(db: sqlite3.Connection) -> None:
    """Add provenance linkage and atomically backfill the visible projection."""

    query = "SELECT value FROM meta WHERE key=?"
    try:
        row = db.execute(query, (SCHEMA_DIGEST_META_KEY,)).fetchone()
    except sqlite3.DatabaseError:
        return
    if row is None or row[0] != _CURRENT_CELLS_FROM_DIGEST:
        return

    db.execute("BEGIN IMMEDIATE")
    try:
        # Another process may have completed the upgrade while we waited.
        row = db.execute(query, (SCHEMA_DIGEST_META_KEY,)).fetchone()
        if row is not None and row[0] == _CURRENT_CELLS_FROM_DIGEST:
            db.execute(
                "CREATE TABLE base_cell_producers ("
                "id INTEGER PRIMARY KEY,"
                "stage_id TEXT NOT NULL UNIQUE CHECK (length(trim(stage_id)) > 0),"
                "op_id INTEGER REFERENCES ops(id) ON DELETE RESTRICT,"
                "created_at TEXT NOT NULL DEFAULT (datetime('now'))"
                ")"
            )
            db.execute(
                "ALTER TABLE cells ADD COLUMN producer_id INTEGER "
                "REFERENCES base_cell_producers(id) ON DELETE RESTRICT"
            )
            db.execute("CREATE INDEX idx_cells_column ON cells(column_id,row_id)")
            db.execute(
                "CREATE TABLE current_cells ("
                "column_id INTEGER NOT NULL REFERENCES columns(id) ON DELETE CASCADE,"
                "row_id INTEGER NOT NULL REFERENCES rows(id) ON DELETE CASCADE,"
                "value TEXT,"
                "origin_kind TEXT NOT NULL CHECK (origin_kind IN "
                "('source_cell', 'run_result', 'manual_edit')),"
                "origin_op_id INTEGER REFERENCES ops(id) ON DELETE CASCADE,"
                "origin_run_id INTEGER REFERENCES runs(id) ON DELETE CASCADE,"
                "base_producer_id INTEGER REFERENCES base_cell_producers(id) "
                "ON DELETE RESTRICT,"
                "PRIMARY KEY (column_id, row_id),"
                "CHECK ("
                "(origin_kind='source_cell' AND origin_op_id IS NULL "
                "AND origin_run_id IS NULL) OR "
                "(origin_kind='run_result' AND origin_op_id IS NOT NULL "
                "AND origin_run_id IS NOT NULL AND base_producer_id IS NULL) OR "
                "(origin_kind='manual_edit' AND origin_op_id IS NOT NULL "
                "AND origin_run_id IS NULL AND base_producer_id IS NULL)"
                ")"
                ") WITHOUT ROWID"
            )
            db.execute("CREATE INDEX idx_current_cells_row ON current_cells(row_id)")

            # Lazy leaf import avoids loading the facade during bundle open.
            from .current_cells import rebuild_current_cells

            rebuild_current_cells(db)
            db.execute(
                "UPDATE meta SET value=? WHERE key=?",
                (_CURRENT_CELLS_TO_DIGEST, SCHEMA_DIGEST_META_KEY),
            )
        db.commit()
    except BaseException:
        db.rollback()
        raise


def _reconcile_open_time_policy(project: Any) -> None:
    # Lazy import: execution_routes imports frisket.execution.promises only;
    # this leaf must stay clear of import cycles at module load.
    from frisket.engine.store.execution_routes import ensure_standing_cost_consent

    # §5.1/F2: today's sub-$X no-gate UX as an explicit, auditable, revocable
    # standing consent -- minted for the current installation principal and
    # RECONCILED at every open (an env-knob change mints a successor row;
    # coverage reads the persisted head, never the knob).
    ensure_standing_cost_consent(project)
    project._ensure_retention_policy()
