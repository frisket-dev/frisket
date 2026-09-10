"""Source admission, OCR return integrity, and invocation-owned resources."""

import asyncio
import copy
import json
from contextlib import asynccontextmanager, closing
from io import BytesIO
from types import SimpleNamespace

import pytest
from PIL import Image

from frisket.actions.media_options import OcrOptions
from frisket.actions.types import ColumnRef, Row, RowError, StagedFile
from frisket.engine.executor import ocr_read as module
from frisket.ops import ocr_engines as engines
from frisket.ops import ocr_engines_local as local_engines
from frisket.ops import ocr_engines_sidecar as sidecar_engines
from frisket.engine.executor.row_file_stage import RowFileStager
from frisket.engine.store import Project
from frisket.engine.executor import run_action_spec
from frisket.engine.store.runs import RunResultStore
from frisket.ops.base import OpContext, RecipeInvocationHalt


def _execute(source, monkeypatch, engine, **options):
    from frisket.actions.core import RegisteredAction
    from frisket.actions.media import OCR
    from frisket.actions.registry import ACTION_REGISTRY

    monkeypatch.setattr(
        ACTION_REGISTRY,
        "_actions",
        {**ACTION_REGISTRY._actions, "media.ocr": RegisteredAction("media.ocr", OCR)},
    )
    body = {
        "action_id": "media.ocr",
        "scope": {"kind": "sheet_rows", "sheet_id": source[1]},
        "params": {"source": "scan", "engine": engine, **options},
        "output_names": {"text": "recognized", "blocks": "geometry"},
        "idempotency_key": "ocr-adapter-test",
    }
    result = run_action_spec(source[0], body, project_id="ocr-adapter")
    if result.status == "needs_confirmation":
        body["confirmation"] = result.errors[0].details["promise_set_hash"]
        result = run_action_spec(source[0], body, project_id="ocr-adapter")
    return result


def _png(width=12, height=8):
    out = BytesIO()
    Image.new("RGB", (width, height), "white").save(out, format="PNG")
    return out.getvalue()


@pytest.fixture
def source(tmp_path):
    with closing(Project.create(tmp_path / "ocr.frisket")) as project:
        sheet = project.add_sheet("Documents")
        column = project.add_column(sheet, "scan", type="image")
        digest = project.add_blob(_png(), filename="scan.png", mime="image/png")
        value = {"blob": digest, "filename": "scan.png", "mime": "image/png"}
        row_id = project.add_rows(sheet, [{"scan": value}], {"scan": column})[0]
        yield project, sheet, column, row_id, value


@asynccontextmanager
async def _reader(source, *, options=None, preview=False, engine="tesseract"):
    project, sheet, column, row_id, value = source
    options = options or OcrOptions()
    ctx = OpContext(project=project, extras={})
    files = RowFileStager(project, preview=preview)
    reader = module.AdmittedOcrReader(
        ctx, files, engine=engine, options=options.normalize(engine)
    )
    row = Row({"scan": copy.deepcopy(value)})
    try:
        await reader.start(expected_rows=2)
        bound = reader.bind_row(
            row,
            sheet_id=sheet,
            row_id=row_id,
            sources={
                "scan": {"column_id": column, "column_type": "image", "value": value}
            },
            ctx=ctx,
        )
        yield reader, bound, row, options
    finally:
        await reader.aclose()
        files.close()


@pytest.fixture
def recognition(monkeypatch):
    calls = []

    async def recognize(self, engine, pages, ctx, **kwargs):
        calls.append((engine, list(pages), kwargs))
        return [
            {
                "text": "Revenue 42",
                "blocks": [{"text": "42", "bbox": [1, 2, 3, 4], "score": 0.9}],
            }
        ]

    monkeypatch.setattr(engines.OcrEngines, "run_engine_on_pages", recognize)
    return calls


