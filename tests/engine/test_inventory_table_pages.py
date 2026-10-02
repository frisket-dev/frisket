from contextlib import closing
import json

import pytest

from frisket.actions.system import typed_action_for_request
from frisket.contracts.action import Receipt
from frisket.engine.executor.file_inventory_read import FileInventoryAdmission
from frisket.engine.executor.map_rows_action import typed_request_hash
from frisket.engine.executor.table_action import run_inventory_table_page
from frisket.engine.store import Project
from frisket.engine.store.import_inventory import ImportInventory
from frisket.engine.store.import_sessions import ImportSessionConflict
from frisket.engine.store.receipts import ReceiptStore
from frisket.engine.store.streaming_import import StreamingSheetWriter


def test_inventory_pages_share_action_receipt_and_resume_without_rereading(
    tmp_path, monkeypatch
):
    with (
        closing(Project.create(tmp_path / "p.frisket")) as project,
        ImportInventory(tmp_path / "inventory.db") as inventory,
    ):
        digest = project.blob_store.put(b"original")
        inventory.append(
            {
                "logical_path": f"{i}.pdf",
                "mime": "application/pdf",
                "sha256": digest,
                "size": 8,
                "kind": "files",
            }
            for i in range(5)
        )
        inventory.seal()
        bound = typed_action_for_request(
            {
                "action_id": "import.files",
                "scope": {"kind": "project"},
                "sheet_name": "Documents",
                "params": {"inventory_ref": "admitted"},
                "idempotency_key": "import-once",
            }
        )
        ReceiptStore(project).insert_running(
            Receipt(
                receipt_id="receipt-inventory",
                project_id="project",
                action_id="action-inventory",
                action_kind="import.files",
                idempotency_key="import-once",
                params_hash=typed_request_hash(bound),
                status="running",
            )
        )

        def page(after, authority="first"):
            return run_inventory_table_page(
                project,
                "project",
                bound,
                admission=FileInventoryAdmission(
                    "admitted", inventory, project.blob_store, after, limit=2
                ),
                session_id="session-inventory",
                writer_authority=authority,
                reserved_action_id="action-inventory",
                reserved_receipt_id="receipt-inventory",
            )

        first = page(0)
        assert first.state == "active"
        assert first.cursor == first.committed_rows == 2
        assert project.row_count(first.sheet_id) == 2
        with pytest.raises(ImportSessionConflict):
            page(0)
        StreamingSheetWriter.resume_session(
            project,
            session_id=first.id,
            writer_authority="replacement",
            expected_cursor=2,
        )
        with pytest.raises(ImportSessionConflict):
            page(2)
        second = page(2, "replacement")
        final = page(4, "replacement")
        assert second.sheet_id == first.sheet_id == final.sheet_id
        assert final.state == "completed"
        assert final.cursor == final.committed_rows == 5
        assert project.row_count(final.sheet_id) == 5
        assert project.db.execute("SELECT count(*) FROM receipts").fetchone()[0] == 1
        assert project.db.execute("SELECT count(*) FROM ops").fetchone()[0] == 1
        assert (
            project.db.execute("SELECT count(*) FROM base_cell_producers").fetchone()[0]
            == 1
        )
        stored = ReceiptStore(project).find_by_id("receipt-inventory")
        assert stored.status == "completed"
        assert len(stored.body) < 5000
        artifacts = project.db.execute(
            "SELECT source_row_id,external_ref_json FROM source_artifacts ORDER BY id"
        ).fetchall()
        assert len(artifacts) == 5
        assert len({row["source_row_id"] for row in artifacts}) == 5
        assert [
            json.loads(row["external_ref_json"])["ordinal"] for row in artifacts
        ] == list(range(1, 6))
        assert [
            json.loads(row["external_ref_json"])["logical_path"] for row in artifacts
        ] == [f"{i}.pdf" for i in range(5)]

        def no_replay_read(**kwargs):
            pytest.fail("completed replay must precede inventory access")

        monkeypatch.setattr(inventory, "page", no_replay_read)
        assert page(0).state == "completed"
