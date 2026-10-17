from __future__ import annotations

import json
from contextlib import closing

import pytest

from frisket.engine.jobs.worker import Worker
from frisket.engine.jobs.ports import WorkerPorts
from frisket.engine.store import Project
from frisket.engine.store.cell_writes import EditCellWrite, insert_edits
from frisket.engine.store.document_text import (
    DocumentTextPublicationRefused,
    ensure_document_text_role,
)
from frisket.engine.store.receipts import ReceiptStore
from frisket.engine.store.result_generations import ResultGenerationStore
from frisket.engine.store.runs import RunResultStore
from frisket.server.services.action_runs import ActionRunService
from frisket.server.workspace import Workspace
from frisket.team.security.secrets import encrypt_secret
from test_native_pdf_text_action import _pdf_bytes, _request, _source
from test_document_text_authority import _project_with_selection, _OTHER_BLOB
from helpers import run_writer_authority_fixture


@pytest.mark.parametrize(
    "publication", ["first_flush", "replacement_seal", "unknown_after_flush"]
)
def test_source_changed_publication_refusal_closes_exact_worker(
    tmp_path, monkeypatch, publication
):
    root = tmp_path / "workspace"
    root.mkdir()
    project_id = "document-publication"
    path = root / f"{project_id}.frisket"
    with closing(Project.create(path)) as project:
        sheet_id, source_id, row_id, _ = _source(
            project, tmp_path, pdf=_pdf_bytes("ORIGINAL DOCUMENT")
        )
        changed_blob = project.add_blob(
            _pdf_bytes("REPLACED DOCUMENT"),
            filename="replacement.pdf",
            mime="application/pdf",
        )
        project.set_provider_key(
            provider="anthropic",
            encrypted=encrypt_secret("returned-call-fixture"),
            hint="fixture",
            spend_cap_micro=1_000_000,
        )
        row_ids = [row_id]
        if publication == "first_flush":
            original_value = project.get_values(sheet_id, source_id, [row_id])[row_id]
            row_ids += project.add_rows(
                sheet_id, [{"source": original_value}], {"source": source_id}
            )

    settled = []

    class SettlementObserver:
        def settle_run(self, *, project, run_id, terminal_status, **_kwargs):
            settled.append(
                (
                    terminal_status,
                    RunResultStore(project).get_run(run_id)["cost_actual"],
                )
            )

        def settle_action_receipt(self, **_kwargs):
            pytest.fail("PDF text preparation must settle its run")

    workspace = Workspace(
        root,
        enable_local_model_pull=False,
        worker_ports=WorkerPorts(settlement_port=SettlementObserver()),
    )
    service = ActionRunService(workspace)
    declare = ResultGenerationStore.declare

    def declare_document_text(store, run_id, column_id, **kwargs):
        # Bind the producer's newly created Text column before its immutable
        # generation policy is captured, including the first create-mode run.
        ensure_document_text_role(
            store.project, source_column_id=source_id, text_column_id=column_id
        )
        return declare(store, run_id, column_id, **kwargs)

    monkeypatch.setattr(ResultGenerationStore, "declare", declare_document_text)
    request = _request(sheet_id, row_id, key="first-document-text")
    request["scope"]["row_ids"] = row_ids
    first = service.run_action(project_id, request)
    assert first.status_code == 200, first.payload
    assert first.payload["status"] == "queued"
    project = workspace.get(project_id)
    text_id = next(
        col["id"] for col in project.columns(sheet_id) if col["name"] == "prepared text"
    )
    worker = Worker(workspace.queue, workspace.registry, worker_id="document-terminal")

    def execute_target(job_id):
        for _ in range(8):
            if workspace.queue.get(job_id).attempts:
                return
            assert worker.run_once()
        pytest.fail("target document job did not run")

    prior_head = None
    if publication == "replacement_seal":
        execute_target(first.payload["job_id"])
        assert workspace.queue.get(first.payload["job_id"]).status == "done", (
            workspace.queue.get(first.payload["job_id"])
        )
        assert (
            workspace.queue.get(first.payload["job_id"]).result["status"] == "completed"
        )
        prior_head = first.payload["run_id"]
        request = {
            **request,
            "idempotency_key": "replacement-document-text",
            "replace_existing": True,
            "expected_output_columns": {"prepared text": text_id},
        }
        response = service.run_action(project_id, request)
        assert response.status_code == 200, response.payload
        assert response.payload["status"] == "queued"
    else:
        response = first
    run_id = response.payload["run_id"]
    receipt_id = response.payload["receipt_id"]
    # The terminal receipt must retain already-recorded external-use facts.
    # No provider is contacted by this local PDF fixture.
    receipts = ReceiptStore(project)
    stored = receipts.find_by_id(receipt_id).parsed()
    provider_use = [{"provider": "recorded-provider", "cost_usd": "0.125"}]
    assert receipts.update_if_status_in(
        stored.model_copy(update={"provider_use": provider_use}), {"queued"}
    )
    actual = RunResultStore.publish_result_batch
    changed = False
    published_rows = []

    def change_source(store, changed_row_id):
        nonlocal changed
        assert not store.db.in_transaction
        store.db.execute("BEGIN IMMEDIATE")
        op_id = store.project.append_op("edit", commit=False)
        insert_edits(
            store.db,
            op_id=op_id,
            edits=[
                EditCellWrite(
                    changed_row_id,
                    source_id,
                    {"blob": changed_blob, "mime": "application/pdf"},
                )
            ],
        )
        store.db.commit()
        changed = True

    def publish(store, target_run, batch, **kwargs):
        if target_run != run_id or changed:
            return actual(store, target_run, batch, **kwargs)
        if publication == "first_flush":
            for item in batch:
                item["model_calls"] = [
                    {
                        "id": f"returned-pdf-{run_id}-{item['row_id']}",
                        "fact_version": "frisket.model-call-fact.v1",
                        "capability": "ocr",
                        "engine": "fixture",
                        "provider": "anthropic",
                        "provider_kind": "test",
                        "credential_source": "project_key",
                        "provider_cost_usd": 0.125,
                    }
                ]
        if publication == "first_flush" and published_rows:
            change_source(store, batch[0]["row_id"])
        result = actual(store, target_run, batch, **kwargs)
        published_rows.extend(item["row_id"] for item in batch)
        if publication == "unknown_after_flush":
            raise RuntimeError("unexpected failure after a durable result effect")
        if publication == "replacement_seal":
            change_source(store, row_id)
        return result

    monkeypatch.setattr(RunResultStore, "publish_result_batch", publish)
    try:
        execute_target(response.payload["job_id"])
        if publication == "unknown_after_flush":
            # The same worker must not turn an unknown post-effect exception
            # into the known publication refusal's abort/settlement path.
            job = workspace.queue.get(response.payload["job_id"])
            assert job.status == "failed" and job.result is None
            run = RunResultStore(project).get_run(run_id)
            assert run["status"] == "running" and run["current_attempt_id"] is not None
            receipt = receipts.find_by_id(receipt_id).parsed()
            assert receipt.status == "queued" and receipt.provider_use == provider_use
            assert (
                project.db.execute(
                    "SELECT status FROM output_column_claims WHERE claim_token=?",
                    (f"output-claim:{receipt_id}",),
                ).fetchone()[0]
                == "active"
            )
            assert (
                ResultGenerationStore(project).bindings_for_run(run_id)[0].state
                == "active"
            )
            return
        assert changed, workspace.queue.get(response.payload["job_id"])
        if publication == "first_flush":
            assert len(published_rows) == 1  # A real head preceded the refused row.
        job = workspace.queue.get(response.payload["job_id"])
        assert job.status == "done", job.error
        assert job.result["status"] == "failed"
        assert job.result["action_result"]["errors"][0]["code"] == "source_changed"
        run = RunResultStore(project).get_run(run_id)
        assert run["status"] == "failed" and run["current_attempt_id"] is None
        assert run["halted_code"] == "source_changed"
        if publication == "first_flush":
            assert len(RunResultStore(project).model_calls(run_id)) == 2
            assert run["cost_actual"] == pytest.approx(0.25)
            assert project.provider_spend_state("anthropic").spent_micro == 250_000
            assert settled == [("failed", 0.25)]
        receipt = receipts.find_by_id(receipt_id).parsed()
        assert receipt.status == "failed" and receipt.provider_use == provider_use
        assert receipt.errors[0].code == "source_changed"
        assert (
            project.db.execute(
                "SELECT status FROM output_column_claims WHERE claim_token=?",
                (f"output-claim:{receipt_id}",),
            ).fetchone()[0]
            == "failed"
        )
        bindings = ResultGenerationStore(project).bindings_for_run(run_id)
        assert len(bindings) == 1 and bindings[0].state == "sealed"
        assert bindings[0].terminal_disposition == "failed"
        head = project.db.execute(
            "SELECT run_id FROM cell_result_heads WHERE row_id=? AND column_id=?",
            (row_id, text_id),
        ).fetchone()
        assert (None if head is None else head[0]) == prior_head
        if publication == "first_flush":
            assert (
                project.db.execute(
                    "SELECT COUNT(*) FROM cell_result_heads WHERE column_id=?",
                    (text_id,),
                ).fetchone()[0]
                == 0
            )
        op = project.db.execute(
            "SELECT undo_info FROM ops WHERE id=?", (run["op_id"],)
        ).fetchone()
        assert text_id in json.loads(op[0])["unpublished_generation_columns"]
        ResultGenerationStore(project).rebuild_heads([text_id])
        rebuilt = project.db.execute(
            "SELECT run_id FROM cell_result_heads WHERE row_id=? AND column_id=?",
            (row_id, text_id),
        ).fetchone()
        assert (None if rebuilt is None else rebuilt[0]) == prior_head
    finally:
        workspace.queue.close()
        project.close()


