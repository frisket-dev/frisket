"""Normal preview confirmation and receipt accounting across the real job host."""

import asyncio
import threading
from dataclasses import replace

import pytest

from frisket.ai.llm import ModelRouter
from frisket.ai.models.metadata import ModelCallMeta
from frisket.contracts.http.action_preview_runs import ActionPreviewStatusResponse
from frisket.engine.runner import MapRunner
from frisket.engine.store.receipts import ReceiptStore
from frisket.engine.store.runs import RunResultStore
from frisket.server.services.action_preview_runs import ActionPreviewRunService
from frisket.server.workspace import Workspace
from tests.deterministic_time import controlled_time
from tests.preview.test_accounted_action_preview import _Adapter, _outputs
from tests.preview.test_preview_inmemory import _seed, _summarize_action
from tests.engine.test_ocr_read import _png


def _fixture(tmp_path, adapter):
    router = ModelRouter(keys={"anthropic": "test-key"}, cache=None, cache_mode="off")
    router._adapters["anthropic"] = adapter
    workspace = Workspace(
        tmp_path / "workspace", router=router, enable_local_model_pull=False
    )
    workspace.create("Paid preview", project_id="paid")
    project = workspace.get("paid")
    sheet, rows, _ = _seed(project, ["alpha", "beta"])
    service = ActionPreviewRunService(workspace)
    return service, project, _summarize_action(sheet, row_ids=rows)


def _wait(service, preview_id):
    result = None

    def finished():
        nonlocal result
        result = service.get_preview("paid", preview_id).payload
        return result["status"] != "running"

    with controlled_time(timeout=10) as clock:
        clock.wait_until(finished, message="paid preview did not finish")
    ActionPreviewStatusResponse.model_validate(result)
    return result


def _confirm(service, body):
    quote = service.start_preview("paid", body)
    assert quote.status_code == 402, quote.payload
    details = quote.payload["error"]["details"]
    assert details["estimate"]
    return {**body, "confirmation": details["promise_set_hash"]}


@pytest.mark.parametrize("mode", ["success", "row_error", "terminal_error"])
def test_paid_preview_ordinary_confirmation_and_terminal_facts(
    tmp_path, monkeypatch, mode
):
    monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "0")
    adapter = _Adapter(invalid=mode == "row_error")
    service, project, body = _fixture(tmp_path, adapter)
    before = _outputs(project)
    try:
        confirmed = _confirm(service, body)
        assert ReceiptStore(project).count() == 0
        assert not adapter.calls
        refused = service.start_preview("paid", {**body, "confirmation": "wrong"})
        assert refused.status_code == 402
        assert ReceiptStore(project).count() == 0

        preview = MapRunner.preview

        async def preview_with_row_authorization(self, *args, **kwargs):
            result = await preview(self, *args, **kwargs)
            attempt = project.db.execute(
                "SELECT id FROM execution_attempts WHERE state='dispatching'"
            ).fetchone()
            assert attempt is not None
            project.db.execute(
                "INSERT INTO attempt_row_authorizations "
                "(attempt_id,row_id,quoted_quantity) VALUES (?,?,?)",
                (attempt["id"], 1, "1"),
            )
            project.db.commit()
            if mode == "terminal_error":
                raise RuntimeError("terminal preview failure")
            return result

        monkeypatch.setattr(MapRunner, "preview", preview_with_row_authorization)
        started = service.start_preview("paid", confirmed)
        assert started.status_code == 202, started.payload
        result = _wait(service, started.payload["preview_id"])
        assert result["status"] == ("error" if mode == "terminal_error" else "done")
        accounting = result["accounting"]
        assert accounting["status"] == (
            "failed" if mode == "terminal_error" else "completed"
        )
        assert accounting["model_call_count"] == len(adapter.calls) > 0
        assert accounting["cost_actual"] == pytest.approx(0.01 * len(adapter.calls))
        assert accounting["elapsed_ms"] >= 0
        receipt = ReceiptStore(project).parsed_by_id(accounting["receipt_id"])
        assert receipt.run_id is None and receipt.outputs == []
        assert sum(item["model_call_count"] for item in receipt.provider_use) == len(
            adapter.calls
        )
        assert _outputs(project) == before
        states = project.db.execute("SELECT state FROM execution_attempts").fetchall()
        assert [row[0] for row in states] == [
            "halted" if mode == "terminal_error" else "effected"
        ]
        outcomes = project.db.execute(
            "SELECT row_id, terminal_outcome FROM attempt_row_authorizations "
            "ORDER BY row_id"
        ).fetchall()
        assert [(row["row_id"], row["terminal_outcome"]) for row in outcomes] == [
            (1, None)
        ]
        if mode == "row_error":
            assert all(
                cells["summary"].get("error")
                for cells in result["result"]["rows"].values()
            )
    finally:
        service._registry.shutdown()
        project.close()