def test_real_gateway_admission_accepts_semantic_dpi(source, monkeypatch):
    monkeypatch.setenv("FRISKET_MODELS_URL", "http://gateway-a:9000")
    monkeypatch.setenv("FRISKET_MODELS_TOKEN", "synthetic-gateway-token")
    observed = []

    async def post(ctx, route, *, files, data, connection, **kwargs):
        from frisket.execution.attempt import routed_admission_in_scope

        admission = routed_admission_in_scope(ctx.extras)
        assert admission is not None
        assert admission.route.options == {"searchable_pdf": False}
        observed.append((route, data, connection.base_url, files))
        return {"pages": [{"text": "Recognized remotely", "blocks": []}]}

    monkeypatch.setattr(sidecar_engines, "sidecar_post", post)
    result = _execute(source, monkeypatch, "paddleocr-vl", dpi=300)
    assert result.status == "completed", result.errors
    assert len(observed) == 1
    assert observed[0][:3] == (
        "/ocr",
        {"engine": "paddleocr-vl"},
        "http://gateway-a:9000",
    )
    column = next(out.column_id for out in result.outputs if out.name == "recognized")
    assert source[0].get_values(source[1], column)[source[3]] == "Recognized remotely"
    (fact,) = RunResultStore(source[0]).model_calls(result.run_id)
    assert fact["epoch_id"] is not None and fact["attempt_id"] is not None


@pytest.mark.parametrize("terminal", ["success", "poll_failure", "missing_acceptance"])
def test_real_datalab_admission_retains_accepted_fact(source, monkeypatch, terminal):
    from frisket.ops.integrations.datalab import (
        DatalabEngineError,
        _accepted_submission_accounting,
        _complete_submission_accounting,
    )

    monkeypatch.setenv("DATALAB_API_KEY", "synthetic-datalab-key")
    accepted_ids = []

    async def ocr(
        http, key, data, filename, mime, *, on_accepted, credential_source, **kwargs
    ):
        assert data == _png()
        if terminal == "missing_acceptance":
            return [{"text": "Unaccounted", "blocks": []}], 0.01
        accounting = _accepted_submission_accounting(
            {"request_check_url": "https://datalab.test/jobs/ocr-one"},
            capability="ocr",
            credential_source=credential_source,
        )
        on_accepted(accounting)
        [fact] = accounting["model_calls"]
        accepted_ids.append(fact["id"])
        persisted = source[0].db.execute("SELECT * FROM model_calls").fetchall()
        assert len(persisted) == 1 and persisted[0]["cost_source"] == "unknown"
        if terminal == "poll_failure":
            raise DatalabEngineError(
                code="cancelled",
                message="Polling stopped after acceptance",
                accounting=accounting,
                provider_job_accepted=True,
            )
        _complete_submission_accounting(
            accounting, pages=1, cost_usd=0.01, duration_ms=7
        )
        return [{"text": "Datalab text", "blocks": []}], 0.01

    monkeypatch.setattr("frisket.ops.integrations.datalab.datalab_ocr", ocr)
    result = _execute(source, monkeypatch, "datalab")
    if terminal == "missing_acceptance":
        assert result.status == "failed", result.errors
        return
    (fact,) = RunResultStore(source[0]).model_calls(result.run_id)
    assert fact["id"] == accepted_ids[0]
    assert fact["epoch_id"] is not None and fact["attempt_id"] is not None
    if terminal == "success":
        assert result.status == "completed", result.errors
        assert fact["provider_cost_usd"] == 0.01
        assert json.loads(fact["units"]) == {"pages": 1, "requests": 1}
    else:
        assert result.status == "failed", result.errors
        assert fact["provider_cost_usd"] is None
        assert result.errors[0].code == "external_effect_reconciliation_required"


