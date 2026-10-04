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

from .disk_capacity import require_disk_headroom
from .schema import (
    BundleSchemaMismatch,
    SCHEMA_DIGEST_META_KEY,
    require_current_schema,
)
from .value_codec import migrate_legacy_json_value

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

# Current-cell validity is derived from the surviving value and current column
# descriptor. The source/result/edit layers remain untouched.
_CELL_VALIDITY_FROM_DIGEST = _CURRENT_CELLS_TO_DIGEST
_CELL_VALIDITY_TO_DIGEST = "frisket.schema.v1:8120b7fe7102b3570ff0c8326bd62fa2"

# Project Ask durable history. This is an additive, data-preserving upgrade
# from the exact preceding schema; the fence remains fail-closed for all other
# digests.
_PROJECT_QA_FROM_DIGEST = _CELL_VALIDITY_TO_DIGEST
_PROJECT_QA_TO_DIGEST = "frisket.schema.v1:b540a83f8325e5cbcd52fc3fac64eeb5"

# Run-level review completion is an additive workflow marker. Existing runs
# remain open; result values and per-cell decisions are untouched.
_RUN_REVIEW_STATUS_FROM_DIGEST = _PROJECT_QA_TO_DIGEST
_RUN_REVIEW_STATUS_TO_DIGEST = "frisket.schema.v1:caa3ac7c8aaa66153dd8e2cad5950942"

# Research options on Ask records plus resumable child state and its exact-cost
# operation ledger. Existing Ask and project data remain untouched.
_PROJECT_QA_RESEARCH_FROM_DIGEST = _RUN_REVIEW_STATUS_TO_DIGEST
_PROJECT_QA_RESEARCH_TO_DIGEST = "frisket.schema.v1:21bac5506d1f7cc240dbc519139168db"

_IMPORT_SESSIONS_FROM_DIGEST = _PROJECT_QA_RESEARCH_TO_DIGEST
_IMPORT_SESSIONS_TO_DIGEST = "frisket.schema.v1:cb9a46c224c6d5e95af3c7e91df76e26"
_SEARCH_WORK_FROM_DIGEST = _IMPORT_SESSIONS_TO_DIGEST
_SEARCH_WORK_TO_DIGEST = "frisket.schema.v1:f2d652e33a1633c4367813af3c2d3746"

# Remove secondary indexes exactly duplicated by UNIQUE constraints, plus the
# measured source-run prefix made redundant by its ordered composite index.
_INDEX_HYGIENE_FROM_DIGEST = _SEARCH_WORK_TO_DIGEST
_INDEX_HYGIENE_TO_DIGEST = "frisket.schema.v1:348c367f3a24a414ba6f1e612e40ea15"

# Large inline values pack poorly in the old index-organized cell tables. The
# upgrade changes only their physical b-tree representation: copied values and
# provenance stay exact, and the existing named current-column index remains
# the search indexer's narrow scan surface.
_ROWID_CELL_LAYOUT_FROM_DIGEST = _INDEX_HYGIENE_TO_DIGEST
_ROWID_CELL_LAYOUT_TO_DIGEST = "frisket.schema.v1:f078f2bc57411d372468936618f2f884"

_ACTIVE_COLUMNS_FROM_DIGEST = _ROWID_CELL_LAYOUT_TO_DIGEST
_ACTIVE_COLUMNS_TO_DIGEST = "frisket.schema.v1:e865652f2f64091605730c143e3ee5f3"

_TYPED_VALUES_FROM_DIGEST = _ACTIVE_COLUMNS_TO_DIGEST
_TYPED_VALUES_TO_DIGEST = "frisket.schema.v1:5e4662708f4f1b9a97a929268d0ab7c5"
_TYPED_VALUE_COPY_BATCH_SIZE = 2_000

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
        _migrate_cell_validity(project.db)
        _migrate_project_qa(project.db)
        _migrate_run_review_status(project.db)
        _migrate_project_qa_research(project.db)
        _migrate_import_sessions(project.db)
        _migrate_search_work(project.db)
        _migrate_index_hygiene(project.db)
        _migrate_rowid_cell_layout(project.db)
        _migrate_active_columns(project.db)
        _migrate_typed_values(project.db, bundle_path=project.db_path)
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

            rebuild_current_cells(db, invalidate_search=False)
            db.execute(
                "UPDATE meta SET value=? WHERE key=?",
                (_CURRENT_CELLS_TO_DIGEST, SCHEMA_DIGEST_META_KEY),
            )
        db.commit()
    except BaseException:
        db.rollback()
        raise