def test_cancel_joins_returned_paid_call_before_receipt_settlement(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "0")
    entered = threading.Event()
    release = threading.Event()

    class Adapter(_Adapter):
        async def complete(self, request, client):
            entered.set()
            await asyncio.to_thread(release.wait)
            return await super().complete(request, client)

    adapter = Adapter()
    service, project, body = _fixture(tmp_path, adapter)
    before = _outputs(project)
    settled = []

    class Settlement:
        def settle_action_receipt(self, *, project, project_id, receipt_id):
            receipt = ReceiptStore(project).parsed_by_id(receipt_id)
            calls = RunResultStore(project).model_calls(receipt_id=receipt_id)
            assert receipt.status == "cancelled"
            assert calls and release.is_set()
            assert (
                project.db.execute(
                    "SELECT COUNT(*) FROM execution_attempts WHERE state='dispatching'"
                ).fetchone()[0]
                == 0
            )
            settled.append(receipt_id)

    service._workspace.direct_action_receipt_settlement_port = Settlement()
    try:
        started = service.start_preview("paid", _confirm(service, body))
        assert started.status_code == 202, started.payload
        preview_id = started.payload["preview_id"]
        assert entered.wait(10)
        service.cancel_preview("paid", preview_id)
        pending = service.get_preview("paid", preview_id).payload
        assert pending["status"] == "running"
        assert pending["accounting"]["status"] == "running"
        assert not settled
        release.set()
        result = _wait(service, preview_id)
        assert result["status"] == "cancelled" and "result" not in result
        assert result["accounting"]["status"] == "cancelled"
        assert result["accounting"]["cost_actual"] > 0
        assert settled == [result["accounting"]["receipt_id"]]
        assert _outputs(project) == before
    finally:
        release.set()
        service._registry.shutdown()
        project.close()


def test_scratch_teardown_error_survives_receipt_settlement_failure(tmp_path):
    from frisket.engine.sandbox.shim import SandboxTeardownError

    service, project, _ = _fixture(tmp_path, _Adapter())
    plan, _source, router, composition, execution_context = service.prepare_ocr_scratch(
        "paid",
        _png(),
        {
            "engine": "anthropic/claude-haiku-4-5",
            "filename": "scan.png",
        },
    )
    fatal = SandboxTeardownError("scratch sandbox teardown failed")

    async def fail_after_teardown(_context):
        raise fatal

    class FailedSettlement:
        def settle_action_receipt(self, **_kwargs):
            raise RuntimeError("settlement unavailable after fatal teardown")

    service._workspace.direct_action_receipt_settlement_port = FailedSettlement()
    try:
        estimate = service.scratch_estimate("paid", plan)
        started = service.start_scratch_preview(
            "paid",
            replace(plan, run=fail_after_teardown),
            confirmation=estimate["promise_set_hash"],
            router=router,
            composition=composition,
            execution_context=execution_context,
        )
        assert started.status_code == 202, started.payload
        result = _wait(service, started.payload["preview_id"])
        assert result["status"] == "error"
        assert result["accounting"]["status"] == "failed"
        with pytest.raises(SandboxTeardownError, match="scratch sandbox teardown"):
            service._registry.start("paid", 0, lambda *_args: None)
    finally:
        service._registry.shutdown()
        project.close()


