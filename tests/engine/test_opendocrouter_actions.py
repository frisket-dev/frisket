"""Public action execution: admission, actual HTTP adapter, evidence and cost."""

from contextlib import closing
import asyncio
import io
import json
from types import SimpleNamespace
from pathlib import Path

import httpx
import pytest
from PIL import Image
from pypdf import PdfReader, PdfWriter

from frisket.engine.executor import run_action_spec
from frisket.engine.store import Project
from frisket.engine.store.media_blobs import media_cell, owned_media_metadata_document
from frisket.engine.store.runs import RunResultStore
from frisket.execution.attempt import run_attempt_receipts
from tests.ops.test_opendocrouter import ENGINE, response


@pytest.mark.parametrize("action", ["media.ocr", "media.to_markdown"])
@pytest.mark.parametrize(
    "scenario",
    [
        "success",
        "partial",
        "ledger_failure",
        "lost_response",
        "repriced",
        "new_model",
        "unknown_price",
    ],
)
def test_paid_document_action(tmp_path, monkeypatch, action, scenario):
    monkeypatch.setenv("OPEN_DOC_ROUTER_API_KEY", "odr-key")
    monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "0")
    calls = []
    engine = ENGINE
    if scenario in {"new_model", "unknown_price"}:
        from frisket.opendocrouter_catalog import current_catalog, DocumentCatalog
        from dataclasses import replace

        catalog = current_catalog()
        model = replace(
            catalog.models[0],
            id="example/future-parser",
            name="Future parser",
            max_charge_per_page_usd=None
            if scenario == "unknown_price"
            else catalog.models[0].max_charge_per_page_usd,
        )
        monkeypatch.setattr(
            "frisket.opendocrouter_catalog._catalog",
            DocumentCatalog(catalog.price_version, (*catalog.models, model)),
        )
        engine = model.engine
    if scenario == "ledger_failure":

        def fail_persist(*args):
            raise RuntimeError("ledger unavailable")

        monkeypatch.setattr(
            "frisket.ops.opendocrouter.persist_hosted_accepted_accounting", fail_persist
        )

    def handler(request):
        calls.append(request)
        assert request.url.path == "/v1/parse"
        payload = json.loads(request.content)
        assert payload["layout"] is (action == "media.ocr")
        assert payload["model"] == engine.removeprefix("opendocrouter/")
        if scenario == "lost_response":
            raise httpx.ReadTimeout("response lost", request=request)
        result = response()
        result["model"] = payload["model"]
        result["pages"][0]["layout"] = {
            "status": "ok",
            "elements": [
                {
                    "type": "text",
                    "lines": [0, 0],
                    "boxes": [{"x": 0.1, "y": 0.2, "w": 0.5, "h": 0.1}],
                }
            ],
        }
        if scenario == "partial":
            result["status"] = "partial"
            result["pages"][0]["status"] = "error"
        return httpx.Response(200, json=result)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    router = SimpleNamespace(client=client)
    with closing(
        Project.create(tmp_path / "test.frisket", name="Documents")
    ) as project:
        sheet = project.add_sheet("Sources")
        column = project.add_column(sheet, "doc", type="file")
        data = io.BytesIO()
        if action == "media.ocr":
            Image.new("RGB", (100, 200), "white").save(data, format="PNG")
            filename, mime = "scan.png", "image/png"
            probe = {"kind": "image", "width": 100, "height": 200}
        else:
            writer = PdfWriter()
            writer.add_blank_page(width=100, height=200)
            writer.write(data)
            filename, mime = "scan.pdf", "application/pdf"
            probe = {"kind": "document", "pages": 1}
        blob = project.add_blob(
            data.getvalue(),
            filename=filename,
            mime=mime,
            metadata=owned_media_metadata_document(probe=probe),
        )
        [row] = project.add_rows(
            sheet,
            [{"doc": media_cell(blob, mime=mime, filename=filename)}],
            {"doc": column},
        )
        spec = {
            "action_id": action,
            "scope": {"kind": "sheet_rows", "sheet_id": sheet},
            "params": {"source": "doc", "engine": engine},
            "idempotency_key": "odr",
            "output_names": {"text": "text", "blocks": "boxes"}
            if action == "media.ocr"
            else {"markdown": "markdown"},
        }
        gate = run_action_spec(project, spec, router=router, project_id="odr")
        assert gate.status == "needs_confirmation", gate.errors
        assert calls == []
        assert any(c["field"] == "cost" for c in gate.errors[0].details["claims"])
        if scenario == "repriced":
            from frisket.opendocrouter_catalog import current_catalog, DocumentCatalog
            from dataclasses import replace

            catalog = current_catalog()
            monkeypatch.setattr(
                "frisket.opendocrouter_catalog._catalog",
                DocumentCatalog(
                    catalog.price_version,
                    tuple(
                        replace(m, max_charge_per_page_usd=0.5)
                        if m.engine == engine
                        else m
                        for m in catalog.models
                    ),
                ),
            )
        result = run_action_spec(
            project,
            {**spec, "confirmation": gate.errors[0].details["promise_set_hash"]},
            router=router,
            project_id="odr",
        )
        if scenario == "repriced":
            assert result.status == "needs_confirmation", result.errors
            assert calls == []
            asyncio.run(client.aclose())
            return
        assert result.run_id is not None, result.errors
        assert len(calls) == 1
        if scenario == "ledger_failure":
            assert result.errors[0].code == "external_effect_reconciliation_required"
            asyncio.run(client.aclose())
            return
        [fact] = RunResultStore(project).model_calls(result.run_id)
        assert fact["provider"] == "opendocrouter"
        if scenario == "lost_response":
            assert result.errors[0].code == "external_effect_reconciliation_required"
            assert fact["provider_cost_usd"] is None
            asyncio.run(client.aclose())
            return
        assert fact["provider_cost_usd"] == 0.00397
        assert fact["epoch_id"] is not None
        [attempt] = run_attempt_receipts(project, result.run_id)
        assert attempt["settlement"]["charge_usd"] == "0.00397", attempt
        assert attempt["settlement"]["charge_authority"] == "provider_usage"
        if scenario in {"success", "new_model", "unknown_price"}:
            assert result.status == "completed", result.errors
            name = "text" if action == "media.ocr" else "markdown"
            target = project.db.execute(
                "SELECT id FROM columns WHERE sheet_id=? AND name=?", (sheet, name)
            ).fetchone()[0]
            assert project.get_values(sheet, target, row_ids=[row])[row] == (
                "Page 1\n\nText" if action == "media.ocr" else "# Page 1\n\nText"
            )
            assert fact["column_id"] == target
        else:
            assert result.status != "completed" or result.errors
            assert not any(
                e.code == "external_effect_reconciliation_required"
                for e in result.errors
            )
    asyncio.run(client.aclose())


