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

pypdf = pytest.importorskip("pypdf")


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


def _pdf_bytes(*sizes: tuple[int, int]) -> bytes:
    writer = pypdf.PdfWriter()
    for width, height in sizes:
        writer.add_blank_page(width=width, height=height)
    output = BytesIO()
    writer.write(output)
    writer.close()
    return output.getvalue()


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
                "scan": {
                    "column_id": column,
                    "column_type": project.db.execute(
                        "SELECT type FROM columns WHERE id=?", (column,)
                    ).fetchone()[0],
                    "value": value,
                }
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
                "width": 8,
                "height": 12,
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
        progress = []
        reader._ctx.extras["report_work_progress"] = lambda **value: progress.append(
            value
        )
        result = await reader.recognize(row, ColumnRef("scan"), options=options)
        assert [(event["completed"], event["total"]) for event in progress] == [
            (0, 1),
            (1, 1),
        ]
        assert result.text.root == "Revenue 42"
        result.blocks[0]["blocks"][0]["bbox"][0] = 999
        (fact,) = owner.calls_by_row[source[3]]
        assert fact["ocr_token_granularity"] == "word"
        assert fact["blocks"][0]["blocks"][0]["bbox"] == [1, 2, 3, 4]
        assert fact["blocks"][0]["positions"] == {"width": 8, "height": 12}
        assert "positions" not in result.blocks[0]
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
async def test_pdf_retains_geometry_without_display_blob_and_stages_searchable_pdf(
    source, recognition, monkeypatch
):
    project, sheet, column, row_id, _ = source
    digest = project.add_blob(
        b"%PDF-1.4\nfixture", filename="scan.pdf", mime="application/pdf"
    )
    value = {"blob": digest, "filename": "scan.pdf", "mime": "application/pdf"}
    source = (project, sheet, column, row_id, value)
    renders = []
    blob_count = project.db.execute("SELECT COUNT(*) FROM blobs").fetchone()[0]

    def forbid_display_blob(*args, **kwargs):
        raise AssertionError("OCR display pages must not enter canonical storage")

    monkeypatch.setattr(type(project.blob_store), "put_path", forbid_display_blob)

    async def pages(self, path, media, options, scratch, *, engine=None):
        del engine
        renders.append(path)
        output = scratch / "page-1.png"
        output.write_bytes(_png(2200, 1100))
        return [output]

    monkeypatch.setattr(engines.OcrEngines, "_page_images", pages)
    monkeypatch.setattr(engines.OcrEngines, "_pdf_page_count", lambda *args: 1)

    def compose(*args, **kwargs):
        assert kwargs["raster_dimensions"] == [(2200, 1100)]
        return (
            b"%PDF-searchable",
            [SimpleNamespace(index=1, degraded=False, painted_count=1)],
        )

    monkeypatch.setattr(
        "frisket.ops.searchable_pdf.compose_searchable_pdf",
        compose,
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
        assert "blob_hash" not in image
        assert len(renders) == 1
        assert (
            project.db.execute("SELECT COUNT(*) FROM blobs").fetchone()[0] == blob_count
        )


@pytest.mark.asyncio
async def test_pdf_page_source_ocr_remaps_facts_to_original_page(
    source, recognition, monkeypatch
):
    project, sheet, column, row_id, _ = source
    raw = _pdf_bytes((111, 222), (333, 444))
    digest = project.add_blob(raw, filename="report.pdf", mime="application/pdf")
    value = {
        "blob": digest,
        "filename": "report.pdf",
        "mime": "application/pdf",
        "page": 2,
    }
    project.db.execute("UPDATE columns SET type='file' WHERE id=?", (column,))
    project.db.commit()
    source = (project, sheet, column, row_id, value)

    async def pages(self, path, media, options, scratch, *, engine=None):
        del engine
        selected = pypdf.PdfReader(path)
        assert len(selected.pages) == 1
        assert float(selected.pages[0].mediabox.width) == 333
        output = scratch / "page-1.png"
        output.write_bytes(_png(300, 400))
        return [output]

    def compose(pdf, pages, **kwargs):
        assert len(pypdf.PdfReader(BytesIO(pdf)).pages) == 1
        assert "page_number" not in pages[0]
        assert kwargs["raster_dimensions"] == [(300, 400)]
        return (
            b"%PDF-searchable",
            [SimpleNamespace(index=1, degraded=False, painted_count=1)],
        )

    monkeypatch.setattr(engines.OcrEngines, "_page_images", pages)
    monkeypatch.setattr(
        "frisket.ops.searchable_pdf.compose_searchable_pdf",
        compose,
    )
    async with _reader(source, options=OcrOptions(searchable_pdf=True)) as (
        owner,
        reader,
        row,
        options,
    ):
        result = await reader.recognize(row, ColumnRef("scan"), options=options)
        assert result.text.root == "Revenue 42"
        assert result.blocks[0]["page"] == 2
        assert isinstance(result.pdf.value, StagedFile)
        (fact,) = owner.calls_by_row[row_id]
        assert fact["source"]["page"] == 2
        assert fact["source"]["page_count"] == 2
        assert fact["pages"][0]["page_number"] == 2
        assert fact["blocks"][0]["page"] == 2
        assert list(fact["page_images"]) == ["2"]
        assert fact["page_images"]["2"]["mime"] == "application/pdf"
        with project.materialize_blob(digest) as stored:
            assert stored.read_bytes() == raw


@pytest.mark.asyncio
async def test_pdf_ocr_preserves_blank_page_separators(source, monkeypatch):
    project, sheet, column, row_id, _ = source
    raw = _pdf_bytes((100, 100), (100, 100), (100, 100))
    digest = project.add_blob(raw, filename="three.pdf", mime="application/pdf")
    value = {"blob": digest, "filename": "three.pdf", "mime": "application/pdf"}
    project.db.execute("UPDATE columns SET type='file' WHERE id=?", (column,))
    project.db.commit()
    source = (project, sheet, column, row_id, value)

    async def page_images(self, path, media, options, scratch, *, engine=None):
        del engine
        [page] = options["_selected_pages"]
        output = scratch / f"page-{page}.png"
        output.write_bytes(_png())
        return [output]

    async def recognize(self, engine, pages, ctx, **kwargs):
        number = int(pages[0].stem.split("-")[-1])
        return [
            [
                {"text": "First", "blocks": []},
                {"text": "", "blocks": []},
                {"text": "Third", "blocks": []},
            ][number - 1]
        ]

    monkeypatch.setattr(engines.OcrEngines, "_page_images", page_images)
    monkeypatch.setattr(engines.OcrEngines, "run_engine_on_pages", recognize)
    async with _reader(source) as (owner, reader, row, options):
        result = await reader.recognize(row, ColumnRef("scan"), options=options)
        assert result.text.root == "First\n\n\n\nThird"
        (fact,) = owner.calls_by_row[row_id]
        assert fact["text"] == result.text.root
        assert fact["source"]["page_count"] == 3
        assert [page["page_number"] for page in fact["pages"]] == [1, 2, 3]
        assert list(fact["page_images"]) == ["1", "2", "3"]


def _pdf_source(source, page_count):
    project, sheet, column, row_id, _ = source
    digest = project.add_blob(
        _pdf_bytes(*[(100, 200)] * page_count),
        filename="pages.pdf",
        mime="application/pdf",
    )
    project.db.execute("UPDATE columns SET type='file' WHERE id=?", (column,))
    project.db.commit()
    return (
        project,
        sheet,
        column,
        row_id,
        {"blob": digest, "filename": "pages.pdf", "mime": "application/pdf"},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("selected_page", [None, 2])
async def test_indra_pdf_uses_one_worker_and_preserves_physical_geometry(
    source, monkeypatch, selected_page
):
    source = _pdf_source(source, 3)
    if selected_page is not None:
        source[-1]["page"] = selected_page
    calls, progress = [], []

    async def forbidden_raster(*args, **kwargs):
        raise AssertionError("Indra PDF rendering belongs inside its model worker")

    async def recognize(self, path, scratch, *, dpi, page_count, report_progress):
        calls.append(path)
        assert len(pypdf.PdfReader(path).pages) == page_count
        assert page_count == (1 if selected_page else 3)
        pages = []
        for number in range(1, page_count + 1):
            pages.append(
                {
                    "text": "",
                    "blocks": [],
                    "error": {
                        "code": "ocr_page_failed",
                        "stage": "rendering",
                    },
                }
                if number == 2
                else {
                    "text": f"Page {selected_page or number}",
                    "blocks": [],
                    "width": 100,
                    "height": 200,
                }
            )
            report_progress(number, page_count)
        return pages

    monkeypatch.setattr(engines.OcrEngines, "_page_images", forbidden_raster)
    monkeypatch.setattr(engines.OcrEngines, "_ocr_indra_pdf", recognize)
    async with _reader(source, engine="indra-ocr") as (owner, reader, row, options):
        reader._ctx.extras["report_work_progress"] = lambda **value: progress.append(
            value
        )
        result = await reader.recognize(row, ColumnRef("scan"), options=options)
        [fact] = owner.calls_by_row[source[3]]
        if selected_page:
            assert result.text.root == "Page 2"
            assert result.blocks[0]["page"] == 2
            assert list(fact["page_images"]) == ["2"]
            assert result.warnings == ()
        else:
            assert result.text.root == "Page 1\n\n\n\nPage 3"
            assert result.warnings == ("Page 2 failed during rendering.",)
            assert list(fact["page_images"]) == ["1", "3"]
        assert all(
            frame["source_height"] == 200 for frame in fact["page_images"].values()
        )
        assert len(calls) == 1
        assert progress[0]["completed"] == 0
        assert progress[-1]["completed"] == progress[-1]["total"]


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["rapidocr", "tesseract"])
async def test_many_page_pdf_consumes_and_removes_each_raster_before_next(
    source, monkeypatch, engine
):
    source = _pdf_source(source, 500)
    seen, scratch_roots, progress = [], [], []

    @asynccontextmanager
    async def pool(**kwargs):
        yield object()

    monkeypatch.setattr(engines, "rapidocr_execution_scope", pool)

    async def render(self, path, media, options, scratch, *, engine=None):
        [number] = options["_selected_pages"]
        assert not list(scratch.parent.rglob("*.png"))
        scratch_roots.append(scratch.parent)
        output = scratch / f"page-{number}.png"
        output.write_bytes(_png(number + 1, 20))
        return [output]

    async def recognize(self, engine, paths, ctx, **kwargs):
        [path] = paths
        assert len(list(path.parent.parent.rglob("*.png"))) == 1
        number = int(path.stem.split("-")[-1])
        seen.append(path)
        return [{"text": f"Page {number}", "blocks": []}]

    monkeypatch.setattr(engines.OcrEngines, "_page_images", render)
    monkeypatch.setattr(engines.OcrEngines, "run_engine_on_pages", recognize)
    async with _reader(source, engine=engine) as (owner, reader, row, options):
        reader._ctx.extras["report_work_progress"] = lambda **value: progress.append(
            value
        )
        result = await reader.recognize(row, ColumnRef("scan"), options=options)
        assert result.text.root == "\n\n".join(f"Page {i}" for i in range(1, 501))
        assert not result.warnings
        [fact] = owner.calls_by_row[source[3]]
        assert [p["page_number"] for p in fact["pages"]] == list(range(1, 501))
        assert fact["page_images"]["500"]["source_width"] == 501
        [call] = owner.accounting_by_row[source[3]]["model_calls"]
        assert call["units"]["pages"] == 500
    assert len(seen) == 500 and all(not path.exists() for path in seen)
    assert all(not path.exists() for path in scratch_roots)
    assert [p["completed"] for p in progress] == list(range(0, 501))
    assert all(p["total"] == 500 and p["row_id"] == source[3] for p in progress)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["rendering", "recognition"])