@pytest.mark.asyncio
async def test_actual_source_and_read_survive_returned_sibling_mutation(
    source, recognition
):
    async with _reader(source) as (owner, reader, row, options):
        result = await reader.recognize(row, ColumnRef("scan"), options=options)
        assert result.text.root == "Revenue 42"
        result.blocks[0]["blocks"][0]["bbox"][0] = 999
        (fact,) = owner.calls_by_row[source[3]]
        assert fact["blocks"][0]["blocks"][0]["bbox"] == [1, 2, 3, 4]
        assert fact["source"]["blob_hash"] == source[4]["blob"]
        assert fact["page_images"]["1"]["blob_hash"] == source[4]["blob"]
        assert fact["page_images"]["1"]["source_blob"] is True
        assert (
            owner.accounting_by_row[source[3]]["model_calls"][0]["engine"]
            == "tesseract"
        )
        assert len(recognition) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["row", "source", "options", "type", "identity"])
async def test_changed_source_or_options_refuse_before_effect(
    source, recognition, mutation
):
    async with _reader(source) as (_, reader, row, options):
        column = ColumnRef("scan")
        expected = RowError
        if mutation == "row":
            row.values["scan"]["blob"] = "f" * 64
        elif mutation == "source":
            column = ColumnRef("another")
        elif mutation == "options":
            options = OcrOptions(dpi=300)
            expected = RecipeInvocationHalt
        elif mutation == "type":
            source[0].db.execute(
                "UPDATE columns SET type='file' WHERE id=?", (source[2],)
            )
        elif mutation == "identity":
            row = Row(copy.deepcopy(dict(row.values)))
        with pytest.raises(expected):
            await reader.recognize(row, column, options=options)
        assert recognition == []


@pytest.mark.asyncio
async def test_one_call_per_row_and_closed_reader(source, recognition):
    async with _reader(source) as (owner, reader, row, options):
        await reader.recognize(row, ColumnRef("scan"), options=options)
        with pytest.raises(RuntimeError, match="one recognition"):
            await reader.recognize(row, ColumnRef("scan"), options=options)
        await owner.aclose()
        with pytest.raises(RuntimeError, match="closed"):
            await reader.recognize(row, ColumnRef("scan"), options=options)
    assert len(recognition) == 1