def test_paid_ocr_compare_uses_same_adapter(tmp_path, monkeypatch):
    from frisket.ai.llm import ModelRouter
    from frisket.execution.provider import (
        ExecutionCompositionContext,
        open_execution_composition,
    )
    from frisket.server.services.scratch_ocr import paid_ocr_scratch_plan
    from tests.preview.test_paid_ocr_scratch import _RunContext

    monkeypatch.setenv("OPEN_DOC_ROUTER_API_KEY", "odr-key")
    monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "0")
    data = io.BytesIO()
    Image.new("RGB", (100, 200), "white").save(data, format="PNG")
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=response())

    router = ModelRouter(use_env_keys=False)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    router._client = client
    with closing(
        Project.create(tmp_path / "compare.frisket", name="Compare")
    ) as project:
        composition = open_execution_composition(
            project, router, ExecutionCompositionContext.direct()
        )
        plan, _ = paid_ocr_scratch_plan(
            project,
            media_bytes=data.getvalue(),
            payload={"engine": ENGINE, "filename": "page.png"},
            composition=composition,
        )
        context = _RunContext(project, router)
        result = asyncio.run(plan.run(context))
        assert result.rows[0]["text"]["value"] == "Page 1\n\nText"
        assert len(calls) == 1
        assert context.calls[0]["provider"] == "opendocrouter"
        assert context.calls[0]["provider_cost_usd"] == 0.00397
    asyncio.run(client.aclose())


@pytest.mark.parametrize("missing_layout", [False, True])
def test_searchable_pdf_from_live_layout_response(
    tmp_path, monkeypatch, missing_layout
):
    """Replay the successful real scan, retaining the complete public action path."""
    monkeypatch.setenv("OPEN_DOC_ROUTER_API_KEY", "odr-key")
    monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "0")
    captured = json.loads(
        (
            Path(__file__).parents[1] / "fixtures/opendocrouter/ocr_layout.json"
        ).read_text()
    )
    if missing_layout:
        captured["pages"][0]["layout"] = {"status": "error", "elements": []}

    def handler(request):
        assert json.loads(request.content)["layout"] is True
        return httpx.Response(200, json=captured)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    router = SimpleNamespace(client=client)
    source = io.BytesIO()
    Image.new("RGB", (1200, 650), "white").save(source, format="PDF", resolution=200)
    assert not PdfReader(io.BytesIO(source.getvalue())).pages[0].extract_text()
    with closing(
        Project.create(tmp_path / "searchable.frisket", name="Scan")
    ) as project:
        sheet = project.add_sheet("Scan")
        column = project.add_column(sheet, "source", type="file")
        blob = project.add_blob(
            source.getvalue(),
            filename="invoice.pdf",
            mime="application/pdf",
            metadata=owned_media_metadata_document(
                probe={"kind": "document", "pages": 1}
            ),
        )
        [row] = project.add_rows(
            sheet,
            [
                {
                    "source": media_cell(
                        blob, filename="invoice.pdf", mime="application/pdf"
                    )
                }
            ],
            {"source": column},
        )
        spec = {
            "action_id": "media.ocr",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet},
            "params": {
                "source": "source",
                "engine": "opendocrouter/" + captured["model"],
                "searchable_pdf": True,
            },
            "idempotency_key": "searchable",
            "output_names": {"text": "text", "blocks": "boxes", "pdf": "searchable"},
        }
        gate = run_action_spec(project, spec, router=router, project_id="test")
        assert gate.status == "needs_confirmation", gate.errors
        result = run_action_spec(
            project,
            {**spec, "confirmation": gate.errors[0].details["promise_set_hash"]},
            router=router,
            project_id="test",
        )
        columns = {
            r["name"]: r["id"]
            for r in project.db.execute(
                "SELECT id, name FROM columns WHERE sheet_id=?", (sheet,)
            )
        }
        text = project.get_values(sheet, columns["text"], row_ids=[row])[row]
        assert "INV-1042" in text and "120.00" in text
        value = project.get_values(sheet, columns["searchable"], row_ids=[row]).get(row)
        if missing_layout:
            assert value is None
            error = project.db.execute(
                "SELECT error FROM results WHERE run_id=? AND row_id=? AND column_id=?",
                (result.run_id, row, columns["searchable"]),
            ).fetchone()[0]
            assert "no usable text positions" in error
        else:
            assert result.status == "completed", result.errors
            with project.materialize_blob(value["blob"]) as path:
                extracted = PdfReader(path).pages[0].extract_text()
            assert "INV-1042" in extracted and "120.00" in extracted
        [attempt] = run_attempt_receipts(project, result.run_id)
        assert attempt["settlement"]["charge_usd"] == "0.001549"
    asyncio.run(client.aclose())
