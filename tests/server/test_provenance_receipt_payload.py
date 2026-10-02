"""Receipt summaries stay bounded by metadata, not stored evidence size."""

import json
import sqlite3

from frisket.engine.store import Project
from frisket.engine.store.receipts import ReceiptStore
from frisket.server.provenance_payloads import provenance_manifest_payload


def test_provenance_summary_does_not_read_receipt_bodies(tmp_path):
    project = Project.create(tmp_path / "receipts.frisket")
    body = json.dumps({"evidence": "long evidence " * 100_000})
    try:
        for index in range(3):
            project.db.execute(
                "INSERT INTO receipts(id,action_kind,status,body,created_at) "
                "VALUES (?, 'map.extract', 'completed', ?, '2026-10-02')",
                (f"receipt-{index}", body),
            )
        project.db.commit()

        def metadata_only(action, table, column, database, source):
            if action == sqlite3.SQLITE_READ and (table, column) == (
                "receipts", "body"
            ):
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        project.db.set_authorizer(metadata_only)
        try:
            payload = provenance_manifest_payload(
                project, "receipts", receipts_offset=1, receipts_limit=1
            )
        finally:
            project.db.set_authorizer(None)
        assert payload["receipts"] == [{
            "receipt_id": "receipt-1", "action_kind": "map.extract",
            "status": "completed", "run_id": None, "created_at": "2026-10-02"
        }]
        assert payload["receipts_page"]["total"] == 3
        assert ReceiptStore(project).body_by_id("receipt-1") == body
    finally:
        project.close()