async def test_failed_pdf_page_preserves_neighbors_and_warns_explicitly(
    source, monkeypatch, failure_stage
):
    source = _pdf_source(source, 3)
    observed = []

    async def render(self, path, media, options, scratch, *, engine=None):
        [number] = options["_selected_pages"]
        output = scratch / f"page-{number}.png"
        output.write_bytes(_png())
        observed.append(output)
        if number == 2 and failure_stage == "rendering":
            raise RuntimeError("private renderer exception must never escape")
        return [output]

    async def recognize(self, engine, paths, ctx, **kwargs):
        number = int(paths[0].stem.split("-")[-1])
        if number == 2:
            raise RuntimeError("private OCR exception must never escape")
        return [{"text": f"Page {number}", "blocks": []}]

    monkeypatch.setattr(engines.OcrEngines, "_page_images", render)
    monkeypatch.setattr(engines.OcrEngines, "run_engine_on_pages", recognize)
    async with _reader(source) as (owner, reader, row, options):
        result = await reader.recognize(row, ColumnRef("scan"), options=options)
        assert result.text.root == "Page 1\n\n\n\nPage 3"
        assert result.warnings == (f"Page 2 failed during {failure_stage}.",)
        assert [p["page"] for p in result.blocks] == [1, 2, 3]
        [fact] = owner.calls_by_row[source[3]]
        assert fact["pages"][1]["error"]["stage"] == failure_stage
        assert fact["warnings"] == list(result.warnings)
        assert "private" not in str(fact)
    assert all(not path.exists() for path in observed)