def _migrate_cell_validity(db: sqlite3.Connection) -> None:
    """Add and backfill derived validity without rewriting source values."""

    query = "SELECT value FROM meta WHERE key=?"
    try:
        row = db.execute(query, (SCHEMA_DIGEST_META_KEY,)).fetchone()
    except sqlite3.DatabaseError:
        return
    if row is None or row[0] != _CELL_VALIDITY_FROM_DIGEST:
        return

    db.execute("BEGIN IMMEDIATE")
    try:
        row = db.execute(query, (SCHEMA_DIGEST_META_KEY,)).fetchone()
        if row is not None and row[0] == _CELL_VALIDITY_FROM_DIGEST:
            db.execute(
                "ALTER TABLE current_cells ADD COLUMN validity TEXT NOT NULL "
                "DEFAULT 'valid' CHECK (validity IN ('valid','missing','invalid'))"
            )
            from .current_cells import rebuild_current_cells

            rebuild_current_cells(db, invalidate_search=False)
            db.execute(
                "UPDATE meta SET value=? WHERE key=?",
                (_CELL_VALIDITY_TO_DIGEST, SCHEMA_DIGEST_META_KEY),
            )
        db.commit()
    except BaseException:
        db.rollback()
        raise


def _migrate_project_qa(db: sqlite3.Connection) -> None:
    """Add durable Ask records without altering existing project content."""

    query = "SELECT value FROM meta WHERE key=?"
    try:
        row = db.execute(query, (SCHEMA_DIGEST_META_KEY,)).fetchone()
    except sqlite3.DatabaseError:
        return
    if row is None or row[0] != _PROJECT_QA_FROM_DIGEST:
        return
    db.execute("BEGIN IMMEDIATE")
    try:
        row = db.execute(query, (SCHEMA_DIGEST_META_KEY,)).fetchone()
        if row is not None and row[0] == _PROJECT_QA_FROM_DIGEST:
            db.execute(
                "CREATE TABLE project_qa_threads ("
                "id TEXT PRIMARY KEY,title TEXT NOT NULL,scope_json TEXT NOT NULL,"
                "model TEXT,web INTEGER NOT NULL DEFAULT 0 CHECK (web IN (0,1)),"
                "suggest_actions INTEGER NOT NULL DEFAULT 1 CHECK (suggest_actions IN (0,1)),"
                "revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),"
                "created_by TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL)"
            )
            db.execute(
                "CREATE INDEX idx_project_qa_threads_updated "
                "ON project_qa_threads(updated_at DESC, id DESC)"
            )
            db.execute(
                "CREATE TABLE project_qa_turns ("
                "id TEXT PRIMARY KEY,thread_id TEXT NOT NULL REFERENCES project_qa_threads(id) ON DELETE CASCADE,"
                "request_id TEXT NOT NULL,question TEXT NOT NULL,scope_json TEXT NOT NULL,model TEXT,"
                "web INTEGER NOT NULL CHECK (web IN (0,1)),"
                "suggest_actions INTEGER NOT NULL CHECK (suggest_actions IN (0,1)),"
                "status TEXT NOT NULL CHECK (status IN ('running','stopping','completed','stopped','failed','interrupted')),"
                "submitted_by TEXT,started_at TEXT NOT NULL,finished_at TEXT,usage_json TEXT,"
                "cost_actual REAL,error_summary TEXT,UNIQUE(thread_id, request_id))"
            )
            db.execute(
                "CREATE INDEX idx_project_qa_turns_thread_started "
                "ON project_qa_turns(thread_id, started_at DESC, id DESC)"
            )
            db.execute(
                "CREATE UNIQUE INDEX uq_project_qa_turns_one_active ON project_qa_turns(thread_id) "
                "WHERE status IN ('running','stopping')"
            )
            db.execute(
                "CREATE TABLE project_qa_events ("
                "thread_id TEXT NOT NULL REFERENCES project_qa_threads(id) ON DELETE CASCADE,"
                "turn_id TEXT NOT NULL REFERENCES project_qa_turns(id) ON DELETE CASCADE,"
                "seq INTEGER NOT NULL CHECK (seq > 0),kind TEXT NOT NULL,payload_json TEXT NOT NULL,"
                "created_at TEXT NOT NULL,PRIMARY KEY (thread_id, seq)) WITHOUT ROWID"
            )
            db.execute(
                "CREATE INDEX idx_project_qa_events_turn ON project_qa_events(turn_id, seq)"
            )
            db.execute(
                "CREATE TABLE project_qa_citations ("
                "id TEXT PRIMARY KEY,turn_id TEXT NOT NULL REFERENCES project_qa_turns(id) ON DELETE CASCADE,"
                "label TEXT NOT NULL,source_kind TEXT NOT NULL,locator_json TEXT NOT NULL,excerpt TEXT,"
                "metadata_json TEXT NOT NULL DEFAULT '{}',created_at TEXT NOT NULL)"
            )
            db.execute(
                "CREATE INDEX idx_project_qa_citations_turn ON project_qa_citations(turn_id, id)"
            )
            db.execute(
                "CREATE TABLE project_qa_usage_calls ("
                "turn_id TEXT NOT NULL REFERENCES project_qa_turns(id) ON DELETE CASCADE,"
                "call_id TEXT NOT NULL,PRIMARY KEY (turn_id, call_id)) WITHOUT ROWID"
            )
            db.execute(
                "UPDATE meta SET value=? WHERE key=?",
                (_PROJECT_QA_TO_DIGEST, SCHEMA_DIGEST_META_KEY),
            )
        db.commit()
    except BaseException:
        db.rollback()
        raise


