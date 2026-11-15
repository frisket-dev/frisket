"""One-time data cutover from layered values to ordered project Changes.

The migration retains payload authorities and captures only their winning
references. Old operations remain inspectable but cannot become executable Undo
history. These helpers deliberately use the frozen preceding schema rather than
calling current execution commands against an older database.
"""

from __future__ import annotations

import json
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
