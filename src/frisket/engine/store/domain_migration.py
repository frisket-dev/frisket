"""One-time data cutover from layered values to ordered project Changes.

The migration retains payload authorities and captures only their winning
references. Old operations remain inspectable but cannot become executable Undo
history. These helpers deliberately use the frozen preceding schema rather than
calling current execution commands against an older database.
"""

from __future__ import annotations

import json
import re
import sqlite3

from .schema import BundleSchemaMismatch

DOMAIN_FROM_DIGEST = "frisket.schema.v1:7e339a8995763aa40346e7d36187837d"
HISTORY_FLOOR_KEY = "domain_history_floor"


def _require_transaction(db: sqlite3.Connection) -> None:
    if not db.in_transaction:
        raise RuntimeError("domain cutover requires its caller's transaction")


def capture_current_baseline(db: sqlite3.Connection) -> None:
    """Freeze the old effective selection before replacing its resolution rules."""
    _require_transaction(db)
    db.execute(
        "CREATE TEMP TABLE domain_baseline_capture AS "
        "SELECT row_id,column_id,origin_kind,origin_op_id,origin_run_id,"
        "base_producer_id FROM current_cells"
    )


def install_current_baseline(db: sqlite3.Connection) -> None:
    """Move captured references to the new ordinary baseline authority."""
    _require_transaction(db)
    db.execute(
        "INSERT INTO cell_baseline "
        "(row_id,column_id,origin_kind,origin_op_id,origin_run_id,base_producer_id) "
        "SELECT row_id,column_id,origin_kind,origin_op_id,origin_run_id,"
        "base_producer_id FROM domain_baseline_capture"
    )
    db.execute("DROP TABLE domain_baseline_capture")


def retire_unfinished_work(db: sqlite3.Connection) -> None:
    """Fence old publishers without discarding returned results or usage.

    Called once under the upgrade's exclusive write transaction, before new
    admissions. Global queue/accounting retirement remains the workspace/Cloud
    cutover's responsibility; cancelled project receipts prevent redispatch.
    """
    _require_transaction(db)
    for receipt_id, action_kind, status, body in db.execute(
        "SELECT id,action_kind,status,body FROM receipts "
        "WHERE status IN ('queued','running')"
    ).fetchall():
        try:
            receipt = json.loads(body)
            if (
                not isinstance(receipt, dict)
                or receipt.get("receipt_id") != receipt_id
                or receipt.get("status") != status
            ):
                raise ValueError("receipt identity/status does not match")
        except (TypeError, ValueError) as exc:
            raise BundleSchemaMismatch(
                f"Cannot retire malformed receipt {receipt_id}; upgrade rolled back"
            ) from exc
        receipt["status"] = "cancelled"
        receipt["errors"] = [
            {
                "code": "execution_cutover",
                "message": "Unfinished work stopped for a project upgrade. Run the action again.",
                "action_kind": action_kind,
            }
        ]
        db.execute(
            "UPDATE receipts SET status='cancelled',body=? WHERE id=?",
            (json.dumps(receipt), receipt_id),
        )
    db.execute("DELETE FROM effect_checkpoints WHERE state='reserved'")
    db.execute(
        "UPDATE execution_attempts SET state='abandoned' "
        "WHERE state IN ('created','admitted','dispatching')"
    )
    db.execute(
        "UPDATE runs SET status='cancelled',finished_at=datetime('now') "
        "WHERE status='running'"
    )
    db.execute("UPDATE runs SET current_attempt_id=NULL")
    db.execute(
        "UPDATE run_output_generations SET state='sealed',"
        "terminal_disposition='cancelled',sealed_at=datetime('now') "
        "WHERE state IN ('active','staged')"
    )
    db.execute(
        "UPDATE output_column_claims SET status='cancelled',"
        "released_at=datetime('now') WHERE status='active'"
    )
    # Keep completed intake, never resume an old importer into the new domain.
    db.execute(
        "UPDATE import_sessions SET state='kept',updated_at=datetime('now') "
        "WHERE state IN ('active','paused','cancelled')"
    )
    db.execute(
        "UPDATE project_qa_turns SET status='interrupted',"
        "finished_at=datetime('now'),error_summary='Stopped for project upgrade' "
        "WHERE status IN ('running','stopping')"
    )


