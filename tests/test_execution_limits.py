"""Runtime behavior for request-scoped, provider-neutral execution limits."""

from __future__ import annotations

import asyncio
import json
from io import BytesIO
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from decimal import Decimal

import pytest
from pypdf import PdfWriter

from frisket.ai.llm import ModelRouter
from frisket.engine.runner import MapRunner, validation
from frisket.engine.store import Project
from frisket.engine.store.execution_routes import instance_principal
from frisket.engine.store.media_blobs import (
    MediaBlobStore,
    media_cell,
    owned_media_metadata_document,
)
from frisket.execution.attempt_authority import UnroutedOnlyAuthority
from frisket.execution.consent_coverage import ConsentCoverage
from frisket.execution.provider import (
    ExecutionCompositionContext,
    open_execution_composition,
)
from frisket.ops.base import OpContext, Recipe
from frisket.ops.integrations import natural_pdf
from frisket.ops.media_probe import probe_for_ingest
from frisket.sdk.ops import transcribe_engines
from frisket.engine.executor import row_media_read as video_frames
from frisket.actions.types import ColumnRef, Row
from frisket.engine.executor.pdf_tables_read import AdmittedPdfTablesReader
from frisket.actions.media_options import OcrOptions, TranscriptionOptions
from frisket.engine.executor.ocr_read import AdmittedOcrReader
from frisket.engine.executor.row_file_stage import RowFileStager
from frisket.ops.ocr_engines import OcrEngines
from frisket.engine.executor.document_convert import _BoundDocumentConverter
from frisket.engine.executor.transcription_read import AdmittedTranscriber
from runner_test_helpers import run_with_output_claim


def _limits(**values: Any) -> Any:
    # Kept inside the test so the RED suite collects before the public type exists.
    from frisket.execution.provider import ExecutionLimits

    return ExecutionLimits(**values)


def _limit_error() -> type[Exception]:
    from frisket.execution.provider import ExecutionLimitExceeded

    return ExecutionLimitExceeded


def _project_with_blob(
    tmp_path: Path,
    *,
    payload: bytes,
    probe: dict[str, Any],
    mime: str,
    filename: str,
) -> tuple[Project, dict[str, str]]:
    project = Project.create(tmp_path / "limits.frisket", name="limits")
    digest = project.add_blob(
        payload,
        filename=filename,
        mime=mime,
        metadata=owned_media_metadata_document(probe=probe),
    )
    return project, media_cell(digest, mime=mime, filename=filename)


def _context(
    project: Project, limits: Any, *, extras: dict[str, Any] | None = None
) -> OpContext:
    return OpContext(
        project=project,
        http=None,
        extras={} if extras is None else extras,
        execution_limits=limits,
    )


def _bound_media(reader, media, ctx, *, column_type):
    project = ctx.project
    sheet = project.add_sheet(f"Source {len(project.sheets())}")
    column = project.add_column(sheet, "source", type=column_type)
    row_id = project.add_rows(sheet, [{"source": media}], {"source": column})[0]
    row = Row({"source": media})
    return row, reader.bind_row(
        row,
        sheet_id=sheet,
        row_id=row_id,
        ctx=ctx,
        sources={
            "source": {"column_id": column, "column_type": column_type, "value": media}
        },
    )


async def _recognize(media, ctx):
    options = OcrOptions()
    files = RowFileStager(ctx.project, preview=False)
    reader = AdmittedOcrReader(
        ctx, files, engine="tesseract", options=options.normalize("tesseract")
    )
    try:
        await reader.start(expected_rows=1)
        row, bound = _bound_media(reader, media, ctx, column_type="file")
        return await bound.recognize(row, ColumnRef("source"), options=options)
    finally:
        await reader.aclose()
        files.close()


async def _transcribe(media, ctx):
    options = TranscriptionOptions()
    reader = AdmittedTranscriber(
        ctx, engine="faster_whisper", options=options.normalize("faster_whisper")
    )
    try:
        await reader.start(expected_rows=1)
        row, bound = _bound_media(reader, media, ctx, column_type="audio")
        return await bound.transcribe(row, ColumnRef("source"), options=options)
    finally:
        await reader.aclose()