def test_partial_pdf_action_persists_text_and_warns_every_successful_output(
    source, monkeypatch
):
    project = source[0]
    sheet = project.add_sheet("PDF documents")
    column = project.add_column(sheet, "scan", type="file")
    digest = project.add_blob(
        _pdf_bytes((100, 100), (100, 100), (100, 100)),
        filename="three.pdf",
        mime="application/pdf",
    )
    value = {"blob": digest, "filename": "three.pdf", "mime": "application/pdf"}
    row_id = project.add_rows(sheet, [{"scan": value}], {"scan": column})[0]

    async def render(self, path, media, options, scratch, *, engine=None):
        [number] = options["_selected_pages"]
        if number == 2:
            raise RuntimeError("private document detail")
        output = scratch / f"page-{number}.png"
        output.write_bytes(_png())
        return [output]

    async def recognize(self, engine, pages, ctx, **kwargs):
        number = int(pages[0].stem.split("-")[-1])
        return [{"text": f"Page {number}", "blocks": []}]

    monkeypatch.setattr(engines.OcrEngines, "_page_images", render)
    monkeypatch.setattr(engines.OcrEngines, "run_engine_on_pages", recognize)
    result = _execute((project, sheet, column, row_id, value), monkeypatch, "tesseract")
    assert result.status == "completed", result.errors
    outputs = {output.name: output.column_id for output in result.outputs}
    assert set(outputs) == {"recognized", "geometry"}
    assert (
        project.get_values(sheet, outputs["recognized"])[row_id]
        == "Page 1\n\n\n\nPage 3"
    )
    warning = "Page 2 failed during rendering."
    rows = project.db.execute(
        "SELECT warnings,error FROM results WHERE run_id=?", (result.run_id,)
    ).fetchall()
    assert len(rows) == 2
    assert all(
        json.loads(row["warnings"]) == [warning] and not row["error"] for row in rows
    )
    receipt = json.loads(
        project.db.execute(
            "SELECT body FROM receipts WHERE id=?", (result.receipt_id,)
        ).fetchone()["body"]
    )
    assert any(warning in item for item in receipt["warnings"])
    assert any(warning in item for item in result.warnings)