def archive_history(db: sqlite3.Connection) -> None:
    """Retain old operation facts while starting a fresh navigation history."""
    _require_transaction(db)
    floor = int(db.execute("SELECT COALESCE(MAX(id),0) FROM ops").fetchone()[0])
    db.execute(
        "INSERT INTO meta(key,value) VALUES (?,?)",
        (HISTORY_FLOOR_KEY, str(floor)),
    )
    db.execute("UPDATE ops SET change_order=NULL,publication_closed=1,redo_eligible=0")
    db.execute("UPDATE meta SET value='0' WHERE key='op_cursor'")


# Pinned endpoint: later schema changes must extend this cutover explicitly.
DOMAIN_TO_DIGEST = "frisket.schema.v1:4c227148137867a71f2da439dd609064"


def _execute_ddl(db: sqlite3.Connection, sql: str) -> None:
    """Unlike executescript, preserve the caller's atomic transaction."""
    statement = ""
    for line in sql.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            db.execute(statement)
            statement = ""
    if statement.strip():
        raise RuntimeError("Incomplete domain migration DDL")


def _version_source_cells(db: sqlite3.Connection) -> None:
    if db.execute("SELECT 1 FROM cells WHERE producer_id IS NULL LIMIT 1").fetchone():
        producer = db.execute(
            "INSERT INTO base_cell_producers(stage_id,op_id) "
            "VALUES ('domain:legacy-unattributed',NULL)"
        ).lastrowid
        # A version identity, not invented authorship of an old value.
        db.execute(
            "UPDATE cells SET producer_id=? WHERE producer_id IS NULL", (producer,)
        )
        db.execute(
            "UPDATE current_cells SET base_producer_id=? "
            "WHERE origin_kind='source_cell' AND base_producer_id IS NULL",
            (producer,),
        )
    old_sql = db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='cells'"
    ).fetchone()[0]
    sql, count = re.subn(
        r"UNIQUE\s*\(\s*row_id\s*,\s*column_id\s*\)",
        "UNIQUE (row_id, column_id, producer_id)",
        old_sql,
        flags=re.I,
    )
    if count != 1:
        raise BundleSchemaMismatch("Unexpected source-cell layout; upgrade rolled back")
    sql = re.sub(
        r"^CREATE TABLE(?: IF NOT EXISTS)? cells", "CREATE TABLE domain_cells_new", sql
    )
    objects = [
        row[0]
        for row in db.execute(
            "SELECT sql FROM sqlite_master WHERE tbl_name='cells' "
            "AND type IN ('index','trigger') AND sql IS NOT NULL ORDER BY type,name"
        )
    ]
    db.execute(sql)
    db.execute("INSERT INTO domain_cells_new SELECT * FROM cells")
    db.execute("DROP TABLE cells")
    db.execute("ALTER TABLE domain_cells_new RENAME TO cells")
    for statement in objects:
        db.execute(statement)


