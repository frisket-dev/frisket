"""Document→markdown conversion through its typed, admitted capability.

Exercises local reads directly and remote conversion / durable provenance via
real typed execution, receipts, and writer admission. No live network/API:
only the sandbox worker, sidecar POST, and Datalab client are stubbed.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import closing
from pathlib import Path

import pytest

from frisket.actions.types import ColumnRef, Row, RowError
from frisket.engine.executor import document_convert as module
from frisket.engine.executor.document_convert import (
    AdmittedDocumentConverter,
    document_convert_reads,
)
from frisket.engine.executor import run_action_spec
from frisket.engine.store.runs import RunResultStore
from frisket.engine.sandbox.shim import SandboxResult
from frisket.engine.store import Project
from frisket.engine.store.media_blobs import media_cell
from frisket.execution.credential_use import CredentialUseContext
from frisket.ops.base import OpContext, RecipeInvocationHalt

HTML = "<html><body><h1>Quarterly Report</h1><p>Revenue doubled.</p></body></html>"


@pytest.fixture
def project(tmp_path):
    with closing(Project.create(tmp_path / "d.frisket", name="d")) as p:
        yield p


def _seed(project, value, *, coltype="text", name="doc"):
    sheet = project.add_sheet("data")
    cid = project.add_column(sheet, name, type=coltype)
    row_id = project.add_rows(sheet, [{name: value}], {name: cid})[0]
    return sheet, cid, row_id


def _sources(name, cid, value, column_type="text"):
    return {name: {"column_id": cid, "column_type": column_type, "value": value}}


def _action(sheet, engine="markitdown"):
    return {
        "action_id": "media.to_markdown",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet},
        "params": {"source": "doc", "engine": engine},
        "output_names": {"markdown": "converted"},
        "idempotency_key": "document-domain-test",
    }


def _execute(project, sheet, engine="markitdown", **kwargs):
    action = _action(sheet, engine)
    result = run_action_spec(project, action, project_id="document-test", **kwargs)
    if result.status == "needs_confirmation":
        action["confirmation"] = result.errors[0].details["promise_set_hash"]
        result = run_action_spec(project, action, project_id="document-test", **kwargs)
    return result


def _receipt(project, result):
    from frisket.contracts.action import Receipt

    body = project.db.execute(
        "SELECT body FROM receipts WHERE id=?", (result.receipt_id,)
    ).fetchone()[0]
    return Receipt.model_validate_json(body)


def _reads(project, result):
    return [
        item.ref
        for item in _receipt(project, result).evidence
        if item.ref.get("kind") == "document_convert_read"
    ]


def _run_convert(
    project,
    *,
    name,
    cid,
    sheet,
    row_id,
    value,
    engine=None,
    extras=None,
    http=None,
    cancelled=None,
    limits=None,
    credential_ctx=None,
):
    ctx = OpContext(
        project=project,
        http=http,
        extras=extras if extras is not None else {"row_id": row_id, "run_id": 1},
        credential_use_context=credential_ctx,
        execution_limits=limits,
    )
    conv = AdmittedDocumentConverter(ctx, engine=engine, cancelled=cancelled)
    row = Row({name: value})

    async def run():
        bound = conv.bind_row(
            row,
            sheet_id=sheet,
            row_id=row_id,
            sources=_sources(
                name,
                cid,
                value,
                project.db.execute(
                    "SELECT type FROM columns WHERE id=?", (cid,)
                ).fetchone()[0],
            ),
            ctx=ctx,
        )
        try:
            return await bound.convert(row, ColumnRef(name))
        finally:
            await conv.aclose()

    return asyncio.run(run()), conv


# -- markitdown / trafilatura (local, free) -----------------------------------


def _stub_markitdown(monkeypatch, markdown="# Quarterly Report\n"):
    seen = {}

    async def fake_sandbox(argv, *, policy, stdin_data=None, should_cancel=None):
        payload = json.loads(stdin_data)
        seen["payload"] = payload
        seen["read"] = policy.confine.read
        Path(payload["out"]).write_text(json.dumps({"markdown": markdown}))
        return SandboxResult(0, "", "")

    monkeypatch.setattr(module, "run_sandboxed", fake_sandbox)
    return seen


def test_markitdown_local_produces_markdown_no_ocr(project, monkeypatch):
    seen = _stub_markitdown(monkeypatch)
    sheet, cid, row_id = _seed(project, HTML)
    result, conv = _run_convert(
        project,
        name="doc",
        cid=cid,
        sheet=sheet,
        row_id=row_id,
        value=HTML,
        engine="markitdown",
    )
    assert result.markdown == "# Quarterly Report"
    assert result.ocr_used == []
    (fact,) = conv.calls_by_row[row_id]
    assert fact["engine"] == "markitdown" and fact["tier"] == "local"
    assert fact["external"] is False and fact["produced_markdown"] is True
    assert fact["document_read"]["kind"] == "text"
    # The sandbox is fenced to the one document + static mime table.
    assert "/etc/mime.types" in seen["read"]


def test_trafilatura_html_local_produces_markdown_no_ocr(project, monkeypatch):
    monkeypatch.setattr(
        "frisket.ops.capture.url.extract_markdown",
        lambda html: ("# Extracted", []),
    )
    sheet, cid, row_id = _seed(project, HTML)
    result, conv = _run_convert(
        project,
        name="doc",
        cid=cid,
        sheet=sheet,
        row_id=row_id,
        value=HTML,
        engine="trafilatura_html",
    )
    assert result.markdown == "# Extracted"
    assert result.ocr_used == []
    assert conv.calls_by_row[row_id][0]["engine"] == "trafilatura_html"


# -- sidecar (docling / chandra) ----------------------------------------------


@pytest.mark.parametrize("engine", ["docling", "chandra"])
def test_sidecar_produces_markdown_and_per_page_ocr(project, monkeypatch, engine):
    monkeypatch.setenv("FRISKET_MODELS_URL", "http://gateway-a:9000")
    monkeypatch.setenv("FRISKET_MODELS_TOKEN", "tok-a")
    captured = {}

    async def fake_post(ctx, route, *, files=None, data=None, connection=None, **kw):
        captured.update(route=route, data=data, connection=connection, files=files)
        return {
            "documents": [{"markdown": "# From Sidecar", "ocr_used": [True, False]}]
        }

    monkeypatch.setattr(module, "sidecar_post", fake_post)

    digest = project.add_blob(
        b"%PDF-1.4 scan", filename="scan.pdf", mime="application/pdf"
    )
    value = media_cell(digest, mime="application/pdf", filename="scan.pdf")
    sheet, cid, row_id = _seed(project, value, coltype="file")

    result = _execute(project, sheet, engine)
    assert result.status == "completed", result.errors
    columns = {c["name"]: c["id"] for c in project.columns(sheet)}
    assert project.get_values(sheet, columns["converted"])[row_id] == "# From Sidecar"
    assert project.get_values(sheet, columns["ocr_used"])[row_id] == [True, False]
    assert captured["route"] == "/to-markdown" and captured["data"]["engine"] == engine
    # The ADMITTED route binding's connection is used, not a fresh deref.
    assert captured["connection"].base_url == "http://gateway-a:9000"
    assert captured["connection"].token == "tok-a"
    fact = _reads(project, result)[0]
    assert fact["engine"] == engine and fact["tier"] == "sidecar"
    assert fact["ocr_used"] == [True, False]
    assert fact["document_read"]["kind"] == "blob"
    assert fact["document_read"]["blob_hash"] == digest


# -- datalab (hosted, metered) ------------------------------------------------


def _accepted_fact(credential_source="local"):
    from frisket.ops.integrations.datalab import _accepted_submission_accounting

    accounting = _accepted_submission_accounting(
        {"request_check_url": "https://datalab.test/accepted/ABC"},
        capability="document.convert",
        credential_source=credential_source,
    )
    fact = accounting["model_calls"][0]
    fact["id"] = "datalab_accepted_ABC"
    return fact


def _stub_datalab(monkeypatch, *, convert_impl):
    monkeypatch.setenv("DATALAB_API_KEY", "synthetic-datalab-key")
    monkeypatch.setattr(
        "frisket.ops.integrations.datalab.datalab_convert", convert_impl
    )


def _datalab_pdf(project):
    digest = project.add_blob(
        b"%PDF-1.4 doc", filename="doc.pdf", mime="application/pdf"
    )
    value = media_cell(digest, mime="application/pdf", filename="doc.pdf")
    return value


@pytest.mark.parametrize(
    ("input_mime", "extension"),
    [
        ("application/pdf", ".pdf"),
        ("image/png", ".png"),
        ("image/jpeg", ".jpeg"),
        ("image/webp", ".webp"),
        ("application/msword", ".doc"),
        ("application/vnd.ms-powerpoint", ".ppt"),
    ],
)
def test_datalab_accepted_fact_terminally_enriched_same_identity(
    project, monkeypatch, input_mime, extension
):
    persisted = []

    async def convert_impl(
        http,
        api_key,
        file_bytes,
        filename,
        mime,
        *,
        should_cancel=None,
        credential_source="none",
        on_accepted=None,
        **kw,
    ):
        assert mime == input_mime
        assert filename.endswith(extension)
        assert file_bytes == b"document input"
        accepted = {
            "tokens_in": None,
            "tokens_out": None,
            "cost": None,
            "model_calls": [_accepted_fact(credential_source)],
        }
        on_accepted(accepted)
        persisted.extend(
            dict(row) for row in project.db.execute("SELECT * FROM model_calls")
        )
        return {"markdown": "# Hosted", "page_count": 3}, 0.03

    _stub_datalab(monkeypatch, convert_impl=convert_impl)
    digest = project.add_blob(
        b"document input", filename=f"document{extension}", mime=input_mime
    )
    value = media_cell(digest, mime=input_mime, filename=f"document{extension}")
    sheet, cid, row_id = _seed(project, value, coltype="file")
    result = _execute(project, sheet, "datalab")
    assert result.status == "completed", result.errors
    # accepted fact persisted immediately (unknown-cost form) — not orphaned.
    assert persisted and persisted[0]["cost_source"] == "unknown"
    # terminal enrichment mutates the SAME call identity, priced + route-bound.
    (fact,) = RunResultStore(project).model_calls(result.run_id)
    assert fact["id"] == "datalab_accepted_ABC"
    assert fact["provider_cost_usd"] == 0.03
    assert fact["provider_reported_cost_usd"] == 0.03
    assert json.loads(fact["units"]) == {"pages": 3, "requests": 1}
    # cost_source is re-derived from the pinned route (not the provider string).
    assert fact["cost_source"] != "unknown"
    assert _receipt(project, result).provider_use[0]["cost_actual"] == 0.03
    # the §6 route-epoch observation is attached to the durable fact.
    assert fact["epoch_id"] is not None and fact["attempt_id"] is not None


def test_datalab_response_without_accepted_callback_refuses(project, monkeypatch):
    async def convert_impl(*args, **kwargs):
        return {"markdown": "# Unaccounted", "page_count": 1}, 0.01

    _stub_datalab(monkeypatch, convert_impl=convert_impl)
    sheet, _, _ = _seed(project, _datalab_pdf(project), coltype="file")
    result = _execute(project, sheet, "datalab")
    assert result.status == "failed", result.errors
    errors = project.db.execute(
        "SELECT error FROM results WHERE run_id=?", (result.run_id,)
    ).fetchall()
    assert any("accepted-job accounting" in str(item["error"]) for item in errors)
    assert RunResultStore(project).model_calls(result.run_id) == []


@pytest.mark.parametrize("engine", ["docling", "chandra", "datalab"])
def test_remote_direct_call_requires_real_admission(project, engine):
    value = _datalab_pdf(project)
    sheet, cid, row_id = _seed(project, value, coltype="file")
    with pytest.raises(RecipeInvocationHalt, match="admitted route"):
        _run_convert(
            project,
            name="doc",
            cid=cid,
            sheet=sheet,
            row_id=row_id,
            value=value,
            engine=engine,
        )


def test_datalab_halts_without_credential_consent(project, monkeypatch):
    from frisket.ai.llm import ModelRouter
    from frisket.engine.executor import ExecutorDeps
    from frisket.execution.credential_use import CredentialOwner
    from frisket.execution.definitions import StaticExecutionTargetProvider
    from frisket.execution.price_book import ByokZero
    from frisket.execution.provider import CompositionFacts, ExecutionComposition

    calls = []

    async def convert_impl(*a, **k):
        calls.append(1)
        return {"markdown": "x"}, 0.0

    _stub_datalab(monkeypatch, convert_impl=convert_impl)
    value = _datalab_pdf(project)
    sheet, cid, row_id = _seed(project, value, coltype="file")
    # Admit real org-BYOK terms; dispatch must not borrow the deployment key.
    router = ModelRouter(cache=None, cache_mode="off")
    organization = CredentialOwner.organization("document-org")
    composition = ExecutionComposition(
        facts=CompositionFacts(
            edition="hosted", org_id="document-org", funding=ByokZero()
        ),
        provider=StaticExecutionTargetProvider(secrets=project, router=router),
        credential_use_context=CredentialUseContext(
            cost_posture="org_key",
            consented_owner=organization,
            selected_owner=organization,
            deployment_owner=CredentialOwner.deployment("document-deployment"),
        ),
    )
    result = _execute(
        project,
        sheet,
        "datalab",
        router=router,
        deps=ExecutorDeps(execution_composition=composition),
    )
    assert result.status == "failed", result.errors
    assert result.errors[0].code == "promise_violation"
    run = project.db.execute(
        "SELECT params FROM runs WHERE id=?", (result.run_id,)
    ).fetchone()
    assert "promise_violation" in run["params"]
    assert not calls  # refused before the document left the machine


def test_datalab_rejects_unsupported_input_before_network(project, monkeypatch):
    calls = []

    async def convert_impl(*a, **k):
        calls.append(1)
        return {"markdown": "x"}, 0.0

    _stub_datalab(monkeypatch, convert_impl=convert_impl)
    # HTML is not a Datalab /convert input type.
    sheet, cid, row_id = _seed(project, HTML, coltype="text")
    result = _execute(project, sheet, "datalab")
    assert result.status == "failed", result.errors
    errors = project.db.execute(
        "SELECT error FROM results WHERE run_id=?", (result.run_id,)
    ).fetchall()
    assert any("does not support" in str(item["error"]) for item in errors)
    assert not calls


# -- actual (renamed) source selection ----------------------------------------


def test_actual_renamed_source_column_is_selected_by_argument(project, monkeypatch):
    _stub_markitdown(monkeypatch, markdown="# Renamed")
    # The column is 'renamed', NOT the Params field name.
    sheet, cid, row_id = _seed(project, HTML, name="renamed")
    result, conv = _run_convert(
        project,
        name="renamed",
        cid=cid,
        sheet=sheet,
        row_id=row_id,
        value=HTML,
        engine="markitdown",
    )
    assert result.markdown == "# Renamed"
    assert conv.calls_by_row[row_id][0]["document_read"]["source_column"] == "renamed"


def test_wrong_source_argument_refuses(project, monkeypatch):
    _stub_markitdown(monkeypatch)
    sheet, cid, row_id = _seed(project, HTML, name="renamed")
    ctx = OpContext(project=project, extras={"row_id": row_id, "run_id": 1})
    conv = AdmittedDocumentConverter(ctx, engine="markitdown")
    row = Row({"renamed": HTML})

    async def run():
        bound = conv.bind_row(
            row,
            sheet_id=sheet,
            row_id=row_id,
            sources=_sources("renamed", cid, HTML),
            ctx=ctx,
        )
        try:
            await bound.convert(row, ColumnRef("other"))
        finally:
            await conv.aclose()

    with pytest.raises(RowError, match="stale|admitted"):
        asyncio.run(run())


# -- source-type / path safety ------------------------------------------------


def test_text_cell_naming_a_file_is_content_not_a_path(project, monkeypatch, tmp_path):
    """A TEXT cell whose value happens to name a real file is inline content to
    convert, never a file reference — the read fact is 'text', not 'path'."""
    _stub_markitdown(monkeypatch)
    secret = tmp_path / "secret.html"
    secret.write_text("<html><body>TOPSECRET</body></html>")
    sheet, cid, row_id = _seed(project, str(secret), coltype="text")
    _result, conv = _run_convert(
        project,
        name="doc",
        cid=cid,
        sheet=sheet,
        row_id=row_id,
        value=str(secret),
        engine="markitdown",
    )
    assert conv.calls_by_row[row_id][0]["document_read"]["kind"] == "text"


def test_url_cell_refuses_with_download_pointer(project, monkeypatch):
    _stub_markitdown(monkeypatch)
    url = "https://example.com/x.pdf"
    sheet, cid, row_id = _seed(project, url, coltype="text")
    with pytest.raises(RowError, match="download media|local file"):
        _run_convert(
            project,
            name="doc",
            cid=cid,
            sheet=sheet,
            row_id=row_id,
            value=url,
            engine="markitdown",
        )


def test_empty_cell_does_no_work_and_produces_blank(project, monkeypatch):
    _stub_markitdown(monkeypatch)
    sheet, cid, row_id = _seed(project, {}, coltype="text")
    result, conv = _run_convert(
        project,
        name="doc",
        cid=cid,
        sheet=sheet,
        row_id=row_id,
        value={},
        engine="markitdown",
    )
    assert result.markdown == ""
    assert row_id not in conv.calls_by_row


def test_stale_source_value_refuses(project, monkeypatch):
    _stub_markitdown(monkeypatch)
    sheet, cid, row_id = _seed(project, HTML)
    ctx = OpContext(project=project, extras={"row_id": row_id, "run_id": 1})
    conv = AdmittedDocumentConverter(ctx, engine="markitdown")
    row = Row({"doc": HTML})

    async def run():
        # Admitted a DIFFERENT value than the row now carries.
        bound = conv.bind_row(
            row,
            sheet_id=sheet,
            row_id=row_id,
            sources=_sources("doc", cid, "<html>OTHER</html>"),
            ctx=ctx,
        )
        try:
            await bound.convert(row, ColumnRef("doc"))
        finally:
            await conv.aclose()

    with pytest.raises(RowError, match="differs from its admitted cell"):
        asyncio.run(run())


# -- cancellation -------------------------------------------------------------


def test_cancelled_owner_settles_with_no_accounting(project, monkeypatch):
    _stub_markitdown(monkeypatch)
    sheet, cid, row_id = _seed(project, HTML)
    with pytest.raises(asyncio.CancelledError):
        _run_convert(
            project,
            name="doc",
            cid=cid,
            sheet=sheet,
            row_id=row_id,
            value=HTML,
            engine="markitdown",
            cancelled=lambda: True,
        )


def test_datalab_cancel_keeps_accepted_accounting(project, monkeypatch):
    from frisket.ops.integrations.datalab import DatalabEngineError

    async def convert_impl(
        http,
        api_key,
        file_bytes,
        filename,
        mime,
        *,
        should_cancel=None,
        credential_source="none",
        on_accepted=None,
        **kw,
    ):
        accepted = {
            "tokens_in": None,
            "tokens_out": None,
            "cost": None,
            "model_calls": [_accepted_fact(credential_source)],
        }
        on_accepted(accepted)  # provider already owns the job
        raise DatalabEngineError(
            code="cancelled",
            message="Datalab conversion stopped: the run was cancelled.",
            accounting=accepted,
            provider_job_accepted=True,
        )

    _stub_datalab(monkeypatch, convert_impl=convert_impl)
    value = _datalab_pdf(project)
    sheet, cid, row_id = _seed(project, value, coltype="file")
    result = _execute(project, sheet, "datalab")
    assert result.status == "failed", result.errors
    assert result.errors[0].code == "external_effect_reconciliation_required"
    # The accepted job survives cancellation as one durable, unknown-cost call.
    (fact,) = RunResultStore(project).model_calls(result.run_id)
    assert fact["id"] == "datalab_accepted_ABC"
    assert fact["provider_cost_usd"] is None
    assert _receipt(project, result).provider_use[0]["model_call_count"] == 1


# -- blank output → no document link; durable-replay provenance ---------------


def test_reads_recovered_from_durable_batch(project, monkeypatch):
    import copy

    _stub_markitdown(monkeypatch, markdown="# Body")
    sheet, cid, row_id = _seed(project, HTML)
    batch = []
    original = module.write_document_convert_evidence

    def capture(*args, **kwargs):
        batch.extend(copy.deepcopy(kwargs["batch"]))
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "write_document_convert_evidence", capture)
    result = _execute(project, sheet)
    assert result.status == "completed", result.errors
    md_col = next(c["id"] for c in project.columns(sheet) if c["name"] == "converted")
    # Recovery view: the read fact is recoverable from the batch alone.
    reads = document_convert_reads(batch)
    assert reads[row_id]["engine"] == "markitdown"
    assert _reads(project, result)[0]["document_read"] == reads[row_id]["document_read"]
    link = project.db.execute(
        "SELECT COUNT(*) FROM evidence_links WHERE column_id=? "
        "AND link_role='source_provenance'",
        (md_col,),
    ).fetchone()[0]
    assert link == 1
    replay = _execute(project, sheet)
    assert replay.status == "completed" and replay.receipt_id == result.receipt_id
    assert _reads(project, replay) == _reads(project, result)


def test_blank_output_writes_no_document_link(project, monkeypatch):
    _stub_markitdown(monkeypatch, markdown="   \n  ")  # strips to empty
    sheet, cid, row_id = _seed(project, HTML)
    result = _execute(project, sheet)
    assert result.status == "completed", result.errors
    md_col = next(c["id"] for c in project.columns(sheet) if c["name"] == "converted")
    assert project.get_values(sheet, md_col)[row_id] == ""
    assert _reads(project, result)[0]["produced_markdown"] is False
    link = project.db.execute(
        "SELECT COUNT(*) FROM evidence_links WHERE column_id=?", (md_col,)
    ).fetchone()[0]
    assert link == 0


def test_errored_row_writes_no_document_link(project, monkeypatch):
    _stub_markitdown(monkeypatch, markdown="# Body")
    sheet, cid, row_id = _seed(project, HTML)
    original = module._BoundDocumentConverter.convert

    async def fail_after_read(self, row, source):
        await original(self, row, source)
        raise RowError("test_post_read_failure", "boom")

    monkeypatch.setattr(module._BoundDocumentConverter, "convert", fail_after_read)
    result = _execute(project, sheet)
    assert result.status == "failed", result.errors
    md_col = next(c["id"] for c in project.columns(sheet) if c["name"] == "converted")
    link = project.db.execute(
        "SELECT COUNT(*) FROM evidence_links WHERE column_id=?", (md_col,)
    ).fetchone()[0]
    assert link == 0