@pytest.mark.asyncio
@pytest.mark.parametrize("all_failed", [True, False])
async def test_pdf_all_failed_is_row_error_and_warnings_are_bounded(
    source, monkeypatch, all_failed
):
    source = _pdf_source(source, 30)

    async def fail(self, path, media, options, scratch, *, engine=None):
        if not all_failed and options["_selected_pages"] == [30]:
            output = scratch / "page-30.png"
            output.write_bytes(_png())
            return [output]
        raise RuntimeError("private page failure")

    async def recognize(*args, **kwargs):
        return [{"text": "Surviving page", "blocks": []}]

    monkeypatch.setattr(engines.OcrEngines, "_page_images", fail)
    monkeypatch.setattr(engines.OcrEngines, "run_engine_on_pages", recognize)
    async with _reader(source) as (owner, reader, row, options):
        if all_failed:
            with pytest.raises(RowError, match="OCR failed for all 30 pages"):
                await reader.recognize(row, ColumnRef("scan"), options=options)
            assert source[3] not in owner.calls_by_row
        else:
            result = await reader.recognize(row, ColumnRef("scan"), options=options)
            assert len(result.warnings) == 21
            assert result.warnings[-1] == (
                "29 pages failed; 9 additional page warnings omitted."
            )
            assert result.text.root.endswith("Surviving page")


@pytest.mark.asyncio
async def test_fatal_local_session_error_stops_without_fake_failed_pages(
    source, monkeypatch
):
    source = _pdf_source(source, 30)
    observed = []

    async def render(self, path, media, options, scratch, *, engine=None):
        observed.append(options["_selected_pages"])
        raise RecipeInvocationHalt("local_artifact_unavailable", "Unavailable runtime")

    monkeypatch.setattr(engines.OcrEngines, "_page_images", render)
    async with _reader(source) as (owner, reader, row, options):
        with pytest.raises(RecipeInvocationHalt, match="Unavailable runtime"):
            await reader.recognize(row, ColumnRef("scan"), options=options)
        assert source[3] not in owner.calls_by_row
    assert observed == [[1]]


@pytest.mark.asyncio
async def test_cancellation_discards_pdf_partial_output_and_cleans_raster(
    source, monkeypatch
):
    source = _pdf_source(source, 3)
    cancelled, observed = False, []

    async def render(self, path, media, options, scratch, *, engine=None):
        [number] = options["_selected_pages"]
        output = scratch / f"page-{number}.png"
        output.write_bytes(_png())
        observed.append(output)
        return [output]

    async def recognize(self, engine, paths, ctx, **kwargs):
        nonlocal cancelled
        cancelled = len(observed) == 2
        return [{"text": "Completed page", "blocks": []}]

    monkeypatch.setattr(engines.OcrEngines, "_page_images", render)
    monkeypatch.setattr(engines.OcrEngines, "run_engine_on_pages", recognize)
    async with _reader(source) as (owner, reader, row, options):
        owner._ctx.extras["cancelled"] = lambda: cancelled
        with pytest.raises(asyncio.CancelledError):
            await reader.recognize(row, ColumnRef("scan"), options=options)
        assert source[3] not in owner.calls_by_row
    assert len(observed) == 2 and all(not p.exists() for p in observed)


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