@pytest.mark.parametrize("mode", ["success", "unknown_cost_error", "fatal"])
def test_actual_ocr_preview_keeps_scope_finalizer_facts(tmp_path, monkeypatch, mode):
    from frisket.engine.sandbox.shim import SandboxTeardownError
    from frisket.ops.ocr_engines import OcrEngines

    monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "0")
    service, project, _ = _fixture(tmp_path, _Adapter())
    sheet = project.add_sheet("Scans")
    column = project.add_column(sheet, "scan", type="image")
    digest = project.add_blob(_png(), filename="scan.png", mime="image/png")
    [row] = project.add_rows(
        sheet,
        [{"scan": {"blob": digest, "filename": "scan.png", "mime": "image/png"}}],
        {"scan": column},
    )
    body = {
        "action_id": "media.ocr",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet, "row_ids": [row]},
        "params": {"source": "scan", "engine": "anthropic/claude-haiku-4-5"},
        "idempotency_key": "ocr-preview",
    }

    async def recognize(self, engine, pages, ctx, *, usage, **kwargs):
        usage["calls"] += 1
        usage["model_calls"] = [
            ModelCallMeta.provider_call(
                capability="ocr",
                engine=engine,
                provider="anthropic",
                provider_kind="chat_api",
                model_ids=["claude-haiku-4-5"],
                credential_source="local",
                provider_reported_cost_usd=None,
                provider_cost_usd=None if mode == "unknown_cost_error" else 0.03,
                units={"input_pages": 1},
                cost_source="provider_reported",
                warnings=[],
                duration_ms=5,
            ).as_dict()
        ]
        if mode == "unknown_cost_error":
            raise ValueError("returned provider response cannot be decoded")
        if mode == "fatal":
            raise SandboxTeardownError("owned provider teardown failed")
        return [{"text": "Paid OCR", "blocks": []}]

    monkeypatch.setattr(OcrEngines, "run_engine_on_pages", recognize)
    if mode == "fatal":

        class FailedSettlement:
            def settle_action_receipt(self, **kwargs):
                raise RuntimeError("settlement unavailable after fatal teardown")

        service._workspace.direct_action_receipt_settlement_port = FailedSettlement()
    before = _outputs(project)
    try:
        confirmed = _confirm(service, body)
        if mode == "success":
            from frisket.server.services.action_previews import ActionPreviewService

            # OCR Compare quotes through the ordinary estimate endpoint, then
            # submits that exact token to preview, not to a separate compare API.
            quote = ActionPreviewService(service._workspace).estimate("paid", body)
            assert quote["estimate"]["promise_set_hash"] == confirmed["confirmation"]
            assert quote["estimate"]["requires_confirmation"]
        started = service.start_preview("paid", confirmed)
        assert started.status_code == 202, started.payload
        result = _wait(service, started.payload["preview_id"])
        assert result["status"] == ("error" if mode == "fatal" else "done")
        summary = result["accounting"]
        assert summary["status"] == ("failed" if mode == "fatal" else "completed")
        assert summary["model_call_count"] == 1
        assert summary["cost_actual"] == (
            None if mode == "unknown_cost_error" else 0.03
        )
        receipt = ReceiptStore(project).parsed_by_id(summary["receipt_id"])
        assert receipt.provider_use[0]["service"] == "ocr"
        assert receipt.provider_use[0]["cost_actual"] == summary["cost_actual"]
        assert _outputs(project) == before
        if mode == "fatal":
            with pytest.raises(SandboxTeardownError):
                service._registry.start("paid", 0, lambda *_args: None)
    finally:
        service._registry.shutdown()
        project.close()


def test_late_cancel_during_settlement_keeps_completed_result_and_snapshot(
    tmp_path, monkeypatch
):
    import json
    from dataclasses import replace
    from types import SimpleNamespace
    from frisket.execution.provider import ExecutionCompositionContext

    entered = threading.Event()
    release = threading.Event()
    monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "0")
    service, project, body = _fixture(tmp_path, _Adapter())
    context = SimpleNamespace(
        state=SimpleNamespace(
            execution_composition_context=replace(
                ExecutionCompositionContext.direct(),
                edition_snapshot={"funding": "trusted-preview"},
            )
        )
    )

    class Settlement:
        def settle_action_receipt(self, *, project, project_id, receipt_id):
            stored = project.db.execute(
                "SELECT status, edition_run_context FROM receipts WHERE id=?",
                (receipt_id,),
            ).fetchone()
            assert stored["status"] == "completed"
            assert json.loads(stored["edition_run_context"]) == {
                "funding": "trusted-preview"
            }
            entered.set()
            assert release.wait(10)

    service._workspace.direct_action_receipt_settlement_port = Settlement()
    try:
        quote = service.start_preview("paid", body, request_context=context)
        assert quote.status_code == 402
        confirmed = {
            **body,
            "confirmation": quote.payload["error"]["details"]["promise_set_hash"],
        }
        started = service.start_preview("paid", confirmed, request_context=context)
        assert started.status_code == 202, started.payload
        preview_id = started.payload["preview_id"]
        assert entered.wait(10)
        service.cancel_preview("paid", preview_id)
        release.set()
        result = _wait(service, preview_id)
        assert result["status"] == "done" and result["result"]["rows"]
        assert result["accounting"]["status"] == "completed"
    finally:
        release.set()
        service._registry.shutdown()
        project.close()