def _stored_pdf(project: Project) -> tuple[int, int, dict[str, str]]:
    sheet_id = project.add_sheet("Documents")
    column_id = project.add_column(sheet_id, "pdf", type="file")
    digest = project.add_blob(
        b"%PDF-1.4\nexample",
        filename="source.pdf",
        mime="application/pdf",
        metadata=owned_media_metadata_document(probe={"kind": "pdf", "pages": 3}),
    )
    media = media_cell(digest, mime="application/pdf", filename="source.pdf")
    row_id = project.add_rows(sheet_id, [{"pdf": media}], {"pdf": column_id})[0]
    return sheet_id, row_id, media


def _pdf_bytes(page_count: int) -> bytes:
    output = BytesIO()
    writer = PdfWriter()
    for _ in range(page_count):
        writer.add_blank_page(width=100, height=100)
    writer.write(output)
    return output.getvalue()


def _stub_ocr_dispatch(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    raster_calls: list[Path] = []

    async def fake_page_images(
        _recipe: OcrEngines, path: Path, *_args: Any, **_kwargs: Any
    ) -> list[dict[str, Any]]:
        raster_calls.append(path)
        return []

    async def fake_engine(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(OcrEngines, "_page_images", fake_page_images)
    monkeypatch.setattr(OcrEngines, "run_engine_on_pages", fake_engine)
    return raster_calls


@pytest.mark.parametrize(
    ("limit", "probe", "dispatches", "detail"),
    [
        (None, {"duration_seconds": 3.0, "kind": "audio"}, True, None),
        (3, {"duration_seconds": 3.0, "kind": "audio"}, True, None),
        (2, {"duration_seconds": 3.0, "kind": "audio"}, False, None),
        (2, {"kind": "audio"}, False, "measurement_unavailable"),
    ],
    ids=["unlimited", "exact-boundary", "excess", "unmeasurable"],
)
def test_transcription_limit_uses_stored_measurement_and_precedes_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    limit: int | None,
    probe: dict[str, Any],
    dispatches: bool,
    detail: str | None,
) -> None:
    project, media = _project_with_blob(
        tmp_path,
        payload=b"RIFF0000WAVEfmt ",
        probe=probe,
        mime="audio/wav",
        filename="source.wav",
    )
    probe_calls: list[str] = []
    dispatch_calls: list[str] = []
    original_probe = MediaBlobStore.probe_metadata

    def recording_probe(store: MediaBlobStore, digest: str) -> dict[str, Any]:
        probe_calls.append(digest)
        return original_probe(store, digest)

    async def fake_dispatch(
        _engine: str,
        path: str,
        _spec: dict[str, Any],
        _ctx: OpContext,
        **_kwargs: Any,
    ) -> transcribe_engines.TranscriptionEngineResult:
        dispatch_calls.append(path)
        return transcribe_engines.TranscriptionEngineResult(
            output={
                "text": "ok",
                "segments": [],
                "language": "en",
                "cost": 0.0,
            },
            model_calls=(),
        )

    monkeypatch.setattr(MediaBlobStore, "probe_metadata", recording_probe)
    monkeypatch.setattr(transcribe_engines, "run_transcription_engine", fake_dispatch)
    try:
        context = _context(
            project,
            _limits(**({"max_media_seconds": limit} if limit is not None else {})),
        )
        if dispatches:
            asyncio.run(_transcribe(media, context))
        else:
            with pytest.raises(_limit_error()) as raised:
                asyncio.run(_transcribe(media, context))
            assert raised.value.limit == "max_media_seconds"
            assert raised.value.detail == detail
    finally:
        project.close()

    assert bool(dispatch_calls) is dispatches
    # The admitted reader also uses stored duration for transcript evidence;
    # this is a metadata lookup, not a fresh ffprobe invocation.
    assert probe_calls == [media["blob"]]


@pytest.mark.parametrize(
    ("limit", "dispatches", "detail"),
    [(3, True, None), (2, False, None), (2, False, "measurement_unavailable")],
    ids=["exact-boundary", "excess", "unmeasurable"],
)
def test_content_sniffed_pdf_limit_precedes_rasterization_despite_spoofed_label(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    limit: int,
    dispatches: bool,
    detail: str | None,
) -> None:
    probe: dict[str, Any] = {"kind": "pdf"}
    if detail is None:
        probe["pages"] = 3
    project, media = _project_with_blob(
        tmp_path,
        payload=b"%PDF-1.4\nnot-an-image",
        probe=probe,
        mime="image/jpeg",
        filename="photo.jpg",
    )
    raster_calls: list[str] = []

    async def fake_page_images(
        _recipe: OcrEngines,
        path: Path,
        _media: Any,
        _spec: dict[str, Any],
        _scratch: Path,
    ) -> list[dict[str, Any]]:
        raster_calls.append(str(path))
        return []

    async def fake_run_pages(
        _recipe: OcrEngines,
        _engine: str,
        _paths: list[dict[str, Any]],
        _ctx: OpContext,
        **_kwargs: Any,
    ) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(OcrEngines, "_page_images", fake_page_images)
    monkeypatch.setattr(OcrEngines, "run_engine_on_pages", fake_run_pages)
    try:
        context = _context(project, _limits(max_pdf_pages=limit))
        if dispatches:
            asyncio.run(_recognize(media, context))
        else:
            with pytest.raises(_limit_error()) as raised:
                asyncio.run(_recognize(media, context))
            assert raised.value.limit == "max_pdf_pages"
            assert raised.value.detail == detail
    finally:
        project.close()

    assert bool(raster_calls) is dispatches


def test_opaque_pdf_uses_content_page_count_at_and_over_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = _pdf_bytes(2)
    source_path = tmp_path / "opaque.bin"
    source_path.write_bytes(payload)
    probe = probe_for_ingest(
        source_path,
        mime="application/octet-stream",
        filename="opaque.bin",
    )
    assert probe["kind"] == "file"
    assert "pages" not in probe
    project, media = _project_with_blob(
        tmp_path,
        payload=payload,
        probe=probe,
        mime="application/octet-stream",
        filename="opaque.bin",
    )
    raster_calls = _stub_ocr_dispatch(monkeypatch)
    try:
        asyncio.run(_recognize(media, _context(project, _limits(max_pdf_pages=2))))
        with pytest.raises(_limit_error()) as raised:
            asyncio.run(_recognize(media, _context(project, _limits(max_pdf_pages=1))))
        assert raised.value.limit == "max_pdf_pages"
        assert raised.value.detail is None
    finally:
        project.close()

    assert len(raster_calls) == 1


def test_prefixed_opaque_pdf_limit_precedes_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b"\n" + _pdf_bytes(2)
    source_path = tmp_path / "prefixed.bin"
    source_path.write_bytes(payload)
    probe = probe_for_ingest(
        source_path,
        mime="application/octet-stream",
        filename="prefixed.bin",
    )
    assert probe["kind"] == "file"
    assert "pages" not in probe
    project, media = _project_with_blob(
        tmp_path,
        payload=payload,
        probe=probe,
        mime="application/octet-stream",
        filename="prefixed.bin",
    )
    raster_calls = _stub_ocr_dispatch(monkeypatch)
    try:
        with pytest.raises(_limit_error()) as raised:
            asyncio.run(_recognize(media, _context(project, _limits(max_pdf_pages=1))))
        assert raised.value.limit == "max_pdf_pages"
        assert raised.value.detail is None
    finally:
        project.close()

    assert raster_calls == []


def test_non_pdf_content_with_pdf_labels_is_not_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b"plain text, not a PDF"
    source_path = tmp_path / "mislabelled.pdf"
    source_path.write_bytes(payload)
    probe = probe_for_ingest(
        source_path,
        mime="application/pdf",
        filename="mislabelled.pdf",
    )
    project, media = _project_with_blob(
        tmp_path,
        payload=payload,
        probe=probe,
        mime="application/pdf",
        filename="mislabelled.pdf",
    )
    raster_calls = _stub_ocr_dispatch(monkeypatch)
    try:
        asyncio.run(_recognize(media, _context(project, _limits(max_pdf_pages=1))))
    finally:
        project.close()

    assert len(raster_calls) == 1


def test_pdf_table_extraction_limit_stops_the_adapter_before_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = Project.create(tmp_path / "tables.frisket", name="tables")
    sheet_id, row_id, media = _stored_pdf(project)
    calls: list[object] = []

    def fake_extract(request: object) -> list[object]:
        calls.append(request)
        return []

    monkeypatch.setattr(natural_pdf, "extract_pdf_tables", fake_extract)

    async def read():
        reader = AdmittedPdfTablesReader(
            project, execution_limits=_limits(max_pdf_pages=2)
        )
        row = Row({"pdf": media})
        column_id = next(
            column["id"]
            for column in project.columns(sheet_id)
            if column["name"] == "pdf"
        )
        bound = reader.bind_row(
            row,
            sheet_id=sheet_id,
            row_id=row_id,
            sources={"pdf": {"column_id": column_id, "value": media}},
        )
        try:
            await bound.read(row, ColumnRef("pdf"))
        finally:
            await reader.aclose()

    try:
        with pytest.raises(_limit_error(), match="max_pdf_pages"):
            asyncio.run(read())
    finally:
        project.close()

    assert calls == []


def test_pdf_markdown_limit_stops_datalab_before_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from frisket.engine.executor import run_action_spec
    from frisket.engine.executor.action_inventory import ExecutorDeps

    monkeypatch.setenv("DATALAB_API_KEY", "test-key")
    project = Project.create(tmp_path / "markdown.frisket", name="markdown")
    sheet_id, _, _ = _stored_pdf(project)
    calls: list[Path] = []

    async def fake_datalab(
        _converter: _BoundDocumentConverter, path: Path
    ) -> tuple[str, dict[str, Any]]:
        calls.append(path)
        return "ok", {"model_calls": []}

    monkeypatch.setattr(_BoundDocumentConverter, "_convert_datalab", fake_datalab)
    router = ModelRouter(cache=None, cache_mode="off")
    composition = replace(
        open_execution_composition(
            project, router, ExecutionCompositionContext.direct()
        ),
        limits=_limits(max_pdf_pages=2),
    )
    deps = ExecutorDeps(
        router=router,
        execution_composition=composition,
        consent_coverage=ConsentCoverage(instance_principal(project), Decimal("0")),
    )
    request = {
        "action_id": "media.to_markdown",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {"source": "pdf", "engine": "datalab"},
        "idempotency_key": "pdf-limit",
    }
    try:
        quote = run_action_spec(project, request, project_id="limits", deps=deps)
        assert quote.status == "needs_confirmation", quote.errors
        request["confirmation"] = quote.errors[0].details["promise_set_hash"]
        result = run_action_spec(project, request, project_id="limits", deps=deps)
        assert result.status == "failed", result.errors
        error = project.db.execute(
            "SELECT error FROM results WHERE run_id=? AND error IS NOT NULL",
            (result.run_id,),
        ).fetchone()
        assert error is not None and "max_pdf_pages" in error["error"]
        assert calls == []
    finally:
        project.close()


def test_video_frames_limit_stops_ffprobe_before_subprocess_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, media = _project_with_blob(
        tmp_path,
        payload=b"video bytes",
        probe={"kind": "video", "duration_seconds": 3.0},
        mime="video/mp4",
        filename="source.mp4",
    )
    commands: list[list[str]] = []

    async def fake_sandboxed(command: list[str], **_kwargs: Any) -> SimpleNamespace:
        commands.append(command)
        return SimpleNamespace(
            ok=True,
            stdout=json.dumps({"format": {"duration": "3"}}),
            stderr="",
        )

    monkeypatch.setattr(video_frames, "run_sandboxed", fake_sandboxed)
    from frisket.actions.row_media_types import FrameCount
    from frisket.engine.executor.row_file_stage import RowFileStager

    async def read():
        sheet_id = project.add_sheet("Video")
        column_id = project.add_column(sheet_id, "video", type="video")
        row_id = project.add_rows(sheet_id, [{"video": media}], {"video": column_id})[0]
        row = Row({"video": media})
        stager = RowFileStager(project)
        owner = video_frames.AdmittedFrameExtractor(
            project, stager, execution_limits=_limits(max_media_seconds=2)
        )
        reader = owner.bind_row(
            row,
            sheet_id=sheet_id,
            row_id=row_id,
            sources={"video": {"column_id": column_id, "value": media}},
        )
        try:
            await reader.extract(row, ColumnRef("video"), sampling=FrameCount(count=1))
        finally:
            await owner.aclose()
            stager.close()

    try:
        with pytest.raises(_limit_error(), match="max_media_seconds"):
            asyncio.run(read())
    finally:
        project.close()

    assert commands == []


def test_visual_cuts_limit_stops_pyscenedetect_before_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import frisket.engine.executor.visual_cuts_read as visual_reader
    from frisket.actions.registry import ACTION_REGISTRY
    from frisket.actions.system import BoundTypedActionRequest
    from frisket.actions.types import ActionRequest, SheetRows
    from frisket.engine.executor.map_rows_action import build_typed_map_rows_plan

    project = Project.create(tmp_path / "cuts.frisket", name="cuts")
    sheet_id = project.add_sheet("Video")
    column_id = project.add_column(sheet_id, "video", type="video")
    digest = project.add_blob(
        b"video bytes",
        filename="source.mp4",
        mime="video/mp4",
        metadata=owned_media_metadata_document(
            probe={"kind": "video", "duration_seconds": 3.0}
        ),
    )
    row_id = project.add_rows(
        sheet_id,
        [{"video": media_cell(digest, mime="video/mp4", filename="source.mp4")}],
        {"video": column_id},
    )[0]
    calls: list[Path] = []

    def fake_visual_cuts(path: Path, **_kwargs: Any) -> dict[str, Any]:
        calls.append(path)
        return {"points": []}

    monkeypatch.setattr(visual_reader, "visual_cuts_value", fake_visual_cuts)
    try:
        request = ActionRequest(
            action_id="map.find_visual_cuts",
            scope=SheetRows(sheet_id=sheet_id, row_ids=(row_id,)),
            params={"source": "video"},
            output_names={"cuts": "Cuts"},
            idempotency_key="visual-cuts-limit",
        )
        plan = build_typed_map_rows_plan(
            project,
            BoundTypedActionRequest.bind(
                ACTION_REGISTRY.get(request.action_id), request
            ),
        )
        router = ModelRouter(cache=None, cache_mode="off", use_env_keys=False)
        composition = replace(
            open_execution_composition(
                project, router, ExecutionCompositionContext.direct()
            ),
            limits=_limits(max_media_seconds=2),
        )
        preview = asyncio.run(
            MapRunner(
                project,
                router,
                authority=UnroutedOnlyAuthority(project),
                execution_composition=composition,
            ).preview(plan.spec_dict(), program=plan.program)
        )
        assert "max_media_seconds" in preview.values[row_id]["Cuts"]["error"]
    finally:
        project.close()

    assert calls == []


@pytest.mark.parametrize("duration", [float("nan"), -1.0], ids=["nan", "negative"])
def test_malformed_media_measurement_fails_closed_before_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, duration: float
) -> None:
    project, media = _project_with_blob(
        tmp_path,
        payload=b"RIFF0000WAVEfmt ",
        probe={"kind": "audio", "duration_seconds": 1.0},
        mime="audio/wav",
        filename="source.wav",
    )
    calls: list[str] = []

    async def fake_dispatch(
        _engine: str, path: str, *_args: Any, **_kwargs: Any
    ) -> transcribe_engines.TranscriptionEngineResult:
        calls.append(path)
        return transcribe_engines.TranscriptionEngineResult(
            output={
                "text": "ok",
                "segments": [],
                "language": "en",
                "cost": 0.0,
            },
            model_calls=(),
        )

    monkeypatch.setattr(
        MediaBlobStore,
        "probe_metadata",
        lambda _store, _digest: {"kind": "audio", "duration_seconds": duration},
    )
    monkeypatch.setattr(transcribe_engines, "run_transcription_engine", fake_dispatch)
    try:
        with pytest.raises(_limit_error(), match="measurement_unavailable"):
            asyncio.run(
                _transcribe(media, _context(project, _limits(max_media_seconds=2)))
            )
    finally:
        project.close()

    assert calls == []


