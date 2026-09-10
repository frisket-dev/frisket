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
            "local"
            if engine in engines.LOCAL_ENGINES
            else "sidecar.ocr"
            if engine in engines.SIDECAR_ENGINES
            else "datalab.convert"
            if engine == engines.DATALAB_ENGINE
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
        return {
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
            path = Path(raw_path)
            with tempfile.TemporaryDirectory(prefix="frisket-ocr-") as td:
                scratch = Path(td)
                if owner._engines._is_pdf(path, source):
                    limits = ctx.execution_limits
                    if limits is not None and limits.max_pdf_pages is not None:
                        probe = MediaBlobStore(owner._project).probe_metadata(
                            source["blob_hash"]
                        )
                        count = probe.get("pages")
                        if type(count) is not int or count <= 0:
                            count = owner._engines._pdf_page_count(
                                path, source["blob_hash"]
                            )
                        enforce_pdf_page_limit(limits.max_pdf_pages, count)
                page_paths = await owner._engines._page_images(
                    path, source, options, scratch
                )
                usage["input_pages"] = len(page_paths)
                returned = False
                try:
                    pages = await owner._engines.run_engine_on_pages(
                        engine,
                        page_paths,
                        ctx,
                        usage=usage,
                        language=options.get("language"),
                        scratch=scratch,
                    )
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
                text = "\n\n".join(p["text"] for p in pages if p.get("text")).strip()
                blocks = [
                    {"page": i + 1, "engine": engine, "blocks": p.get("blocks", [])}
                    for i, p in enumerate(pages)
                ]
                fact = {
                    "kind": "ocr_read",
                    "call_id": uuid.uuid4().hex,
                    "engine": engine,
                    "options": copy.deepcopy(options),
                    "source": source,
                    "text": text,
                    "blocks": copy.deepcopy(blocks),
                    "pages": copy.deepcopy(pages),
                    "page_images": self._page_images(source, path, page_paths, scratch),
                }
                owner.calls_by_row[self._row_id] = [copy.deepcopy(fact)]
                pdf = (
                    self._searchable_pdf(source, path, pages, engine, scratch)
                    if options["searchable_pdf"] and not owner._files.preview
                    else Outcome.ok(None)
                )
                return RecognizedDocument(text=OcrText(text), blocks=blocks, pdf=pdf)

    def _page_images(self, source, original, paths, scratch):
        if self._owner._files.preview:
            return {}
        from PIL import Image

        images = {}
        is_pdf = self._owner._engines._is_pdf(original, source)
        for index, path in enumerate(paths, 1):
            width, height = engines.image_dimensions(path.read_bytes()) or (None, None)
            actual_width, actual_height = width, height
            downscaled = bool(is_pdf and max(width or 0, height or 0) > 2000)
            if downscaled:
                with Image.open(path) as image:
                    image.thumbnail((2000, 2000))
                    path = scratch / f"display-{index}.png"
                    image.save(path, format="PNG")
                    width, height = image.size
            digest = source["blob_hash"]
            if is_pdf:
                expected = hashlib.sha256(path.read_bytes()).hexdigest()
                digest = self._owner._project.blob_store.put_path(
                    path, expected_digest=expected
                )
                if digest != expected:
                    raise RowError(
                        "invalid_output", "OCR page storage returned different bytes"
                    )
            images[str(index)] = {
                "blob_hash": digest,
                "mime": "image/png" if is_pdf else source["mime"],
                "filename": f"page-{index}.png" if is_pdf else source["filename"],
                "size": path.stat().st_size,
                "width": width,
                "height": height,
                "source_width": actual_width,
                "source_height": actual_height,
                "downscaled": downscaled,
                "source_blob": not is_pdf,
            }
        return images

    def _searchable_pdf(self, source, path, pages, engine, scratch):
        from frisket.ops.searchable_pdf import (
            COMPOSITOR_VERSION,
            compose_searchable_pdf,
        )
        from frisket.authoring.output_names import (
            OutputNameTemplateError,
            format_output_name,
            source_stem,
        )

        if "/" in engine or not self._owner._engines._is_pdf(path, source):
            return Outcome.failed(
                "searchable_pdf_failed",
                "Searchable PDF requires a PDF source and an engine with text geometry",
            )
        try:
            pdf, results = compose_searchable_pdf(
                path.read_bytes(), pages, dpi=self._owner._options["dpi"]
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
