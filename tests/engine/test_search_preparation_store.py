from __future__ import annotations

import pytest

from frisket.engine.jobs.queue import open_queue
from frisket.engine.jobs.search_preparation_store import SearchPreparationStore


def _create(store: SearchPreparationStore, *, receipt: str | None = "receipt-1"):
    return store.create(
        workspace_root="/workspace",
        storage_org_id=None,
        project_id="project",
        trigger_receipt_id=receipt,
        sheet_id=7,
        row_ids=[3, 1, 3],
        source_cutoff=14,
        force_ocr=False,
        plan={"groups": [{"kind": "ocr", "count": 2}], "models": []},
    )


def test_import_receipt_is_idempotent_and_consent_is_exact(tmp_path) -> None:
    queue = open_queue(workspace=tmp_path)
    try:
        store = SearchPreparationStore(queue)
        batch = _create(store)
        assert batch.row_ids == (1, 3)
        assert _create(store).id == batch.id

        decided = store.decide(
            batch.id,
            project_id="project",
            storage_org_id=None,
            plan_hash=batch.plan_hash,
            decision="prepare",
            selected_setup_refs=["engine-setup:rapidocr.local@1"],
        )
        assert decided.decision == "prepare"
        assert decided.selected_setup_refs == ("engine-setup:rapidocr.local@1",)

        with pytest.raises(ValueError, match="plan changed"):
            store.decide(
                batch.id,
                project_id="project",
                storage_org_id=None,
                plan_hash="sha256:stale",
                decision="prepare",
                selected_setup_refs=[],
            )
        with pytest.raises(ValueError, match="already recorded"):
            store.decide(
                batch.id,
                project_id="project",
                storage_org_id=None,
                plan_hash=batch.plan_hash,
                decision="not_now",
                selected_setup_refs=[],
            )
    finally:
        queue.close()


def test_child_is_reserved_before_cursor_advance_and_retry_reuses_it(tmp_path) -> None:
    queue = open_queue(workspace=tmp_path)
    try:
        store = SearchPreparationStore(queue)
        batch = _create(store, receipt=None)
        first = store.reserve_child_and_advance(
            batch.id, expected_cursor=0, child_key="ocr:column-4", kind="ocr"
        )
        replay = store.reserve_child_and_advance(
            batch.id, expected_cursor=0, child_key="ocr:column-4", kind="ocr"
        )
        assert replay.id == first.id
        assert store.get(
            batch.id, project_id="project", storage_org_id=None
        ).cursor == 1

        store.bind_child(first.id, job_id=91, receipt_id="receipt-child")
        [bound] = store.children(batch.id)
        assert (bound.job_id, bound.receipt_id) == (91, "receipt-child")
        with pytest.raises(ValueError, match="already bound"):
            store.bind_child(first.id, job_id=92, receipt_id="receipt-child")
    finally:
        queue.close()


def test_stop_is_durable_without_changing_completed_children(tmp_path) -> None:
    queue = open_queue(workspace=tmp_path)
    try:
        store = SearchPreparationStore(queue)
        batch = _create(store, receipt=None)
        child = store.reserve_child_and_advance(
            batch.id, expected_cursor=0, child_key="native:row-2", kind="native_pdf"
        )
        store.bind_child(child.id, job_id=4, receipt_id="native-receipt")
        stopped = store.request_stop(
            batch.id, project_id="project", storage_org_id=None
        )
        assert stopped.stop_requested_at is not None
        assert store.children(batch.id)[0].receipt_id == "native-receipt"
    finally:
        queue.close()