def _migrate_run_review_status(db: sqlite3.Connection) -> None:
    """Add a nullable workflow marker while preserving existing run data."""

    query = "SELECT value FROM meta WHERE key=?"
    try:
        row = db.execute(query, (SCHEMA_DIGEST_META_KEY,)).fetchone()
    except sqlite3.DatabaseError:
        return
    if row is None or row[0] != _RUN_REVIEW_STATUS_FROM_DIGEST:
        return
    db.execute("BEGIN IMMEDIATE")
    try:
        row = db.execute(query, (SCHEMA_DIGEST_META_KEY,)).fetchone()
        if row is not None and row[0] == _RUN_REVIEW_STATUS_FROM_DIGEST:
            db.execute("ALTER TABLE runs ADD COLUMN review_completed_at TEXT")
            db.execute(
                "UPDATE meta SET value=? WHERE key=?",
                (_RUN_REVIEW_STATUS_TO_DIGEST, SCHEMA_DIGEST_META_KEY),
            )
        db.commit()
    except BaseException:
        db.rollback()
        raise


def _migrate_project_qa_research(db: sqlite3.Connection) -> None:
    """Add research settings, continuation state, and exact-cost operations."""

    query = "SELECT value FROM meta WHERE key=?"
    try:
        row = db.execute(query, (SCHEMA_DIGEST_META_KEY,)).fetchone()
    except sqlite3.DatabaseError:
        return
    if row is None or row[0] != _PROJECT_QA_RESEARCH_FROM_DIGEST:
        return
    db.execute("BEGIN IMMEDIATE")
    try:
        row = db.execute(query, (SCHEMA_DIGEST_META_KEY,)).fetchone()
        if row is not None and row[0] == _PROJECT_QA_RESEARCH_FROM_DIGEST:
            db.execute("ALTER TABLE project_qa_threads ADD COLUMN research_json TEXT")
            db.execute("ALTER TABLE project_qa_turns ADD COLUMN research_json TEXT")
            db.execute(
                "CREATE TABLE IF NOT EXISTS project_qa_research_runs ("
                "id TEXT PRIMARY KEY,"
                "turn_id TEXT NOT NULL UNIQUE REFERENCES project_qa_turns(id) ON DELETE CASCADE,"
                "actor TEXT,"
                "state TEXT NOT NULL CHECK (state IN ('running','paused','interrupted','completed')),"
                "budget_micros INTEGER NOT NULL CHECK (budget_micros >= 0),"
                "currency TEXT NOT NULL,"
                "write_mode TEXT NOT NULL CHECK (write_mode IN ('ask_each','ask_overwrite','full_access')),"
                "max_turns INTEGER CHECK (max_turns IS NULL OR max_turns > 0),"
                "turn_count INTEGER NOT NULL DEFAULT 0 CHECK (turn_count >= 0),"
                "skills_json TEXT NOT NULL DEFAULT '[]',"
                "saved_messages_json TEXT NOT NULL DEFAULT '[]',"
                "pending_approval_json TEXT,"
                "output_grants_json TEXT NOT NULL DEFAULT '[]',"
                "revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),"
                "created_at TEXT NOT NULL,updated_at TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS project_qa_research_operations ("
                "research_id TEXT NOT NULL REFERENCES project_qa_research_runs(id) ON DELETE CASCADE,"
                "operation_id TEXT NOT NULL,payload_identity TEXT NOT NULL,"
                "operation_kind TEXT NOT NULL CHECK (operation_kind IN ('model','action','search')),"
                "estimate_micros INTEGER NOT NULL CHECK (estimate_micros >= 0),"
                "actual_micros INTEGER CHECK (actual_micros IS NULL OR actual_micros >= 0),"
                "metadata_json TEXT NOT NULL DEFAULT '{}',created_at TEXT NOT NULL,settled_at TEXT,"
                "PRIMARY KEY (research_id, operation_id)) WITHOUT ROWID"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_project_qa_research_operations_unsettled "
                "ON project_qa_research_operations(research_id) "
                "WHERE actual_micros IS NULL"
            )
            db.execute(
                "UPDATE meta SET value=? WHERE key=?",
                (_PROJECT_QA_RESEARCH_TO_DIGEST, SCHEMA_DIGEST_META_KEY),
            )
        db.commit()
    except BaseException:
        db.rollback()
        raise


