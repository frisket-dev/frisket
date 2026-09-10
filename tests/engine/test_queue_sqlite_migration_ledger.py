from __future__ import annotations

import sqlite3
from pathlib import Path

import frisket.engine.jobs.queue as queue_module
from frisket.engine.jobs import QUEUE_DB_NAME, open_queue
from frisket.engine.jobs.queue_migrations import (
    QUEUE_SCHEMA_LEDGER,
    QUEUE_SCHEMA_VERSION,
)

# A jobs table whose columns match what today's SqliteJobQueue._ensure_ref_schema
# produces (verified against PRAGMA table_info at authoring time): the base
# JOBS_SCHEMA columns plus the appended ref/version columns, and NO version
# ledger. This is the "real pre-ledger .queue.db" shape the migrator must
# upgrade in place; it is spelled out as raw SQL so the fixture stays valid
# after _ensure_ref_schema/REF_COLUMNS are deleted.
_PRE_LEDGER_JOBS_DDL = """
CREATE TABLE jobs (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,
  payload TEXT NOT NULL DEFAULT '{}',
  org_id TEXT,
  project_id TEXT,
  run_id INTEGER,
  source_id INTEGER,
  sheet_id INTEGER,
  row_id INTEGER,
  receipt_id TEXT,
  action_kind TEXT,
  trace_id TEXT,
  workspace_root TEXT,
  status TEXT NOT NULL DEFAULT 'queued',
  attempts INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 3,
  locked_by TEXT,
  locked_at TEXT,
  lease_expires_at TEXT,
  available_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  result TEXT,
  error TEXT,
  storage_org_id INTEGER,
  dedupe_key TEXT,
  code_version TEXT,
  claimed_code_version TEXT,
  version_mismatch_requeues INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE worker_heartbeats (
  worker_id TEXT PRIMARY KEY,
  queue TEXT,
  first_seen_at TEXT NOT NULL,
  last_heartbeat_at TEXT NOT NULL,
  worker_version TEXT
);
"""

_ISO = "2020-01-01T00:00:00.000000+00:00"


