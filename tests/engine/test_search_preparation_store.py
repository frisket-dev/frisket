from __future__ import annotations

import pytest

from frisket.engine.jobs.queue import open_queue
from frisket.engine.jobs.search_preparation_store import SearchPreparationStore


def _create(
    store: SearchPreparationStore,
    *,
    receipt: str | None = "receipt-1",
    retry_terminal: bool = False,
):
    return store.create(
        storage_org_id=None,
        project_id="project",
        trigger_receipt_id=receipt,
        sheet_id=7,
        row_ids=[3, 1, 3],
        force_ocr=False,
        plan={
            "source_cutoff": 14,
            "groups": [{"kind": "ocr", "count": 2}],
            "models": [],
        },
        retry_terminal=retry_terminal,
    )


def test_import_receipt_is_idempotent_and_consent_is_exact(tmp_path) -> None:
    queue = open_queue(workspace=tmp_path)
    try:
        store = SearchPreparationStore(queue, workspace_root="/workspace")
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
            decided_by="alice",
        )
        assert decided.decision == "prepare"
        assert decided.selected_setup_refs == ("engine-setup:rapidocr.local@1",)
        assert decided.decision_at is not None
        assert decided.decided_by == "alice"

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
        replayed = store.decide(
            batch.id,
            project_id="project",
            storage_org_id=None,
            plan_hash=batch.plan_hash,
            decision="prepare",
            selected_setup_refs=["engine-setup:rapidocr.local@1"],
            decided_by="bob",
        )
        assert replayed.decision_at == decided.decision_at
        assert replayed.decided_by == "alice"
    finally:
        queue.close()


def test_pending_import_is_internal_until_atomically_hydrated(tmp_path) -> None:
    queue = open_queue(workspace=tmp_path)
    try:
        store = SearchPreparationStore(queue, workspace_root="/workspace")
        pending = store.create_pending_import(
            storage_org_id=None,
            project_id="project",
            receipt_id="receipt-pending",
            import_ref="import-pending",
        )
        assert pending.sheet_id is None
        assert pending.plan is None
        assert store.list_recent(project_id="project", storage_org_id=None) == []
        assert (
            store.for_receipt(
                storage_org_id=None,
                project_id="project",
                receipt_id="receipt-pending",
            )
            is None
        )
        store.mark_settled(pending.id, project_id="project", storage_org_id=None)

        hydrated = store.hydrate_pending_import(
            pending.id,
            project_id="project",
            storage_org_id=None,
            sheet_id=7,
            plan={"groups": [], "models": [], "items": []},
        )
        assert hydrated.sheet_id == 7
        assert hydrated.settled_at is None
        assert hydrated.plan == {"groups": [], "models": [], "items": []}
        assert (
            store.for_receipt(
                storage_org_id=None,
                project_id="project",
                receipt_id="receipt-pending",
            ).id
            == pending.id
        )
    finally:
        queue.close()


def test_pending_materialization_is_internal_until_exact_job_is_hydrated(
    tmp_path,
) -> None:
    queue = open_queue(workspace=tmp_path)
    try:
        store = SearchPreparationStore(queue, workspace_root="/workspace")
        pending = store.create_pending_materialization(
            storage_org_id=None,
            project_id="project",
            job_id=41,
            sheet_id=7,
            row_ids=[3],
        )
        assert pending.materialization_job_id == 41
        assert pending.row_ids == (3,)
        assert pending.plan is None
        later = store.create_pending_materialization(
            storage_org_id=None,
            project_id="project",
            job_id=42,
            sheet_id=7,
            row_ids=[3],
        )
        assert (
            store.pending_materialization(
                project_id="project",
                storage_org_id=None,
                job_id=42,
                sheet_id=7,
                row_ids=[3],
            ).id
            == later.id
        )
        assert store.list_recent(project_id="project", storage_org_id=None) == []
        assert (
            store.pending_materialization(
                project_id="project",
                storage_org_id=None,
                job_id=41,
                sheet_id=7,
                row_ids=[3],
            ).id
            == pending.id
        )

        hydrated = store.hydrate_pending_materialization(
            pending.id,
            project_id="project",
            storage_org_id=None,
            plan={"groups": [], "models": [], "items": []},
        )
        assert hydrated.plan == {"groups": [], "models": [], "items": []}
        assert (
            store.pending_materialization(
                project_id="project",
                storage_org_id=None,
                job_id=41,
                sheet_id=7,
                row_ids=[3],
            )
            is None
        )
    finally:
        queue.close()


