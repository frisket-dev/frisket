from __future__ import annotations

from contextlib import closing

import pytest

from frisket.contracts.action import ActionSpec, Receipt, ReceiptEvidence, ReceiptIO
from frisket.engine.executor import run_action_spec
from frisket.engine.executor.action_support import _params_hash
from frisket.engine.store import Project
from frisket.engine.store.receipts import ReceiptStore


@pytest.mark.parametrize("kind", ["import.files", "import.pdf"])
def test_legacy_import_cannot_replay_a_receipt_when_source_is_missing(tmp_path, kind):
    source = {"kind": "file", "path": str(tmp_path / "removed-source.pdf")}
    request = {
        "schema_version": "frisket.action.v2",
        "kind": kind,
        "capabilities": ["project:write"],
        "params": {
            "sheet_name": "Historical import",
            "mode": "create_sheet",
            **({"files": [source]} if kind == "import.files" else {"source": source}),
        },
        "idempotency_key": "historical-import",
    }
    request_hash = _params_hash(ActionSpec.model_validate(request))
    receipt = Receipt(
        receipt_id="historical-receipt",
        project_id="p",
        action_id="historical-action",
        action_kind=kind,
        idempotency_key=request["idempotency_key"],
        params_hash=request_hash,
        status="completed",
        inputs=[ReceiptIO(name="source", ref={"request_hash": request_hash})],
        evidence=[
            ReceiptEvidence(
                ref={"kind": "imported_blob", "row_index": 1, "hash": "a" * 64}
            )
        ],
    )
    with closing(Project.create(tmp_path / "project")) as project:
        store = ReceiptStore(project)
        store.insert_completed(receipt)
        original_body = store.body_by_id(receipt.receipt_id)

        result = run_action_spec(project, request, project_id="p")

        assert result.status == "failed"
        assert result.errors[0].code == "invalid_action_request"
        assert result.receipt_id is None
        assert store.body_by_id(receipt.receipt_id) == original_body
        assert project.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 1
        for table in ("sheets", "rows", "columns", "blobs", "ops"):
            assert (
                project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
            )