def _migrate_import_sessions(db: sqlite3.Connection) -> None:
    """Add the resumable import checkpoint owner without rewriting user data."""

    query = "SELECT value FROM meta WHERE key=?"
    try:
        row = db.execute(query, (SCHEMA_DIGEST_META_KEY,)).fetchone()
    except sqlite3.DatabaseError:
        return
    if row is None or row[0] != _IMPORT_SESSIONS_FROM_DIGEST:
        return
    db.execute("BEGIN IMMEDIATE")
    try:
        row = db.execute(query, (SCHEMA_DIGEST_META_KEY,)).fetchone()
        if row is not None and row[0] == _IMPORT_SESSIONS_FROM_DIGEST:
            db.execute(
                "CREATE TABLE import_sessions ("
                "id TEXT PRIMARY KEY,"
                "sheet_id INTEGER REFERENCES sheets(id) ON DELETE SET NULL,"
                "producer_id INTEGER REFERENCES base_cell_producers(id) ON DELETE SET NULL,"
                "op_id INTEGER NOT NULL REFERENCES ops(id) ON DELETE RESTRICT,"
                "receipt_id TEXT REFERENCES receipts(id) ON DELETE SET NULL,"
                "writer_authority TEXT NOT NULL,"
                "cursor INTEGER NOT NULL DEFAULT 0 CHECK (cursor >= 0),"
                "committed_rows INTEGER NOT NULL DEFAULT 0 CHECK (committed_rows >= 0),"
                "committed_bytes INTEGER NOT NULL DEFAULT 0 CHECK (committed_bytes >= 0),"
                "state TEXT NOT NULL CHECK (state IN ('active','paused','cancelled','kept','completed','removed')),"
                "created_at TEXT NOT NULL DEFAULT (datetime('now')),"
                "updated_at TEXT NOT NULL DEFAULT (datetime('now')))"
            )
            db.execute(
                "CREATE UNIQUE INDEX uq_import_sessions_active_sheet "
                "ON import_sessions(sheet_id) "
                "WHERE state IN ('active','paused','cancelled')"
            )
            db.execute(
                "UPDATE meta SET value=? WHERE key=?",
                (_IMPORT_SESSIONS_TO_DIGEST, SCHEMA_DIGEST_META_KEY),
            )
        db.commit()
    except BaseException:
        db.rollback()
        raise


def _migrate_search_work(db: sqlite3.Connection) -> None:
    """Add the sidecar worklist and seed existing projects for full repair."""

    query = "SELECT value FROM meta WHERE key=?"
    try:
        row = db.execute(query, (SCHEMA_DIGEST_META_KEY,)).fetchone()
    except sqlite3.DatabaseError:
        return
    if row is None or row[0] != _SEARCH_WORK_FROM_DIGEST:
        return
    db.execute("BEGIN IMMEDIATE")
    try:
        row = db.execute(query, (SCHEMA_DIGEST_META_KEY,)).fetchone()
        if row is not None and row[0] == _SEARCH_WORK_FROM_DIGEST:
            # Reuse worklist DDL, preserving this historical endpoint's index.
            from .schema import SCHEMA

            marker = "CREATE UNIQUE INDEX IF NOT EXISTS idx_current_cells_column_row"
            tail = SCHEMA[SCHEMA.index(marker) :]
            tail = tail[: tail.index("-- SEARCH_INDEX_WORK_END")]
            tail = tail.replace(marker, marker.replace("UNIQUE ", ""), 1)
            # The stable native-value view was added later with the typed
            # authority migration. Historical search upgrades still run over
            # JSON authorities, so install only their original worklist DDL.
            typed_marker = (
                "CREATE INDEX IF NOT EXISTS idx_current_cells_column_origin_row"
            )
            search_table = "CREATE TABLE IF NOT EXISTS search_dirty_scopes"
            if typed_marker in tail:
                tail = (
                    tail[: tail.index(typed_marker)] + tail[tail.index(search_table) :]
                )
            statement = ""
            for line in tail.splitlines():
                statement += line + "\n"
                if sqlite3.complete_statement(statement):
                    db.execute(statement)
                    statement = ""
            if statement.strip():
                raise RuntimeError("incomplete search work migration DDL")
            db.execute(
                "UPDATE meta SET value=? WHERE key=?",
                (_SEARCH_WORK_TO_DIGEST, SCHEMA_DIGEST_META_KEY),
            )
        db.commit()
    except BaseException:
        db.rollback()
        raise