def test_child_reservation_is_idempotent(tmp_path) -> None:
    queue = open_queue(workspace=tmp_path)
    try:
        store = SearchPreparationStore(queue, workspace_root="/workspace")
        batch = _create(store, receipt=None)
        first = store.reserve_child(batch.id, child_key="ocr:column-4", kind="ocr")
        replay = store.reserve_child(batch.id, child_key="ocr:column-4", kind="ocr")
        assert replay.id == first.id

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
        store = SearchPreparationStore(queue, workspace_root="/workspace")
        batch = _create(store, receipt=None)
        child = store.reserve_child(
            batch.id, child_key="native:row-2", kind="native_pdf"
        )
        store.bind_child(child.id, job_id=4, receipt_id="native-receipt")
        stopped = store.request_stop(
            batch.id, project_id="project", storage_org_id=None
        )
        assert stopped.stop_requested_at is not None
        assert stopped.settled_at is not None
        assert store.children(batch.id)[0].receipt_id == "native-receipt"
        with pytest.raises(ValueError, match="stopped"):
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


def test_stop_racing_action_enqueue_refuses_late_child_binding(tmp_path) -> None:
    queue = open_queue(workspace=tmp_path)
    try:
        store = SearchPreparationStore(queue, workspace_root="/workspace")
        batch = _create(store, receipt=None)
        child = store.reserve_child(
            batch.id, child_key="native:row-2", kind="native_pdf"
        )
        store.request_stop(batch.id, project_id="project", storage_org_id=None)

        assert not store.bind_child(child.id, job_id=4, receipt_id="native-receipt")
        [unbound] = store.children(batch.id)
        assert unbound.job_id is None and unbound.receipt_id is None
        with pytest.raises(ValueError, match="stopped"):
            store.reserve_child(batch.id, child_key="ocr:column-4", kind="ocr")
    finally:
        queue.close()


def test_not_now_stays_active_until_eligible_children_are_coordinated(tmp_path) -> None:
    queue = open_queue(workspace=tmp_path)
    try:
        store = SearchPreparationStore(queue, workspace_root="/workspace")
        batch = _create(store, receipt=None)
        decided = store.decide(
            batch.id,
            project_id="project",
            storage_org_id=None,
            plan_hash=batch.plan_hash,
            decision="not_now",
            selected_setup_refs=[],
        )
        assert decided.settled_at is None
        assert [row.id for row in store.list_active_page(storage_org_id=None)] == [
            batch.id
        ]
    finally:
        queue.close()


def test_active_scan_retires_settled_intent_and_decision_reopens_it(tmp_path) -> None:
    queue = open_queue(workspace=tmp_path)
    try:
        store = SearchPreparationStore(queue, workspace_root="/workspace")
        batch = _create(store, receipt=None)
        assert [row.id for row in store.list_active_page(storage_org_id=None)] == [
            batch.id
        ]
        store.mark_settled(batch.id, project_id="project", storage_org_id=None)
        assert store.list_active_page(storage_org_id=None) == []

        reopened = store.decide(
            batch.id,
            project_id="project",
            storage_org_id=None,
            plan_hash=batch.plan_hash,
            decision="prepare",
            selected_setup_refs=[],
        )
        assert reopened.settled_at is None
        assert [row.id for row in store.list_active_page(storage_org_id=None)] == [
            batch.id
        ]
    finally:
        queue.close()


def test_explicit_retry_reuses_active_attempt_but_replaces_stopped_attempt(
    tmp_path,
) -> None:
    queue = open_queue(workspace=tmp_path)
    try:
        store = SearchPreparationStore(queue, workspace_root="/workspace")
        first = _create(store, receipt=None)
        assert _create(store, receipt=None).id == first.id
        store.request_stop(first.id, project_id="project", storage_org_id=None)

        retry = _create(store, receipt=None, retry_terminal=True)
        assert retry.id != first.id
        assert _create(store, receipt=None).id == retry.id
    finally:
        queue.close()