@pytest.mark.asyncio
async def test_preview_does_not_persist_page_bytes(source, recognition, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("preview persisted OCR evidence bytes")

    monkeypatch.setattr(type(source[0].blob_store), "put_path", forbidden)
    async with _reader(source, preview=True) as (owner, reader, row, options):
        result = await reader.recognize(row, ColumnRef("scan"), options=options)
        assert result.text.root == "Revenue 42"
        assert owner.calls_by_row[source[3]][0]["page_images"] == {}


@pytest.mark.asyncio
async def test_pdf_uses_actual_capped_raster_and_stages_searchable_pdf(
    source, recognition, monkeypatch
):
    project, sheet, column, row_id, _ = source
    digest = project.add_blob(
        b"%PDF-1.4\nfixture", filename="scan.pdf", mime="application/pdf"
    )
    value = {"blob": digest, "filename": "scan.pdf", "mime": "application/pdf"}
    source = (project, sheet, column, row_id, value)
    renders = []

    async def pages(self, path, media, options, scratch):
        renders.append(path)
        output = scratch / "page-1.png"
        output.write_bytes(_png(2200, 1100))
        return [output]

    monkeypatch.setattr(engines.OcrEngines, "_page_images", pages)
    monkeypatch.setattr(
        "frisket.ops.searchable_pdf.compose_searchable_pdf",
        lambda *args, **kwargs: (
            b"%PDF-searchable",
            [SimpleNamespace(index=1, degraded=False, painted_count=1)],
        ),
    )
    async with _reader(source, options=OcrOptions(searchable_pdf=True)) as (
        owner,
        reader,
        row,
        options,
    ):
        result = await reader.recognize(row, ColumnRef("scan"), options=options)
        assert isinstance(result.pdf.value, StagedFile)
        image = owner.calls_by_row[row_id][0]["page_images"]["1"]
        assert (image["source_width"], image["source_height"]) == (2200, 1100)
        assert (image["width"], image["height"], image["downscaled"]) == (
            2000,
            1000,
            True,
        )
        with project.blob_store.materialize(image["blob_hash"]) as path:
            with Image.open(path) as stored:
                assert stored.size == (2000, 1000)
        assert len(renders) == 1
        assert (
            project.db.execute(
                "SELECT 1 FROM blobs WHERE hash=?", (image["blob_hash"],)
            ).fetchone()
            is None
        )


@pytest.mark.asyncio
async def test_searchable_pdf_failure_keeps_text_and_geometry(source, recognition):
    async with _reader(source, options=OcrOptions(searchable_pdf=True)) as (
        _,
        reader,
        row,
        options,
    ):
        result = await reader.recognize(row, ColumnRef("scan"), options=options)
        assert result.text.root == "Revenue 42"
        assert result.blocks[0]["blocks"]
        assert result.pdf.status == "failed"
        assert "PDF source" in result.pdf.message


@pytest.mark.asyncio
async def test_remote_engine_cannot_start_without_admission(source):
    project = source[0]
    files = RowFileStager(project)
    reader = module.AdmittedOcrReader(
        OpContext(project=project, extras={}),
        files,
        engine="datalab",
        options=OcrOptions().normalize("datalab"),
    )
    try:
        with pytest.raises(RecipeInvocationHalt, match="admitted route"):
            await reader.start(expected_rows=1)
    finally:
        await reader.aclose()
        files.close()


@pytest.mark.asyncio
async def test_close_settles_active_recognition_before_returning(source, monkeypatch):
    entered = asyncio.Event()
    stopped = asyncio.Event()

    async def recognize(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    monkeypatch.setattr(engines.OcrEngines, "run_engine_on_pages", recognize)
    async with _reader(source) as (owner, reader, row, options):
        task = asyncio.create_task(
            reader.recognize(row, ColumnRef("scan"), options=options)
        )
        await entered.wait()
        await owner.aclose()
        assert stopped.is_set()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
@pytest.mark.parametrize("teardown_failed", [False, True])
async def test_rapidocr_reuses_pool_and_releases_lease_after_children_stop(
    source, monkeypatch, teardown_failed
):
    events = []
    monkeypatch.setattr(
        local_engines, "_rapidocr_model_root_dir", lambda language: None
    )
    monkeypatch.setattr(
        local_engines,
        "rapidocr_topology",
        lambda rows: SimpleNamespace(
            workers=2,
            onnx_intra_threads=1,
            onnx_inter_threads=1,
            opencv_threads=1,
            memory_mb=512,
        ),
    )
    lease = SimpleNamespace(
        release=lambda: events.append("release"), poison=lambda: events.append("poison")
    )
    monkeypatch.setattr(
        local_engines, "acquire_local_engine_lease", lambda engine: lease
    )

    class Pool:
        def __init__(self, **kwargs):
            self.teardown_failed = teardown_failed
            events.append(("pool", kwargs["expected_rows"], kwargs["max_workers"]))

        async def ocr(self, paths):
            events.append("ocr")
            return [{"text": "42", "blocks": []}]

        async def close(self):
            events.append("close")

    monkeypatch.setattr(local_engines, "RapidOCRProcessPool", Pool)
    async with _reader(source, engine="rapidocr") as (owner, first, row, options):
        await first.recognize(row, ColumnRef("scan"), options=options)
        second = owner.bind_row(
            row,
            sheet_id=source[1],
            row_id=source[3] + 1,
            sources=first._sources,
            ctx=first._ctx,
        )
        await second.recognize(row, ColumnRef("scan"), options=options)
    assert events == [
        ("pool", 2, 2),
        "ocr",
        "ocr",
        "close",
        "poison" if teardown_failed else "release",
    ]