@pytest.mark.parametrize("pages", [True, 0, -1], ids=["bool", "zero", "negative"])
def test_malformed_pdf_measurement_fails_closed_before_rasterization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pages: int | bool
) -> None:
    project, media = _project_with_blob(
        tmp_path,
        payload=b"%PDF-1.4\nexample",
        probe={"kind": "pdf", "pages": 1},
        mime="application/pdf",
        filename="source.pdf",
    )
    raster_calls: list[Path] = []

    async def fake_page_images(
        _recipe: OcrEngines, path: Path, *_args: Any, **_kwargs: Any
    ) -> list[Path]:
        raster_calls.append(path)
        return []

    async def fake_engine(*_args: Any, **_kwargs: Any) -> list[Any]:
        return []

    monkeypatch.setattr(
        MediaBlobStore,
        "probe_metadata",
        lambda _store, _digest: {"kind": "pdf", "pages": pages},
    )
    monkeypatch.setattr(OcrEngines, "_page_images", fake_page_images)
    monkeypatch.setattr(OcrEngines, "run_engine_on_pages", fake_engine)
    try:
        with pytest.raises(_limit_error(), match="measurement_unavailable"):
            asyncio.run(_recognize(media, _context(project, _limits(max_pdf_pages=2))))
    finally:
        project.close()

    assert raster_calls == []