def _migrate_index_hygiene(db: sqlite3.Connection) -> None:
    """Drop redundant b-trees without changing their constraints or rows."""

    query = "SELECT value FROM meta WHERE key=?"
    try:
        row = db.execute(query, (SCHEMA_DIGEST_META_KEY,)).fetchone()
    except sqlite3.DatabaseError:
        return
    if row is None or row[0] != _INDEX_HYGIENE_FROM_DIGEST:
        return

    db.execute("BEGIN IMMEDIATE")
    try:
        row = db.execute(query, (SCHEMA_DIGEST_META_KEY,)).fetchone()
        if row is not None and row[0] == _INDEX_HYGIENE_FROM_DIGEST:
            db.execute("DROP INDEX IF EXISTS idx_execution_attempts_run")
            db.execute("DROP INDEX IF EXISTS idx_source_items_source_dedupe")
            db.execute("DROP INDEX IF EXISTS idx_source_runs_source")
            db.execute(
                "UPDATE meta SET value=? WHERE key=?",
                (_INDEX_HYGIENE_TO_DIGEST, SCHEMA_DIGEST_META_KEY),
            )
        db.commit()
    except BaseException:
        db.rollback()
        raise


def _migrate_rowid_cell_layout(db: sqlite3.Connection) -> None:
    """Repack cell values into rowid tables without changing logical rows.

    The tables have no inbound foreign keys or triggers. Their existing
    outbound constraints and every value/provenance column are copied before
    the old tables are replaced, together with the named search scan index.
    """

    query = "SELECT value FROM meta WHERE key=?"
    try:
        row = db.execute(query, (SCHEMA_DIGEST_META_KEY,)).fetchone()
    except sqlite3.DatabaseError:
        return
    if row is None or row[0] != _ROWID_CELL_LAYOUT_FROM_DIGEST:
        return

    db.execute("BEGIN IMMEDIATE")
    try:
        row = db.execute(query, (SCHEMA_DIGEST_META_KEY,)).fetchone()
        if row is not None and row[0] == _ROWID_CELL_LAYOUT_FROM_DIGEST:
            # Connection.executescript() commits any pending transaction before
            # it starts. Execute each complete statement so the table swap and
            # digest stamp remain one rollback unit.
            for statement in (
                """
                CREATE TABLE cells_rowid_new (
                  row_id INTEGER NOT NULL REFERENCES rows(id) ON DELETE CASCADE,
                  column_id INTEGER NOT NULL REFERENCES columns(id) ON DELETE CASCADE,
                  value TEXT,
                  producer_id INTEGER REFERENCES base_cell_producers(id) ON DELETE RESTRICT,
                  UNIQUE (row_id, column_id)
                )
                """,
                """
                CREATE TABLE current_cells_rowid_new (
                  column_id INTEGER NOT NULL REFERENCES columns(id) ON DELETE CASCADE,
                  row_id INTEGER NOT NULL REFERENCES rows(id) ON DELETE CASCADE,
                  value TEXT,
                  origin_kind TEXT NOT NULL CHECK (
                    origin_kind IN ('source_cell', 'run_result', 'manual_edit')
                  ),
                  origin_op_id INTEGER REFERENCES ops(id) ON DELETE CASCADE,
                  origin_run_id INTEGER REFERENCES runs(id) ON DELETE CASCADE,
                  base_producer_id INTEGER REFERENCES base_cell_producers(id) ON DELETE RESTRICT,
                  validity TEXT NOT NULL CHECK (validity IN ('valid', 'missing', 'invalid')),
                  CHECK (
                    (
                      origin_kind='source_cell'
                      AND origin_op_id IS NULL
                      AND origin_run_id IS NULL
                    )
                    OR (
                      origin_kind='run_result'
                      AND origin_op_id IS NOT NULL
                      AND origin_run_id IS NOT NULL
                      AND base_producer_id IS NULL
                    )
                    OR (
                      origin_kind='manual_edit'
                      AND origin_op_id IS NOT NULL
                      AND origin_run_id IS NULL
                      AND base_producer_id IS NULL
                    )
                  )
                )
                """,
                "INSERT INTO cells_rowid_new(row_id,column_id,value,producer_id) "
                "SELECT row_id,column_id,value,producer_id FROM cells",
                "INSERT INTO current_cells_rowid_new("
                "column_id,row_id,value,origin_kind,origin_op_id,origin_run_id,"
                "base_producer_id,validity"
                ") SELECT "
                "column_id,row_id,value,origin_kind,origin_op_id,origin_run_id,"
                "base_producer_id,validity FROM current_cells",
                "DROP TABLE cells",
                "DROP TABLE current_cells",
                "ALTER TABLE cells_rowid_new RENAME TO cells",
                "ALTER TABLE current_cells_rowid_new RENAME TO current_cells",
                "CREATE INDEX idx_cells_column ON cells(column_id,row_id)",
                "CREATE UNIQUE INDEX idx_current_cells_column_row "
                "ON current_cells(column_id,row_id)",
                "CREATE INDEX idx_current_cells_row ON current_cells(row_id,column_id)",
            ):
                db.execute(statement)
            db.execute(
                "UPDATE meta SET value=? WHERE key=?",
                (_ROWID_CELL_LAYOUT_TO_DIGEST, SCHEMA_DIGEST_META_KEY),
            )
        db.commit()
    except BaseException:
        db.rollback()
        raise


