"""A returned conversion is recovered without submitting its document again."""

from copy import deepcopy
import asyncio

import pytest
from fastapi.testclient import TestClient

from frisket.actions.system import typed_action_for_request
from frisket.ai.llm import ModelRouter
from frisket.engine.executor.actions import _default_map_runner_factory
from frisket.engine.executor.map_rows_action import build_typed_map_rows_plan
from frisket.engine.executor.document_convert import AdmittedDocumentConverter
from frisket.engine.jobs.ports import JobHandlerContext
from frisket.engine.store.media_blobs import media_cell
from frisket.engine.store.receipts import ReceiptStore
from frisket.engine.store.runs import RunResultStore
from frisket.ops.integrations import datalab
from frisket.server.app import create_app
from tests.engine.test_row_effect_checkpoint_boundary_fixes import _age_silent_attempt


class InterruptedAfterConversion(BaseException):
    pass


def test_queued_document_returned_checkpoint_recovers_without_provider_repeat(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("DATALAB_API_KEY", "synthetic-recovery-key")
    calls = []

    async def convert(http, api_key, content, filename, mime, **kwargs):
        calls.append((content, filename, mime))
        accounting = datalab._accepted_submission_accounting(
            {"request_check_url": "https://datalab.test/job/recover"},
            capability="document.convert",
            credential_source=kwargs["credential_source"],
        )
        kwargs["on_accepted"](accounting)
        return {"markdown": "# Recovered document", "page_count": 3}, 0.03

    monkeypatch.setattr(datalab, "datalab_convert", convert)
    with TestClient(
        create_app(tmp_path / "ws", run_status_grace_seconds=3600)
    ) as client:
        project_id = client.post("/api/projects", json={"name": "Documents"}).json()[
            "id"
        ]
        workspace = client.app.state.workspace
        project = workspace.get(project_id)
        sheet = project.add_sheet("Documents")
        column = project.add_column(sheet, "document", type="file")
        digest = project.add_blob(
            b"%PDF-1.4 source", filename="source.pdf", mime="application/pdf"
        )
        (row,) = project.add_rows(
            sheet,
            [
                {
                    "document": media_cell(
                        digest, mime="application/pdf", filename="source.pdf"
                    )
                }
            ],
            {"document": column},
        )
        body = {
            "action_id": "media.to_markdown",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet},
            "params": {"source": "document", "engine": "datalab"},
            "output_names": {"markdown": "Renamed document"},
            "idempotency_key": "document-returned-recovery",
        }
        endpoint = f"/api/projects/{project_id}/actions/v1/run"
        quote = client.post(endpoint, json=body)
        assert quote.status_code == 402, quote.text
        body["confirmation"] = quote.json()["errors"][0]["details"]["promise_set_hash"]
        queued = client.post(endpoint, json=body)
        assert queued.status_code == 200, queued.text
        queued = queued.json()
        job = workspace.queue.get(queued["job_id"])
        payload = {**deepcopy(job.payload), "job_id": job.id}
        handler = workspace.registry.get("project.run")
        context = JobHandlerContext.from_claimed_job(trusted_org_id=None)

        def crash(*args, **kwargs):
            raise InterruptedAfterConversion()

        with monkeypatch.context() as patch:
            patch.setattr(
                RunResultStore, "consume_returned_row_effect_checkpoint", crash
            )
            with pytest.raises(InterruptedAfterConversion):
                handler(deepcopy(payload), context)
        run_id = queued["run_id"]
        checkpoint = RunResultStore(project).row_effect_checkpoint(run_id, row)
        assert checkpoint["state"] == "returned"
        returned = checkpoint["response"]["Renamed document"]
        assert returned["value"] == "# Recovered document"
        assert returned["row_file_calls"][0]["document_read"]["blob_hash"] == digest
        assert len(calls) == 1
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM results WHERE run_id=?", (run_id,)
            ).fetchone()[0]
            == 0
        )
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM evidence_links WHERE run_id=?", (run_id,)
            ).fetchone()[0]
            == 0
        )
        _age_silent_attempt(project, checkpoint["authorized_attempt_id"])

        def forbidden(*args, **kwargs):
            pytest.fail("A recovered returned conversion must not call its provider")

        monkeypatch.setattr(datalab, "datalab_convert", forbidden)
        created = []
        original_init = AdmittedDocumentConverter.__init__

        def fresh_owner(self, *args, **kwargs):
            original_init(self, *args, **kwargs)
            created.append(self)

        monkeypatch.setattr(AdmittedDocumentConverter, "__init__", fresh_owner)
        # The generic recovery path replaces the abandoned attempt under the
        # retained output claim; raw queue redelivery is not writer recovery.
        plan = build_typed_map_rows_plan(
            project, typed_action_for_request(body), _allow_existing_outputs=True
        )
        claim = project.db.execute(
            "SELECT claim_token FROM output_column_claims WHERE run_id=?", (run_id,)
        ).fetchone()[0]
        runner = _default_map_runner_factory(project, ModelRouter())
        progress = asyncio.run(
            runner.run(
                deepcopy(payload["spec"]),
                program=plan.program,
                confirmed=True,
                resume_run_id=run_id,
                claim_token=claim,
            )
        )
        assert progress.halted_code is None and progress.completed == 1
        handler(deepcopy(payload), context)
        assert created  # Fresh invocation, not the first converter's in-memory facts.
        assert len(calls) == 1
        assert RunResultStore(project).row_effect_checkpoint(run_id, row) is None
        receipt = ReceiptStore(project).parsed_by_id(queued["receipt_id"])
        assert receipt.status == "completed", receipt.errors
        reads = [
            item.ref
            for item in receipt.evidence
            if item.ref.get("kind") == "document_convert_read"
        ]
        assert len(reads) == 1
        assert reads[0]["document_read"]["blob_hash"] == digest
        output = next(
            c["id"] for c in project.columns(sheet) if c["name"] == "Renamed document"
        )
        assert project.get_values(sheet, output)[row] == "# Recovered document"
        links = project.db.execute(
            "SELECT column_id, receipt_id FROM evidence_links WHERE run_id=? AND link_role='source_provenance'",
            (run_id,),
        ).fetchall()
        assert [(link["column_id"], link["receipt_id"]) for link in links] == [
            (output, queued["receipt_id"])
        ]
        (fact,) = RunResultStore(project).model_calls(run_id)
        assert fact["provider_cost_usd"] == 0.03
        assert fact["attempt_id"] == checkpoint["authorized_attempt_id"]
        assert receipt.provider_use[0]["model_call_count"] == 1
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM execution_attempts WHERE run_id=?", (run_id,)
            ).fetchone()[0]
            == 2
        )