@dataclass
class _LimitCaptureRecipe(Recipe):
    name: str = "limit_capture"
    llm: bool = False
    cost_class = "free"
    consumes_resolution = False

    def __post_init__(self) -> None:
        self.observed_limits: list[Any] = []

    def output_fields(self, _spec: dict[str, Any]) -> list[dict[str, str]]:
        return [{"name": "out", "column_type": "text"}]

    async def execute(
        self, _values: dict[str, Any], _spec: dict[str, Any], ctx: OpContext
    ) -> dict[str, str]:
        self.observed_limits.append(ctx.execution_limits)
        return {"out": "ok"}


def test_run_preview_and_row_clone_receive_the_same_execution_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recipe = _LimitCaptureRecipe()
    monkeypatch.setattr(validation, "recipe_for_spec", lambda _spec: recipe)
    project = Project.create(tmp_path / "propagation.frisket", name="propagation")
    sheet_id = project.add_sheet("Rows")
    row_id = project.add_rows(sheet_id, [{}], {})[0]
    router = ModelRouter(cache=None, cache_mode="off")
    limits = _limits(max_media_seconds=60, max_pdf_pages=12)
    composition = replace(
        open_execution_composition(
            project, router, ExecutionCompositionContext.direct()
        ),
        limits=limits,
    )
    runner = MapRunner(
        project,
        router,
        authority=UnroutedOnlyAuthority(project),
        execution_composition=composition,
    )
    spec = {
        "action_kind": "test.limit_capture",
        "sheet_id": sheet_id,
        "row_ids": [row_id],
    }
    try:
        progress = asyncio.run(run_with_output_claim(runner, spec, confirmed=True))
        assert progress.failed == 0
        preview = asyncio.run(runner.preview(spec))
        assert set(preview.values) == {row_id}
    finally:
        project.close()

    assert recipe.observed_limits == [limits, limits]