def _migrate_active_columns(db: sqlite3.Connection) -> None:
    """Release undone names without changing column identity or stored history."""
    query = "SELECT value FROM meta WHERE key=?"
    try:
        row = db.execute(query, (SCHEMA_DIGEST_META_KEY,)).fetchone()
    except sqlite3.DatabaseError:
        return
    if row is None or row[0] != _ACTIVE_COLUMNS_FROM_DIGEST:
        return

    # SQLite requires FK enforcement disabled outside the transaction when
    # replacing a referenced table. Never rename the old table: that rewrites
    # child references. Legacy ALTER permits the brief gap in trigger references.
    foreign_keys = db.execute("PRAGMA foreign_keys").fetchone()[0]
    legacy_alter = db.execute("PRAGMA legacy_alter_table").fetchone()[0]
    db.execute("PRAGMA foreign_keys=OFF")
    db.execute("PRAGMA legacy_alter_table=ON")
    try:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(query, (SCHEMA_DIGEST_META_KEY,)).fetchone()
        if row is not None and row[0] == _ACTIVE_COLUMNS_FROM_DIGEST:
            objects = db.execute(
                "SELECT sql FROM sqlite_master WHERE tbl_name='columns' "
                "AND type IN ('index','trigger') AND sql IS NOT NULL"
            ).fetchall()
            db.execute("""
                CREATE TABLE columns_active_new (
                  id INTEGER PRIMARY KEY,
                  sheet_id INTEGER NOT NULL REFERENCES sheets(id) ON DELETE CASCADE,
                  name TEXT NOT NULL,
                  type TEXT NOT NULL DEFAULT 'text',
                  position INTEGER NOT NULL DEFAULT 0,
                  current_run_id INTEGER,
                  ai_generated INTEGER NOT NULL DEFAULT 0,
                  hidden INTEGER NOT NULL DEFAULT 0,
                  active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1) AND (active=1 OR hidden=1)),
                  default_hidden INTEGER NOT NULL DEFAULT 0,
                  format TEXT,
                  semantic_type TEXT,
                  created_at TEXT NOT NULL DEFAULT (datetime('now'))
                )
            """)
            # Older writers revived hidden IDs, so only their latest creation
            # operation determines lifecycle. Hidden columns without undone
            # creation history remain live (e.g. internal sidecars).
            db.execute("""
                INSERT INTO columns_active_new
                  (id,sheet_id,name,type,position,current_run_id,ai_generated,
                   hidden,default_hidden,format,semantic_type,created_at,active)
                SELECT id,sheet_id,name,type,position,current_run_id,ai_generated,
                       hidden,default_hidden,format,semantic_type,created_at,
                       CASE WHEN hidden=1 AND (
                         SELECT o.status FROM ops o,
                           json_each(o.undo_info,'$.created_columns') created
                         WHERE CAST(created.value AS INTEGER)=columns.id
                         ORDER BY o.id DESC LIMIT 1
                       ) IN ('undone','discarded') THEN 0 ELSE 1 END
                FROM columns
            """)
            db.execute("DROP TABLE columns")
            db.execute("ALTER TABLE columns_active_new RENAME TO columns")
            for (statement,) in objects:
                db.execute(statement)
            db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_columns_active_name "
                "ON columns(sheet_id,name) WHERE active=1"
            )
            # Validate only the rebuilt table and references to it, not every
            # unrelated relationship in a potentially long-lived project.
            for table in (
                "columns",
                "cells",
                "output_column_claims",
                "run_output_generations",
                "current_cells",
                "watch_run_hits",
            ):
                if any(
                    table == "columns" or violation[2] == "columns"
                    for violation in db.execute(f"PRAGMA foreign_key_check({table})")
                ):
                    raise BundleSchemaMismatch(
                        "Column migration found an invalid column relationship "
                        f"in {table}. The migration was rolled back; keep the project "
                        "intact and repair the relationship before reopening."
                    )
            db.execute(
                "UPDATE meta SET value=? WHERE key=?",
                (_ACTIVE_COLUMNS_TO_DIGEST, SCHEMA_DIGEST_META_KEY),
            )
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.execute(f"PRAGMA legacy_alter_table={int(legacy_alter)}")
        db.execute(f"PRAGMA foreign_keys={int(foreign_keys)}")