def migrate_domain(db: sqlite3.Connection) -> None:
    """Atomically upgrade the last pre-Change schema; never reset user data."""
    from .blob_retention_migration import migrate_blob_derivation_ownership
    from .change_schema import CHANGE_SCHEMA_SQL
    from .current_cells import register_current_cell_functions
    from .domain_document_text_migration import adopt_legacy_document_text
    from .domain_identity_migration import migrate_identity_high_water
    from .keyed_updates import KEYED_UPDATE_SCHEMA
    from .schema import (
        CURRENT_CELL_VALUES_SQL,
        SCHEMA,
        SCHEMA_DIGEST,
        SCHEMA_DIGEST_META_KEY,
    )

    def digest() -> str | None:
        row = db.execute(
            "SELECT value FROM meta WHERE key=?", (SCHEMA_DIGEST_META_KEY,)
        ).fetchone()
        return None if row is None else str(row[0])

    if digest() != DOMAIN_FROM_DIGEST:
        return
    if SCHEMA_DIGEST != DOMAIN_TO_DIGEST:
        raise BundleSchemaMismatch(
            "Domain migration endpoint needs updating for this build"
        )
    if db.in_transaction:
        raise RuntimeError("domain migration owns its transaction")
    foreign_keys = int(db.execute("PRAGMA foreign_keys").fetchone()[0])
    legacy_alter = int(db.execute("PRAGMA legacy_alter_table").fetchone()[0])
    db.execute("PRAGMA foreign_keys=OFF")
    db.execute("PRAGMA legacy_alter_table=ON")
    register_current_cell_functions(db)
    try:
        db.execute("BEGIN IMMEDIATE")
        if digest() != DOMAIN_FROM_DIGEST:
            db.rollback()
            return
        migrate_identity_high_water(db)
        migrate_blob_derivation_ownership(db)
        adopt_legacy_document_text(db)
        _version_source_cells(db)
        capture_current_baseline(db)
        retire_unfinished_work(db)
        db.execute("ALTER TABLE ops ADD COLUMN change_order INTEGER")
        db.execute(
            "ALTER TABLE ops ADD COLUMN publication_closed INTEGER NOT NULL DEFAULT 1 CHECK(publication_closed IN (0,1))"
        )
        db.execute(
            "ALTER TABLE ops ADD COLUMN redo_eligible INTEGER NOT NULL DEFAULT 1 CHECK(redo_eligible IN (0,1))"
        )
        db.execute(
            "ALTER TABLE results ADD COLUMN published INTEGER NOT NULL DEFAULT 0 CHECK(published IN (0,1))"
        )
        db.execute(
            "ALTER TABLE current_cells ADD COLUMN outcome TEXT NOT NULL DEFAULT 'completed' CHECK(outcome IN ('completed','pending','failed','not_completed'))"
        )
        _execute_ddl(db, CHANGE_SCHEMA_SQL)
        _execute_ddl(db, KEYED_UPDATE_SCHEMA)
        install_current_baseline(db)
        # Saved-but-unpublished checkpoints must not become visible.
        db.execute(
            "UPDATE results SET published=1 WHERE EXISTS ("
            "SELECT 1 FROM cell_baseline baseline WHERE baseline.origin_kind='run_result' "
            "AND baseline.origin_run_id=results.run_id AND baseline.row_id=results.row_id "
            "AND baseline.column_id=results.column_id)"
        )
        archive_history(db)
        db.execute("INSERT INTO meta(key,value) VALUES ('change_sequence','0')")
        db.execute("DROP VIEW current_cell_values")
        _execute_ddl(db, CURRENT_CELL_VALUES_SQL)
        for name in (
            "trg_cell_result_heads_require_publishable_result",
            "trg_cell_result_heads_update_publishable_result",
        ):
            db.execute(f"DROP TRIGGER {name}")
            trigger = re.search(
                r"CREATE TRIGGER IF NOT EXISTS " + name + r"\b.*?END;", SCHEMA, re.S
            )
            if trigger is None:
                raise RuntimeError(f"Missing new publication trigger {name}")
            _execute_ddl(db, trigger.group())
        db.execute(
            "UPDATE current_cells SET outcome='failed' WHERE origin_kind='run_result' "
            "AND EXISTS (SELECT 1 FROM results r WHERE r.run_id=current_cells.origin_run_id "
            "AND r.row_id=current_cells.row_id AND r.column_id=current_cells.column_id "
            "AND r.publication_effect NOT IN ('publish_value','publish_null'))"
        )
        db.execute(
            "INSERT INTO cell_authority_revisions(column_id,revision) "
            "SELECT column_id,MAX(retrieval_revision) FROM current_cells GROUP BY column_id"
        )
        violation = db.execute("PRAGMA foreign_key_check").fetchone()
        if violation is not None:
            raise BundleSchemaMismatch(
                f"Domain upgrade found a broken reference: {tuple(violation)}"
            )
        db.execute(
            "UPDATE meta SET value=? WHERE key=?",
            (DOMAIN_TO_DIGEST, SCHEMA_DIGEST_META_KEY),
        )
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.execute(f"PRAGMA legacy_alter_table={legacy_alter}")
        db.execute(f"PRAGMA foreign_keys={foreign_keys}")
