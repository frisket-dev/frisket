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
from uuid import uuid4

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
    # Keep the saved answer underneath an ordinary correction too. It remains
    # available for Review and apply_edits=False without duplicating payloads.
    db.execute(
        "INSERT INTO domain_baseline_capture "
        "SELECT current.row_id,current.column_id,"
        "CASE WHEN head.run_id IS NULL THEN 'source_cell' ELSE 'run_result' END,"
        "run.op_id,head.run_id,CASE WHEN head.run_id IS NULL THEN cell.producer_id END "
        "FROM current_cells current "
        "LEFT JOIN cell_result_heads head ON head.row_id=current.row_id AND head.column_id=current.column_id "
        "LEFT JOIN runs run ON run.id=head.run_id "
        "LEFT JOIN cells cell ON cell.row_id=current.row_id AND cell.column_id=current.column_id "
        "WHERE current.origin_kind='manual_edit' AND (head.run_id IS NOT NULL OR cell.row_id IS NOT NULL)"
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
DOMAIN_TO_DIGEST = "frisket.schema.v1:69b2536ad4f6e48a207e49f2b6e0b2e3"


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
    producer = None
    if db.execute("SELECT 1 FROM cells WHERE producer_id IS NULL LIMIT 1").fetchone():
        producer = db.execute(
            "INSERT INTO base_cell_producers(stage_id,op_id) "
            "VALUES ('domain:legacy-unattributed',NULL)"
        ).lastrowid
        # A version identity, not invented authorship of an old value.
        db.execute(
            "UPDATE current_cells SET base_producer_id=? "
            "WHERE origin_kind='source_cell' AND base_producer_id IS NULL",
            (producer,),
        )
    _rebuild_table(db, "cells", fallback_producer_id=producer)


def _retain_output_descriptors(db: sqlite3.Connection) -> None:
    """Recover only frozen declarations, never infer an old schema from today's columns."""
    db.execute("DROP TRIGGER trg_run_output_generations_immutable_declaration")
    db.execute(
        "ALTER TABLE run_output_generations ADD COLUMN output_descriptor_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(output_descriptor_json))"
    )
    # Claims carry the descriptor used for execution, including renamed fields.
    db.execute(
        "CREATE TEMP TABLE domain_output_declarations AS "
        "SELECT generation.run_id,generation.column_id,generation.output_role,claim.details,run.params "
        "FROM run_output_generations generation JOIN runs run ON run.id=generation.run_id "
        "LEFT JOIN output_column_claims claim ON claim.claim_token=generation.claim_token "
        "AND claim.column_id=generation.column_id"
    )
    # A user can swap two physical output names. Rename through a temporary
    # namespace so the unique logical-role constraint holds throughout cutover.
    prefix = f"domain-cutover-{uuid4().hex}:"
    db.execute(
        "UPDATE run_output_generations SET output_role=? || column_id", (prefix,)
    )
    cursor = db.execute("SELECT * FROM domain_output_declarations")
    for run_id, column_id, role, raw_details, raw_params in cursor:
        details = json.loads(raw_details or "{}")
        params = json.loads(raw_params or "{}")
        descriptor = next(
            (
                field
                for field in details.get("output_fields", [])
                if isinstance(field, dict) and field.get("name") == role
            ),
            {},
        )
        descriptor = {
            key: descriptor[key]
            for key in ("column_type", "schema", "format", "semantic_type")
            if key in descriptor
        }
        names = params.get("output_names", {})
        logical = next((key for key, name in names.items() if name == role), role)
        db.execute(
            "UPDATE run_output_generations SET output_role=?,output_descriptor_json=? WHERE run_id=? AND column_id=?",
            (logical, json.dumps(descriptor), run_id, column_id),
        )
    db.execute("DROP TABLE domain_output_declarations")


def _rebuild_table(
    db: sqlite3.Connection, table: str, *, fallback_producer_id: int | None = None
) -> None:
    """Rebuild one known cutover table, retaining IDs, indexes and triggers."""
    from .schema import SCHEMA

    if table not in {"cells", "sheets", "current_cells"}:
        raise ValueError("Unknown domain table rebuild")
    match = re.search(
        r"CREATE TABLE IF NOT EXISTS " + table + r" \(.*?\n\);", SCHEMA, re.S
    )
    if match is None:
        raise RuntimeError(f"Missing current table definition: {table}")
    original_columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
    high_water = db.execute(
        "SELECT seq FROM sqlite_sequence WHERE name=?", (table,)
    ).fetchone()
    objects = [
        row[0]
        for row in db.execute(
            "SELECT sql FROM sqlite_master WHERE tbl_name=? AND type IN ('index','trigger') "
            "AND sql IS NOT NULL ORDER BY type,name",
            (table,),
        )
    ]
    replacement = f"domain_{table}_new"
    db.execute(match.group().replace(f"IF NOT EXISTS {table}", replacement, 1))
    columns = [
        row[1]
        for row in db.execute(f"PRAGMA table_info({replacement})")
        if row[1] in original_columns
    ]
    names = ",".join('"' + column + '"' for column in columns)
    if fallback_producer_id is not None:
        if table != "cells":
            raise ValueError("Only source cells can receive a fallback producer")
        selection = ",".join(
            "COALESCE(producer_id,?)" if column == "producer_id" else '"' + column + '"'
            for column in columns
        )
        db.execute(
            f"INSERT INTO {replacement}({names}) SELECT {selection} FROM {table}",
            (fallback_producer_id,),
        )
    else:
        db.execute(f"INSERT INTO {replacement}({names}) SELECT {names} FROM {table}")
    db.execute(f"DROP TABLE {table}")
    db.execute(f"ALTER TABLE {replacement} RENAME TO {table}")
    for statement in objects:
        db.execute(statement)
    if high_water is not None:
        db.execute(
            "UPDATE sqlite_sequence SET seq=MAX(seq,?) WHERE name=?",
            (high_water[0], table),
        )


def migrate_domain(db: sqlite3.Connection) -> None:
    """Atomically upgrade the last pre-Change schema; never reset user data."""
    from .blob_retention_migration import migrate_blob_derivation_ownership
    from .change_schema import CHANGE_SCHEMA_SQL
    from .column_descriptors import DESCRIPTOR_SCHEMA_SQL
    from .structural_changes import STRUCTURAL_SCHEMA_SQL
    from .copied_results import COPIED_RESULTS_SCHEMA_SQL
    from .domain_alert_migration import migrate_alert_baselines
    from .document_text import DOCUMENT_TEXT_SELECTION_VIEW_SQL
    from .current_cells import register_current_cell_functions
    from .domain_document_text_migration import adopt_legacy_document_text
    from .domain_reapply_migration import migrate_reapplied_results
    from .domain_identity_migration import migrate_identity_high_water
    from .keyed_updates import KEYED_UPDATE_SCHEMA
    from .consumed_sources import CONSUMED_SOURCE_SCHEMA_SQL
    from .purge import PURGE_SCHEMA_SQL
    from .materialized_lineage import migrate_materialized_lineage
    from .domain_view_migration import migrate_unified_views
    from .review_judgments import JUDGMENT_SCHEMA_SQL
    from .schema import (
        CURRENT_CELL_VALUES_SQL,
        UNMODIFIED_CELL_VALUES_SQL,
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
        db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_visible_sheet_name ON sheets(name) WHERE hidden=0"
        )
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
        _retain_output_descriptors(db)
        _rebuild_table(db, "current_cells")
        _execute_ddl(db, COPIED_RESULTS_SCHEMA_SQL)
        _execute_ddl(db, CHANGE_SCHEMA_SQL)
        _execute_ddl(db, DESCRIPTOR_SCHEMA_SQL)
        _execute_ddl(db, STRUCTURAL_SCHEMA_SQL)
        migrate_materialized_lineage(db)
        migrate_alert_baselines(db)
        migrate_unified_views(db)
        _execute_ddl(db, JUDGMENT_SCHEMA_SQL)
        _execute_ddl(db, KEYED_UPDATE_SCHEMA)
        _execute_ddl(db, CONSUMED_SOURCE_SCHEMA_SQL)
        _execute_ddl(db, PURGE_SCHEMA_SQL)
        migrate_reapplied_results(db)
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
        _execute_ddl(db, UNMODIFIED_CELL_VALUES_SQL)
        db.execute("DROP VIEW current_document_text_selections")
        _execute_ddl(db, DOCUMENT_TEXT_SELECTION_VIEW_SQL)
        for name in (
            "trg_cell_result_heads_require_publishable_result",
            "trg_cell_result_heads_update_publishable_result",
            "trg_run_output_generations_immutable_declaration",
        ):
            db.execute(f"DROP TRIGGER IF EXISTS {name}")
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
