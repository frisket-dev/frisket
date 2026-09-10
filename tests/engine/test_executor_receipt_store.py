from __future__ import annotations

from pathlib import Path
from typing import get_args

import pytest

from frisket.contracts.action import Receipt, ReceiptIO
from frisket.engine.store import Project
from frisket.engine.store.receipts import (
    FINISHED_RECEIPT_STATUSES,
    ReceiptStatus,
    ReceiptStore,
    StoredReceipt,
)
from frisket.engine.store.runs import RunResultStore


ROOT = Path(__file__).resolve().parents[2]


def _receipt(
    receipt_id: str = "receipt_1",
    *,
    status: str = "running",
) -> Receipt:
    return Receipt(
        receipt_id=receipt_id,
        project_id="proj_receipts",
        action_id="act_1",
        action_kind="map.template",
        idempotency_key="idem_1",
        params_hash="sha256:params",
        status=status,
        inputs=[
            ReceiptIO(
                name="idempotency",
                ref={"kind": "map_template_idempotency_reservation"},
            )
        ],
    )


@pytest.mark.parametrize(
    ("status", "is_finished"),
    [
        ("queued", False),
        ("running", False),
        ("completed", True),
        ("partial", True),
        ("failed", True),
        ("cancelled", True),
    ],
)
def test_receipt_status_vocabulary_and_finished_inserts(
    tmp_path: Path,
    status: str,
    is_finished: bool,
) -> None:
    expected_vocabulary = {
        "queued",
        "running",
        "completed",
        "partial",
        "failed",
        "cancelled",
    }
    assert set(get_args(ReceiptStatus)) == expected_vocabulary
    assert (status in FINISHED_RECEIPT_STATUSES) is is_finished

    project = Project.create(tmp_path / "status-vocabulary.frisket", name="Receipts")
    store = ReceiptStore(project)
    receipt = _receipt(f"receipt_{status}", status=status).model_copy(
        update={"idempotency_key": f"idem_{status}"}
    )
    if is_finished:
        store.insert_finished(receipt)
        assert store.status_by_id(receipt.receipt_id) == status
    else:
        with pytest.raises(ValueError, match="expected completed"):
            store.insert_finished(receipt)


def test_receipt_store_round_trips_sorted_receipt_body_and_guarded_updates(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "receipts.frisket", name="Receipts")
    store = ReceiptStore(project)
    receipt = _receipt()

    store.insert(receipt)
    stored = store.find_by_idempotency_key("idem_1")

    assert isinstance(stored, StoredReceipt)
    assert stored["id"] == "receipt_1"
    assert stored.parsed() == receipt
    assert '"action_id": "act_1"' in stored.body

    completed = receipt.model_copy(
        update={
            "status": "completed",
            "outputs": [ReceiptIO(name="out", ref={"kind": "template_output"})],
        }
    )
    assert store.update_body_status(completed, require_status="queued") is False
    assert store.find_by_id("receipt_1")["status"] == "running"
    assert store.update_body_status(completed, require_status="running") is True

    updated = store.find_by_action_kind_and_idempotency_key(
        action_kind="map.template",
        key="idem_1",
    )
    assert updated is not None
    assert updated["status"] == "completed"
    assert updated.parsed().outputs[0].ref == {"kind": "template_output"}
    assert (
        store.update_if_status_in(
            updated.parsed().model_copy(update={"status": "failed"}),
            {"queued", "running"},
        )
        is False
    )
    assert (
        store.update_if_status_in(
            updated.parsed().model_copy(update={"status": "failed"}),
            {"completed", "partial"},
        )
        is True
    )
    assert store.delete_if_status("receipt_1", "queued") is False
    assert store.delete_if_status("receipt_1", "completed") is False
    assert store.delete_if_status("receipt_1", "failed") is True
    assert store.find_by_id("receipt_1") is None


def test_receipt_store_finished_insert_and_body_fetch_helpers(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "finished_receipts.frisket", name="Receipts")
    store = ReceiptStore(project)
    sheet_id = project.add_sheet("Receipt Runs")
    op_id = project.append_op("fixture", {"kind": "receipt-store"})
    run_id = RunResultStore(project).start_run(
        op_id,
        sheet_id,
        "test.receipt_store",
        total_rows=0,
    )
    second_run_id = RunResultStore(project).start_run(
        op_id,
        sheet_id,
        "test.receipt_store",
        total_rows=0,
    )

    completed = _receipt("receipt_completed", status="completed").model_copy(
        update={"run_id": run_id, "idempotency_key": "idem_completed"}
    )
    partial = _receipt("receipt_partial", status="partial").model_copy(
        update={"run_id": run_id, "idempotency_key": "idem_partial"}
    )
    failed = _receipt("receipt_failed", status="failed").model_copy(
        update={"run_id": second_run_id, "idempotency_key": "idem_failed"}
    )

    store.insert_completed(completed)
    store.insert_partial(partial)
    store.insert_failed(failed)

    assert store.parsed_by_id("receipt_completed") == completed
    assert store.body_by_id("receipt_partial") is not None
    assert store.parsed_by_id("receipt_partial") == partial
    assert store.status_by_id("receipt_failed") == "failed"
    assert store.latest_id_for_run(run_id) == "receipt_partial"
    assert (
        store.latest_for_run_statuses(run_id, {"completed", "partial"}).id
        == "receipt_partial"
    )
    assert [
        Receipt.model_validate_json(body).receipt_id
        for body in store.bodies_for_run_status(run_id, "completed")
    ] == ["receipt_completed"]
    assert [
        Receipt.model_validate_json(body).receipt_id
        for body in store.bodies_for_action_status(
            action_kind="map.template",
            status="partial",
        )
    ] == ["receipt_partial"]

    with pytest.raises(ValueError):
        store.insert_completed(partial)
    with pytest.raises(ValueError):
        store.insert_finished(_receipt("receipt_running", status="running"))


def test_action_families_do_not_write_receipts_with_raw_sql() -> None:
    action_family_root = ROOT / "src/frisket/engine/executor/action_families"
    guarded_sources = [
        *sorted(action_family_root.rglob("*.py")),
        ROOT / "src/frisket/engine/executor/embedding_export.py",
    ]
    raw_write_markers = (
        "INSERT INTO receipts",
        "UPDATE receipts",
        "DELETE FROM receipts",
    )
    offenders: list[str] = []
    for source_path in guarded_sources:
        # rule19: closure fence — no raw receipts SQL in action_families; expected-empty architecture grep retained under marker precedent
        source = source_path.read_text(encoding="utf-8")
        for marker in raw_write_markers:
            if marker in source:
                offenders.append(f"{source_path.relative_to(ROOT)}: {marker}")

    assert offenders == []
