"""Admitted OCR reads; result publication and evidence links belong to the host."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import tempfile
import uuid
from contextlib import AsyncExitStack
from pathlib import Path

from frisket.actions.media_options import OcrOptions
from frisket.actions.media_types import OcrText, RecognizedDocument
from frisket.actions.types import ColumnRef, Outcome, RowError
from frisket.execution.resolver import preview_resolution_in_scope
from frisket.ai.models.metadata import model_calls_cost_actual
from frisket.ops import ocr_engines as engines
from frisket.engine.executor.blob_outputs import RowBlobOutput, RowBlobPlan
from frisket.engine._workers.pdf_render_worker import is_environment_failure
from frisket.engine.pdf_render import (
    PdfRenderCancelled,
    PdfRenderEnvironmentError,
    render_pdf_pages,
)
from frisket.engine.sandbox.fence import SandboxEnforcementUnavailable
from frisket.engine.sandbox.shim import SandboxTeardownError
from frisket.engine.pdf_page_source import (
    PdfPageSourceError,
    materialize_pdf_page,
)
from frisket.engine.executor.visual_cuts_read import _settle
from frisket.engine.store.artifact_timeline import canonical_json_hash
from frisket.engine.store.media_blobs import (
    MediaBlobStore,
    owned_media_metadata_document,
)
from frisket.execution.attempt import routed_admission_in_scope
from frisket.execution.provider import enforce_pdf_page_limit
from frisket.execution.resolve_for_action import authored_options
from frisket.execution.runtime_binding import bind_fact_to_route
from frisket.execution.targets import CAPABILITY_OCR
from frisket.ops.base import RecipeInvocationHalt
from frisket.contracts.actions.schemas._engines import is_generic_vlm_ocr_engine


def ocr_token_granularity(
    engine: str, *, keep_text_position: bool | None = None
) -> str:
    """Describe the geometry emitted by the selected OCR adapter."""
    if engine == "tesseract":
        return "word"
    if engine in {engines.LIGHT_ENGINE, "pp-ocrv6", engines.DATALAB_ENGINE}:
        return "line"
    if is_generic_vlm_ocr_engine(engine):
        return "block" if keep_text_position is False else "line"
    return "block"


def _page_coordinate_frame(page: dict, *, required: bool) -> dict[str, int]:
    width, height = page.get("width"), page.get("height")
    if width is None and height is None and not required:
        return {}
    if type(width) is not int or type(height) is not int or width <= 0 or height <= 0:
        raise RuntimeError("positioned OCR returned no valid coordinate frame")
    return {"width": width, "height": height}


def _page_warnings(pages, *, first_page):
    failures, warnings = 0, []
    for number, page in enumerate(pages, first_page):
        if error := page.get("error"):
            failures += 1
            if len(warnings) < 19:
                warnings.append(f"Page {number} failed during {error['stage']}.")
    if failures and failures == len(pages):
        raise RowError(
            "ocr_pages_failed", f"OCR failed for all {len(pages)} pages. {warnings[0]}"
        )
    if failures > len(warnings):
        warnings.append(
            f"{failures} pages failed; {failures - len(warnings)} "
            "additional page warnings omitted."
        )
    return tuple(warnings)


class AdmittedOcrReader:
    def __init__(self, ctx, files, *, engine=None, options):
        self._ctx, self._project, self._files = ctx, ctx.project, files
        self._engine = engines.ENGINE_ALIASES.get(engine, engine) if engine else None
        self._options = copy.deepcopy(options)
        self._closed = False
        self._started = False
        self._called_rows = set()
        self._tasks = set()
        self._scope = AsyncExitStack()
        self._engines = engines.OcrEngines(cancelled=self._cancelled)
        self.accounting_by_row = {}
        self.calls_by_row = {}

    def _cancelled(self):
        cancel = self._ctx.extras.get("cancelled")
        return self._closed or bool(callable(cancel) and cancel())

    def _check_open(self):
        if self._closed:
            raise RuntimeError("OCR reader is closed")
        if self._cancelled():
            raise asyncio.CancelledError

    def _admitted_engine(self, ctx):
        admission = routed_admission_in_scope(ctx.extras)
        preview = preview_resolution_in_scope(ctx.extras)
        engine = (
            admission.route.engine
            if admission is not None
            else preview.facts.engine
            if preview is not None
            else self._engine
        )
        if engine is None:
            raise RecipeInvocationHalt(
                "promise_violation", "OCR requires its admitted engine"
            )
        expected = (
            "frisket.cloud.media.v1"
            if (
                admission is not None
                and admission.binding.facts.cloud_payer is not None
            )
            or (preview is not None and preview.facts.cloud_payer is not None)
            else "local"
            if engine in engines.LOCAL_ENGINES
            else "sidecar.ocr"
            if engine in engines.SIDECAR_ENGINES
            else "datalab.convert"
            if engine == engines.DATALAB_ENGINE
            else "opendocrouter.parse"
            if engine.startswith("opendocrouter/")
            else "remote"
        )
        if admission is None and preview is None:
            if engine not in engines.LOCAL_ENGINES:
                raise RecipeInvocationHalt(
                    "promise_violation", "Remote OCR requires its admitted route"
                )
        elif admission is not None and (
            admission.route.target_snapshot.get("capability") != CAPABILITY_OCR
            or admission.route.target_snapshot.get("transport") != expected
            or (self._engine is not None and engine != self._engine)
            or admission.route.options
            != authored_options(self._options, CAPABILITY_OCR)
        ):
            raise RecipeInvocationHalt(
                "promise_violation", "OCR engine or options differ from admission"
            )
        elif preview is not None and (
            preview.support.capability != CAPABILITY_OCR
            or preview.support.transport != expected
            or (self._engine is not None and engine != self._engine)
        ):
            raise RecipeInvocationHalt(
                "promise_violation", "OCR engine differs from its preview resolution"
            )
        return engine

    async def start(self, *, expected_rows):
        self._check_open()
        if self._started:
            raise RuntimeError("OCR reader already started")
        engine = self._admitted_engine(self._ctx)
        if OcrOptions.model_validate(self._options).normalize(engine) != self._options:
            raise RecipeInvocationHalt(
                "promise_violation", "OCR options are not normalized"
            )
        self._started = True
        if engine not in engines.RUN_SCOPED_ENGINES:
            return
        self._engines.pool = await self._scope.enter_async_context(
            engines.rapidocr_execution_scope(
                expected_rows=expected_rows,
                language=self._options.get("language"),
                cancelled=self._cancelled,
            )
        )

    def bind_row(self, row, *, sheet_id, row_id, sources, ctx):
        self._check_open()
        if not self._started:
            raise RuntimeError("OCR reader requires its invocation scope")
        if (
            type(sheet_id) is not int
            or type(row_id) is not int
            or min(sheet_id, row_id) <= 0
        ):
            raise RowError("invalid_input_ref", "OCR requires its admitted row")
        return _BoundOcrReader(
            self, row, sheet_id, row_id, copy.deepcopy(dict(sources or {})), ctx
        )

    async def _run(self, operation):
        self._check_open()
        task = asyncio.create_task(operation())
        self._tasks.add(task)
        try:
            return await task
        finally:
            await _settle(task)
            self._tasks.discard(task)

    async def aclose(self):
        self._closed = True
        for task in tuple(self._tasks):
            task.cancel()
        for task in tuple(self._tasks):
            await _settle(task)
        try:
            await self._scope.aclose()
        finally:
            self._engines.pool = None


class _BoundOcrReader:
    def __init__(self, owner, row, sheet_id, row_id, sources, ctx):
        self._owner, self._row, self._ctx = owner, row, ctx
        self._sheet_id, self._row_id, self._sources = sheet_id, row_id, sources

    def _source(self, row, source):
        if row is not self._row or not isinstance(source, ColumnRef):
            raise RowError("invalid_input_ref", "OCR requires its admitted source")
        captured = self._sources.get(source.name)
        if (
            not isinstance(captured, dict)
            or source.name not in row.values
            or canonical_json_hash(source.read(row))
            != canonical_json_hash(captured["value"])
        ):
            raise RowError("stale_input", "OCR source differs from its admitted cell")
        project = self._owner._project
        column = project.db.execute(
            "SELECT sheet_id,type,hidden FROM columns WHERE id=?",
            (captured["column_id"],),
        ).fetchone()
        if (
            column is None
            or column["sheet_id"] != self._sheet_id
            or column["hidden"]
            or column["type"] not in {"image", "file"}
            or column["type"] != captured.get("column_type")
        ):
            raise RowError(
                "invalid_input_ref", "OCR source column is unavailable or incompatible"
            )
        value = captured["value"]
        if not isinstance(value, dict) or not isinstance(value.get("blob"), str):
            raise RowError(
                "invalid_media_cell", "OCR requires a blob-backed image or PDF"
            )
        blob = MediaBlobStore(project).blob_row(value["blob"])
        if blob is None:
            raise RowError("missing_blob", "OCR source blob is missing")
        page = value.get("page")
        if page is not None and (
            type(page) is not int or page < 1 or column["type"] != "file"
        ):
            raise RowError(
                "invalid_media_cell",
                "OCR PDF page selector requires a positive integer file page",
            )
        source_ref = {
            "sheet_id": self._sheet_id,
            "row_id": self._row_id,
            "column_id": captured["column_id"],
            "column_type": column["type"],
            "source_column": source.name,
            "blob_hash": value["blob"],
            "value_hash": canonical_json_hash(value),
            "filename": str(value.get("filename") or blob["filename"] or "document"),
            "mime": str(
                value.get("mime") or blob["mime"] or "application/octet-stream"
            ),
            "size": blob["size"],
        }
        if page is not None:
            source_ref["page"] = page
        return source_ref

    async def recognize(self, row, source, *, options):
        owner = self._owner
        owner._check_open()
        engine = owner._admitted_engine(self._ctx)
        if (
            not isinstance(options, OcrOptions)
            or options.normalize(engine) != owner._options
        ):
            raise RecipeInvocationHalt(
                "promise_violation", "OCR call options differ from admission"
            )
        source_ref = self._source(row, source)
        if self._row_id in owner._called_rows:
            raise RuntimeError("OCR permits one recognition per admitted row")
        owner._called_rows.add(self._row_id)
        return await owner._run(lambda: self._recognize(source_ref, engine))

    async def _recognize(self, source, engine):
        owner, ctx = self._owner, self._ctx
        options = owner._options
        usage = {"calls": 0, "in": 0, "out": 0, "cost": 0.0, "duration_ms": 0}
        pages = []
        with owner._project.materialize_blob(source["blob_hash"]) as raw_path:
            original_path = Path(raw_path)
            with tempfile.TemporaryDirectory(prefix="frisket-ocr-") as td:
                scratch = Path(td)
                is_pdf = owner._engines._is_pdf(original_path, source)
                selected_page = source.get("page")
                if selected_page is not None and not is_pdf:
                    raise RowError(
                        "invalid_media_cell", "OCR page selector requires a PDF source"
                    )
                path = original_path
                page_count = None
                if is_pdf:
                    probe = MediaBlobStore(owner._project).probe_metadata(
                        source["blob_hash"]
                    )
                    page_count = probe.get("pages")
                    if type(page_count) is not int or page_count <= 0:
                        page_count = owner._engines._pdf_page_count(
                            original_path, source["blob_hash"]
                        )
                    if selected_page is not None:
                        try:
                            path = materialize_pdf_page(
                                original_path,
                                page=selected_page,
                                destination=scratch / "selected-page.pdf",
                            )
                        except PdfPageSourceError as exc:
                            raise RowError("invalid_media_cell", str(exc)) from exc
                    limits = ctx.execution_limits
                    if limits is not None and limits.max_pdf_pages is not None:
                        enforce_pdf_page_limit(
                            limits.max_pdf_pages,
                            1 if selected_page is not None else page_count,
                        )
                    if self._incremental_engine(engine) and (
                        type(page_count) is not int or page_count <= 0
                    ):
                        # A bounded first chunk can still open PDFs that the
                        # inexpensive metadata probe cannot inspect.
                        with tempfile.TemporaryDirectory(
                            prefix="count-", dir=scratch
                        ) as count_dir:
                            try:
                                rendered = await render_pdf_pages(
                                    original_path,
                                    Path(count_dir),
                                    dpi=options["dpi"],
                                    pages=[1],
                                    timeout_seconds=600,
                                    should_cancel=owner._cancelled,
                                    max_edge=engines.RAPIDOCR_MAX_IMAGE_EDGE
                                    if engine == engines.LIGHT_ENGINE
                                    else None,
                                )
                            except PdfRenderCancelled as exc:
                                raise engines.OcrCancelled("OCR cancelled") from exc
                            except PdfRenderEnvironmentError as exc:
                                raise RecipeInvocationHalt(
                                    "local_session_failed",
                                    "The PDF rendering environment failed; repair local storage or runtime before resuming.",
                                ) from exc
                            page_count = rendered.page_count
                incremental = is_pdf and self._incremental_engine(engine)
                page_images = {}
                warnings = ()
                returned = False
                try:
                    if incremental:
                        if type(page_count) is not int or page_count <= 0:
                            raise RowError(
                                "invalid_media_cell", "PDF page count could not be read"
                            )
                        source["page_count"] = page_count
                        recognize_pdf = (
                            self._recognize_indra_pdf
                            if engine == engines.INDRA_OCR_ENGINE
                            else self._recognize_pdf_pages
                        )
                        pages, page_images, warnings = await recognize_pdf(
                            source,
                            path,
                            engine,
                            scratch,
                            usage,
                            page_count=1 if selected_page is not None else page_count,
                            first_page=selected_page or 1,
                        )
                    else:
                        local_image = not is_pdf and self._incremental_engine(engine)
                        if local_image:
                            owner._check_open()
                            self._report_page_progress(0, 1)
                        page_paths = await owner._engines._page_images(
                            path, source, options, scratch, engine=engine
                        )
                        if type(page_count) is not int or page_count <= 0:
                            page_count = len(page_paths)
                        source["page_count"] = page_count
                        usage["input_pages"] = len(page_paths)
                        pages = await owner._engines.run_engine_on_pages(
                            engine,
                            page_paths,
                            ctx,
                            usage=usage,
                            language=options.get("language"),
                            keep_text_position=options.get("keep_text_position"),
                            scratch=scratch,
                        )
                        page_images = self._page_images(
                            source,
                            path,
                            page_paths,
                            scratch,
                            first_page=selected_page or 1,
                        )
                        if local_image:
                            owner._check_open()
                            self._report_page_progress(1, 1)
                    returned = True
                finally:
                    if usage.get("model_calls") or usage["calls"] or returned:
                        calls = usage.get("model_calls") or [
                            owner._engines._model_call_for(
                                engine, path, pages, usage, ctx=ctx
                            ).as_dict()
                        ]
                        admission = routed_admission_in_scope(ctx.extras)
                        if admission is not None:
                            calls = [
                                bind_fact_to_route(admission.route, call)
                                for call in calls
                            ]
                        owner.accounting_by_row[self._row_id] = copy.deepcopy(
                            {
                                "tokens_in": usage["in"],
                                "tokens_out": usage["out"],
                                "cost": model_calls_cost_actual(calls),
                                "model_calls": calls,
                            }
                        )
                if selected_page is not None and len(pages) != 1:
                    raise RuntimeError("page-scoped OCR must return exactly one page")
                owner._check_open()
                pdf = (
                    self._searchable_pdf(
                        source,
                        path,
                        pages,
                        page_images,
                        engine,
                        scratch,
                    )
                    if options["searchable_pdf"] and not owner._files.preview
                    else Outcome.ok(None)
                )
                owner._check_open()
                fact_pages = []
                blocks = []
                fact_blocks = []
                positioned_generic = is_generic_vlm_ocr_engine(engine) and options.get(
                    "keep_text_position", True
                )
                for index, page in enumerate(pages, 1):
                    physical_page = selected_page or index
                    positions = _page_coordinate_frame(
                        page, required=bool(positioned_generic)
                    )
                    page_fact = copy.deepcopy(page)
                    page_fact["page_number"] = physical_page
                    fact_pages.append(page_fact)
                    block_page = {
                        "page": physical_page,
                        "engine": engine,
                        "blocks": copy.deepcopy(page.get("blocks", [])),
                    }
                    blocks.append(block_page)
                    fact_block = copy.deepcopy(block_page)
                    if positions:
                        fact_block["positions"] = positions
                    fact_blocks.append(fact_block)
                text = "\n\n".join(str(page.get("text") or "") for page in pages)
                fact = {
                    "kind": "ocr_read",
                    "call_id": uuid.uuid4().hex,
                    "engine": engine,
                    "ocr_token_granularity": ocr_token_granularity(
                        engine,
                        keep_text_position=options.get("keep_text_position"),
                    ),
                    "options": copy.deepcopy(options),
                    "source": source,
                    "text": text,
                    "blocks": fact_blocks,
                    "pages": fact_pages,
                    "page_images": page_images,
                    "warnings": list(warnings),
                }
                owner.calls_by_row[self._row_id] = [copy.deepcopy(fact)]
                document = RecognizedDocument(
                    text=OcrText(text), blocks=blocks, pdf=pdf
                )
                document._warnings = warnings
                return document

    def _incremental_engine(self, engine):
        # Connected Cloud and sidecars own a single admitted request. Splitting
        # that request here would change accounting and admission semantics.
        admission = routed_admission_in_scope(self._ctx.extras)
        preview = preview_resolution_in_scope(self._ctx.extras)
        transport = (
            admission.route.target_snapshot.get("transport")
            if admission is not None
            else preview.support.transport
            if preview is not None
            else None
        )
        return engine in engines.LOCAL_ENGINES and (
            transport != "frisket.cloud.media.v1"
        )

    async def _recognize_indra_pdf(
        self, source, path, engine, scratch, usage, *, page_count, first_page
    ):
        del source, engine
        self._owner._check_open()
        self._report_page_progress(0, page_count)

        pages = await self._owner._engines._ocr_indra_pdf(
            path,
            scratch,
            dpi=self._owner._options["dpi"],
            page_count=page_count,
            report_progress=self._report_page_progress,
        )
        self._owner._check_open()
        images = {}
        for number, page in enumerate(pages, first_page):
            positions = _page_coordinate_frame(
                page, required=not bool(page.get("error"))
            )
            if positions and not self._owner._files.preview:
                images[str(number)] = {
                    "mime": "application/pdf",
                    "source_width": positions["width"],
                    "source_height": positions["height"],
                }
        usage["input_pages"] = sum(
            page.get("error", {}).get("stage") != "rendering" for page in pages
        )
        return pages, images, _page_warnings(pages, first_page=first_page)

    def _report_page_progress(self, completed, total):
        report = self._ctx.extras.get("report_work_progress")
        if callable(report):
            report(
                kind="ocr_pages",
                row_id=self._row_id,
                completed=completed,
                total=total,
            )

    async def _recognize_pdf_pages(
        self, source, path, engine, scratch, usage, *, page_count, first_page
    ):
        owner, options = self._owner, self._owner._options
        pages, images = [], {}
        consecutive_failures = 0
        usage["input_pages"] = 0
        owner._check_open()
        self._report_page_progress(0, page_count)
        for number in range(1, page_count + 1):
            owner._check_open()
            physical_page = first_page + number - 1
            # The directory is page-owned, including renderer output created
            # before a failure. It is removed before the next page is started.
            with tempfile.TemporaryDirectory(prefix="page-", dir=scratch) as td:
                page_scratch = Path(td)
                stage = "rendering"
                try:
                    paths = await owner._engines._page_images(
                        path,
                        source,
                        {**options, "_selected_pages": [number]},
                        page_scratch,
                        engine=engine,
                    )
                    if len(paths) != 1:
                        raise RuntimeError(
                            "PDF rendering returned the wrong page count"
                        )
                    stage = "reading image geometry"
                    frame = self._page_images(
                        source, path, paths, page_scratch, first_page=physical_page
                    )
                    images.update(frame)
                    stage = "recognition"
                    usage["input_pages"] += 1
                    output = await owner._engines.run_engine_on_pages(
                        engine,
                        paths,
                        self._ctx,
                        usage=usage,
                        language=options.get("language"),
                        keep_text_position=options.get("keep_text_position"),
                        scratch=page_scratch,
                    )
                    if len(output) != 1:
                        raise RuntimeError("OCR returned the wrong page count")
                    pages.append(output[0])
                    consecutive_failures = 0
                except (
                    engines.OcrCancelled,
                    RecipeInvocationHalt,
                    SandboxTeardownError,
                ):
                    raise
                except SandboxEnforcementUnavailable as exc:
                    raise RecipeInvocationHalt(
                        "local_session_failed",
                        "The local OCR sandbox is unavailable; repair the local runtime before resuming.",
                    ) from exc
                except (RuntimeError, OSError, ValueError) as exc:
                    owner._check_open()
                    if is_environment_failure(exc):
                        raise RecipeInvocationHalt(
                            "local_session_failed",
                            "The local OCR environment failed; repair local storage or runtime before resuming.",
                        ) from exc
                    consecutive_failures += 1
                    if consecutive_failures >= 5:
                        raise RowError(
                            "ocr_pages_failed",
                            f"OCR stopped after 5 consecutive page failures during {stage} (page {physical_page}).",
                        ) from None
                    pages.append(
                        {
                            "text": "",
                            "blocks": [],
                            "error": {"code": "ocr_page_failed", "stage": stage},
                        }
                    )
            owner._check_open()
            self._report_page_progress(number, page_count)
        return pages, images, _page_warnings(pages, first_page=first_page)

    def _page_images(self, source, original, paths, scratch, *, first_page=1):
        if self._owner._files.preview:
            return {}
        images = {}
        is_pdf = self._owner._engines._is_pdf(original, source)
        for index, path in enumerate(paths, first_page):
            if is_pdf:
                with path.open("rb") as image:
                    dimensions = engines.image_dimensions(image.read(24))
                if dimensions is None:
                    raise RuntimeError(f"Page {index} has no valid image geometry")
                width, height = dimensions
                # The original PDF is the durable source. Display pages are
                # rendered on demand; retain only OCR's exact coordinate frame.
                images[str(index)] = {
                    "mime": "application/pdf",
                    "source_width": width,
                    "source_height": height,
                }
                continue
            width, height = engines.image_dimensions(path.read_bytes()) or (None, None)
            images[str(index)] = {
                "blob_hash": source["blob_hash"],
                "mime": source["mime"],
                "filename": source["filename"],
                "size": path.stat().st_size,
                "width": width,
                "height": height,
                "source_width": width,
                "source_height": height,
                "downscaled": False,
                "source_blob": True,
            }
        return images

    def _searchable_pdf(self, source, path, pages, page_images, engine, scratch):
        from frisket.ops.searchable_pdf import (
            COMPOSITOR_VERSION,
            compose_searchable_pdf,
        )
        from frisket.authoring.output_names import (
            OutputNameTemplateError,
            format_output_name,
            source_stem,
        )

        if not self._owner._engines._is_pdf(path, source):
            return Outcome.failed(
                "searchable_pdf_failed",
                "Searchable PDF requires a PDF source and an engine with text geometry",
            )
        try:
            raster_dimensions = [
                ((frame["source_width"], frame["source_height"]) if frame else None)
                for index in range(1, len(pages) + 1)
                for frame in [page_images.get(str(source.get("page") or index))]
            ]
            pdf, results = compose_searchable_pdf(
                path.read_bytes(),
                pages,
                dpi=self._owner._options["dpi"],
                raster_dimensions=raster_dimensions,
            )
        except Exception as exc:
            return Outcome.failed(
                "searchable_pdf_failed",
                f"Searchable PDF composition failed: {exc}"[:500],
            )
        try:
            stem = source_stem(source["filename"])
        except OutputNameTemplateError:
            stem = "document"
        filename = format_output_name(
            "{source_stem}.searchable.pdf", {"source_stem": stem}
        )
        output = scratch / "searchable.pdf"
        output.write_bytes(pdf)
        degraded = [result.index for result in results if result.degraded]
        metadata = owned_media_metadata_document(
            probe={"kind": "pdf", "pages": len(results)},
            owner={
                "pages_with_text": sum(bool(r.painted_count) for r in results),
                "pages_degraded": len(degraded),
                "degraded_pages": degraded,
                "compositor_version": COMPOSITOR_VERSION,
            },
        )
        handle = self._owner._files.bind_row(self._row_id).stage_output(
            RowBlobOutput(
                primary=RowBlobPlan(
                    role="searchable_pdf",
                    content_digest=hashlib.sha256(pdf).hexdigest(),
                    staged_path=output,
                    filename=filename,
                    mime="application/pdf",
                    source_url=None,
                    metadata=metadata,
                ),
                facts={
                    "kind": "searchable_pdf",
                    "source_blob_hash": source["blob_hash"],
                    "engine": engine,
                    "dpi": self._owner._options["dpi"],
                    "degraded_pages": degraded,
                    "compositor_version": COMPOSITOR_VERSION,
                },
            )
        )
        return Outcome.ok(handle)