def _tables(db_path: Path) -> "set[str]":
    con = sqlite3.connect(db_path)
    try:
        return {
            row[0]
            for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        con.close()


def _ledger_versions(db_path: Path) -> "list[int]":
    con = sqlite3.connect(db_path)
    try:
        return [
            int(row[0])
            for row in con.execute(
                f"SELECT version FROM {QUEUE_SCHEMA_LEDGER} ORDER BY version"
            )
        ]
    finally:
        con.close()


def _build_pre_ledger_queue_db(db_path: Path) -> None:
    con = sqlite3.connect(db_path)
    try:
        con.executescript(_PRE_LEDGER_JOBS_DDL)
        con.execute(
            "INSERT INTO jobs (kind, payload, status, attempts, max_attempts,"
            " available_at, created_at) VALUES (?, ?, 'queued', 0, 3, ?, ?)",
            ("echo", '{"sentinel": true}', _ISO, _ISO),
        )
        con.commit()
    finally:
        con.close()


def test_sqlite_queue_upgrades_through_the_ordered_version_ledger(tmp_path):
    queue = open_queue(workspace=tmp_path)
    try:
        queue.enqueue("echo", {"x": 1})
    finally:
        queue.close()

    db_path = tmp_path / QUEUE_DB_NAME
    assert QUEUE_SCHEMA_LEDGER in _tables(db_path), (
        f"the SQLite queue db has no {QUEUE_SCHEMA_LEDGER} ledger; the ordered "
        "version ledger (queue_migrations) must extend to the SQLite backend "
        "and run inline at open_queue() in initialize mode"
    )
    assert _ledger_versions(db_path) == list(range(1, QUEUE_SCHEMA_VERSION + 1)), (
        f"SQLite queue ledger recorded {_ledger_versions(db_path)}, expected the "
        f"contiguous prefix 1..{QUEUE_SCHEMA_VERSION}"
    )

    # _ensure_ref_schema / REF_COLUMNS are retired by the unified ledger.
    assert not hasattr(queue_module, "REF_COLUMNS"), (
        "frisket.engine.jobs.queue still defines REF_COLUMNS: the ordered ledger "
        "replaces the runtime column migrator entirely"
    )
    for name, obj in vars(queue_module).items():
        if isinstance(obj, type) and "_ensure_ref_schema" in vars(obj):
            raise AssertionError(
                f"{name}._ensure_ref_schema still present: the ordered ledger "
                "retires the runtime column migrator entirely"
            )


def test_pre_ledger_queue_db_upgrades_in_place_and_records_version(tmp_path):
    db_path = tmp_path / QUEUE_DB_NAME
    _build_pre_ledger_queue_db(db_path)

    # Reopening drives the inline initialize-mode migration on the existing db.
    queue = open_queue(workspace=tmp_path)
    try:
        payloads = [job.payload for job in queue.list_jobs()]
    finally:
        queue.close()

    assert QUEUE_SCHEMA_LEDGER in _tables(db_path), (
        "a pre-ledger .queue.db was not upgraded to carry the version ledger; "
        "the SQLite ledger must upgrade an existing pre-ledger db in place"
    )
    assert _ledger_versions(db_path) == list(range(1, QUEUE_SCHEMA_VERSION + 1)), (
        "the upgraded pre-ledger db did not record the contiguous version "
        f"prefix 1..{QUEUE_SCHEMA_VERSION}; got {_ledger_versions(db_path)}"
    )
    assert {"sentinel": True} in payloads, (
        "the pre-existing pre-ledger job row was lost: the ledger must upgrade "
        "the queue db IN PLACE and record the version, never stamp a fresh-only "
        "create_all result"
    )


def _insert_active_dedupe_row(
    con: sqlite3.Connection, *, kind: str, project_id: str, dedupe_key: str
) -> int:
    cursor = con.execute(
        "INSERT INTO jobs (kind, payload, project_id, dedupe_key, status,"
        " attempts, max_attempts, available_at, created_at)"
        " VALUES (?, '{}', ?, ?, 'queued', 0, 3, ?, ?)",
        (kind, project_id, dedupe_key, _ISO, _ISO),
    )
    return int(cursor.lastrowid)


def test_v5_reconciles_duplicate_active_dedupe_rows_and_enforces_uniqueness(
    tmp_path,
):
    """A legal v4 database holding duplicate ACTIVE rows for one
    (kind, project_id, dedupe_key) upgrades in place: the
    oldest row survives, newer duplicates are cancelled with a canonical
    reason — and the partial unique index then blocks new duplicates at
    insert time."""
    db_path = tmp_path / QUEUE_DB_NAME
    _build_pre_ledger_queue_db(db_path)
    con = sqlite3.connect(db_path)
    try:
        first = _insert_active_dedupe_row(
            con, kind="notification.deliver", project_id="p1", dedupe_key="dk-1"
        )
        second = _insert_active_dedupe_row(
            con, kind="notification.deliver", project_id="p1", dedupe_key="dk-1"
        )
        other = _insert_active_dedupe_row(
            con, kind="notification.deliver", project_id="p2", dedupe_key="dk-1"
        )
        con.commit()
    finally:
        con.close()

    queue = open_queue(workspace=tmp_path)
    try:
        assert queue.get(first).status == "queued", "the oldest duplicate survives"
        loser = queue.get(second)
        assert loser.status == "cancelled"
        assert "schema upgrade" in (loser.error or "")
        assert queue.get(other).status == "queued", (
            "a same-key row in ANOTHER project is not a duplicate"
        )

        # The index now enforces what callers used to only check: a raced
        # duplicate enqueue returns the EXISTING active job's id.
        raced = queue.enqueue(
            "notification.deliver",
            {"project_id": "p1", "dedupe_key": "dk-1"},
        )
        assert raced == first

        # Terminal rows never block a fresh enqueue for the same key.
        assert queue.cancel(first) is True
        fresh = queue.enqueue(
            "notification.deliver",
            {"project_id": "p1", "dedupe_key": "dk-1"},
        )
        assert fresh not in (first, second)
        assert queue.get(fresh).status == "queued"
    finally:
        queue.close()