def _fresh_schema_statement(prefix: str) -> str:
    """Return one complete statement from the current fresh-bundle schema."""

    from .schema import SCHEMA

    start = SCHEMA.index(prefix)
    statement = ""
    for line in SCHEMA[start:].splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            return statement.strip()
    raise RuntimeError(f"incomplete schema statement starting with {prefix!r}")


def _typed_table_ddl(table: str, replacement: str) -> str:
    prefix = f"CREATE TABLE IF NOT EXISTS {table}"
    return _fresh_schema_statement(prefix).replace(
        prefix, f"CREATE TABLE {replacement}", 1
    )


def _copy_typed_authority(
    db: sqlite3.Connection, *, table: str, replacement: str
) -> int:
    """Copy and decode one legacy JSON authority in bounded batches."""

    columns = [str(row[1]) for row in db.execute(f"PRAGMA table_info({table})")]
    value_index = columns.index("value")
    result_effect_index = (
        columns.index("publication_effect") if table == "results" else None
    )
    result_error_index = columns.index("error") if table == "results" else None
    destination_columns = [*columns[:value_index], "value_kind", *columns[value_index:]]
    placeholders = ",".join("?" for _ in destination_columns)
    insert_sql = (
        f"INSERT INTO {replacement} ({','.join(destination_columns)}) "
        f"VALUES ({placeholders})"
    )
    copied = 0
    cursor = db.execute(f"SELECT {','.join(columns)} FROM {table}")
    while batch := cursor.fetchmany(_TYPED_VALUE_COPY_BATCH_SIZE):
        converted = []
        for row in batch:
            values = list(row)
            raw_value = values[value_index]
            if table == "results":
                effect = values[result_effect_index]  # type: ignore[index]
                error = values[result_error_index]  # type: ignore[index]
                if effect == "publish_error" or (
                    raw_value is None and error is not None
                ):
                    value_kind, stored_value = None, None
                elif effect == "publish_null":
                    value_kind, stored_value = "null", None
                else:
                    value_kind, stored_value = migrate_legacy_json_value(raw_value)
            else:
                value_kind, stored_value = migrate_legacy_json_value(raw_value)
            values[value_index] = stored_value
            values.insert(value_index, value_kind)
            converted.append(values)
        db.executemany(insert_sql, converted)
        copied += len(converted)
    expected = int(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    actual = int(db.execute(f"SELECT COUNT(*) FROM {replacement}").fetchone()[0])
    if copied != expected or actual != expected:
        raise BundleSchemaMismatch(
            f"typed storage migration copied {actual} of {expected} {table} rows"
        )
    return copied


def _migrate_typed_values(db: sqlite3.Connection, *, bundle_path: str | Path) -> None:
    """Replace JSON payload copies with native typed authority values once."""

    query = "SELECT value FROM meta WHERE key=?"
    try:
        row = db.execute(query, (SCHEMA_DIGEST_META_KEY,)).fetchone()
    except sqlite3.DatabaseError:
        return
    if row is None or row[0] != _TYPED_VALUES_FROM_DIGEST:
        return

    page_count = int(db.execute("PRAGMA page_count").fetchone()[0])
    page_size = int(db.execute("PRAGMA page_size").fetchone()[0])
    # One old+new authority copy plus rollback journal/WAL is the conservative
    # peak. The projection is dropped first inside the transaction so SQLite
    # can reuse its pages, but the preflight does not rely on that saving.
    require_disk_headroom(
        Path(bundle_path).parent,
        2 * page_count * page_size,
    )

    foreign_keys = int(db.execute("PRAGMA foreign_keys").fetchone()[0])
    legacy_alter = int(db.execute("PRAGMA legacy_alter_table").fetchone()[0])
    db.execute("PRAGMA foreign_keys=OFF")
    db.execute("PRAGMA legacy_alter_table=ON")
    try:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(query, (SCHEMA_DIGEST_META_KEY,)).fetchone()
        if row is not None and row[0] == _TYPED_VALUES_FROM_DIGEST:
            current_count = int(
                db.execute("SELECT COUNT(*) FROM current_cells").fetchone()[0]
            )
            # Release the duplicated payload projection before allocating typed
            # authority pages. It is rebuildable from the three authorities.
            db.execute("DROP TABLE current_cells")

            authority_objects = db.execute(
                "SELECT type,sql FROM sqlite_master "
                "WHERE tbl_name IN ('cells','results','edits') "
                "AND type IN ('index','trigger') AND sql IS NOT NULL "
                "ORDER BY CASE type WHEN 'index' THEN 0 ELSE 1 END,name"
            ).fetchall()
            for table in ("cells", "results", "edits"):
                replacement = f"{table}_typed_new"
                db.execute(_typed_table_ddl(table, replacement))
                _copy_typed_authority(db, table=table, replacement=replacement)

            for table in ("cells", "results", "edits"):
                db.execute(f"DROP TABLE {table}")
                db.execute(f"ALTER TABLE {table}_typed_new RENAME TO {table}")
            for _object_type, statement in authority_objects:
                db.execute(str(statement))
            # These two historical triggers enumerate semantic payload fields;
            # reinstall their typed definitions so value_kind cannot change
            # independently of a published value.
            for trigger in (
                "trg_results_semantic_update_open_generation",
                "trg_results_published_semantics_immutable",
            ):
                db.execute(f"DROP TRIGGER {trigger}")
                db.execute(
                    _fresh_schema_statement(f"CREATE TRIGGER IF NOT EXISTS {trigger}")
                )

            db.execute(_typed_table_ddl("current_cells", "current_cells"))
            db.execute(
                "CREATE INDEX idx_current_cells_row ON current_cells(row_id,column_id)"
            )
            db.execute(
                "CREATE UNIQUE INDEX idx_current_cells_column_row "
                "ON current_cells(column_id,row_id)"
            )
            db.execute(
                "CREATE INDEX idx_current_cells_column_origin_row "
                "ON current_cells(column_id,origin_kind,row_id)"
            )
            from .current_cells import rebuild_current_cells

            rebuilt = rebuild_current_cells(db)
            if rebuilt != current_count:
                raise BundleSchemaMismatch(
                    "typed storage migration rebuilt a different number of current "
                    f"cells ({rebuilt}, expected {current_count})"
                )
            db.execute(
                _fresh_schema_statement("CREATE VIEW IF NOT EXISTS current_cell_values")
            )
            from .citation_text import (
                install_citation_text_schema,
                migrate_legacy_citation_texts,
            )

            install_citation_text_schema(db)
            migrate_legacy_citation_texts(db)
            visible = int(
                db.execute("SELECT COUNT(*) FROM current_cell_values").fetchone()[0]
            )
            if visible != current_count:
                raise BundleSchemaMismatch(
                    "typed storage migration left unresolved current-cell heads "
                    f"({visible}, expected {current_count})"
                )
            violations = db.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise BundleSchemaMismatch(
                    "typed storage migration found invalid foreign-key relationships"
                )
            db.execute(
                "UPDATE meta SET value=? WHERE key=?",
                (_TYPED_VALUES_TO_DIGEST, SCHEMA_DIGEST_META_KEY),
            )
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.execute(f"PRAGMA legacy_alter_table={legacy_alter}")
        db.execute(f"PRAGMA foreign_keys={foreign_keys}")


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