def test_refused_checkpoint_publication_preserves_returned_accounting(tmp_path):
    project, sheet_id, row_id, source_id, text_id = _project_with_selection(tmp_path)
    try:
        project.set_provider_key(
            provider="anthropic",
            encrypted=encrypt_secret("checkpoint-call-fixture"),
            hint="fixture",
            spend_cap_micro=1_000_000,
        )
        store = RunResultStore(project)
        op_id = project.append_op("media.ocr", {"action_kind": "media.ocr"})
        run_id = store.start_run(
            op_id, sheet_id, "media.ocr", row_ids=[row_id], total_rows=1
        )
        authority = run_writer_authority_fixture(
            project, run_id, output_column_ids={text_id}
        )
        writer_kwargs = {
            key: value
            for key, value in authority.kwargs().items()
            if key != "authorized_attempt_id"
        }
        ResultGenerationStore(project).declare(
            run_id,
            text_id,
            output_role="text",
            compatibility_key="document-text",
            write_mode="create",
            claim_token=authority.claim_token,
        )
        ref_id = project.db.execute("SELECT id FROM prepared_content_refs").fetchone()[
            0
        ]
        call = {
            "id": "returned-checkpoint-call",
            "fact_version": "frisket.model-call-fact.v1",
            "capability": "ocr",
            "engine": "fixture",
            "provider": "anthropic",
            "provider_kind": "test",
            "credential_source": "project_key",
            "provider_cost_usd": 0.125,
        }
        batch = [
            {
                "row_id": row_id,
                "column_id": text_id,
                "prepared_ref_id": ref_id,
                "publication_effect": "publish_value",
                "model_calls": [call],
            }
        ]
        assert store.reserve_row_effect_checkpoint(
            "returned-document",
            run_id=run_id,
            row_id=row_id,
            action_kind="media.ocr",
            identity="document-source",
            authorized_attempt_id=authority.writer_attempt_id,
            **writer_kwargs,
        )
        assert store.complete_row_effect_checkpoint(
            "returned-document",
            run_id=run_id,
            row_id=row_id,
            action_kind="media.ocr",
            identity="document-source",
            batch=batch,
            replay_response={"text": {"prepared_ref_id": ref_id, "cost": 0.0}},
            **writer_kwargs,
        ) == pytest.approx(0.125)
        project.db.execute("BEGIN IMMEDIATE")
        edit_op = project.append_op("edit", commit=False)
        insert_edits(
            project.db,
            op_id=edit_op,
            edits=[
                EditCellWrite(
                    row_id, source_id, {"blob": _OTHER_BLOB, "mime": "application/pdf"}
                )
            ],
        )
        project.db.commit()

        for caller_owns_transaction in (False, True):
            if caller_owns_transaction:
                project.db.execute("BEGIN IMMEDIATE")
            with pytest.raises(DocumentTextPublicationRefused):
                store.publish_result_batch(
                    run_id,
                    batch,
                    checkpoint=("returned-document", row_id, "document-source"),
                    checkpoint_action_kind="media.ocr",
                    **writer_kwargs,
                )
            assert project.db.in_transaction is caller_owns_transaction
            if caller_owns_transaction:
                project.db.rollback()
            assert len(store.model_calls(run_id)) == 1
            assert store.get_run(run_id)["cost_actual"] == pytest.approx(0.125)
            assert project.provider_spend_state("anthropic").spent_micro == 125_000
            checkpoint = store.row_effect_checkpoint(run_id, row_id)
            assert (
                checkpoint["state"] == "returned" and checkpoint["accounting_persisted"]
            )
            assert checkpoint["authorized_attempt_id"] == authority.writer_attempt_id
            assert (
                project.db.execute(
                    "SELECT COUNT(*) FROM results WHERE run_id=?", (run_id,)
                ).fetchone()[0]
                == 0
            )
    finally:
        project.close()
