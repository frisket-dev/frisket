from __future__ import annotations

import json
from contextlib import closing

import pytest

from frisket.engine.jobs.worker import Worker
from frisket.engine.store import Project
from frisket.engine.store.cell_writes import EditCellWrite, insert_edits
from frisket.engine.store.document_text import ensure_document_text_role
from frisket.engine.store.receipts import ReceiptStore
from frisket.engine.store.result_generations import ResultGenerationStore
from frisket.engine.store.runs import RunResultStore
from frisket.server.services.action_runs import ActionRunService
from frisket.server.workspace import Workspace
from test_native_pdf_text_action import _pdf_bytes, _request, _source


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
        row_ids = [row_id]
        if publication == "first_flush":
            original_value = project.get_values(sheet_id, source_id, [row_id])[row_id]
            row_ids += project.add_rows(
                sheet_id, [{"source": original_value}], {"source": source_id}
            )

    workspace = Workspace(root, enable_local_model_pull=False)
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
